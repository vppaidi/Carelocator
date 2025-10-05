from flask import Flask, render_template, session, redirect, url_for, session, request, jsonify
import os
import pandas as pd
import numpy as np
import pulp

from spopt.locate import PMedian, PCenter  # p-median & p-center
import osmnx as ox
import networkx as nx
import geopandas as gpd
from shapely.geometry import Point
import time
import io
import csv
import datetime
import redis
import json
from flask_rq2 import RQ
from rq import Queue
from redis import Redis
from rq import get_current_job
from multiprocessing import Pool, cpu_count
from worker import calculate_path
from worker3 import haversine            # <-- haversine for nearest mapping
from io import StringIO
import traceback
from solvers import highs_solver

# --- Redis connection ---------------------------------------------------------
REDIS_URL = os.environ.get("REDIS_URL")
# TLS URL (rediss://:key@host:6380/0) works automatically; disable cert checks if needed:
redis_conn = Redis.from_url(REDIS_URL)

def convert_numpy_types(obj):
    """Converts numpy types into Python native types for JSON serialization."""
    if isinstance(obj, np.integer):
        return int(obj)
    elif isinstance(obj, np.floating):
        return float(obj)
    elif isinstance(obj, np.ndarray):
        return obj.tolist()
    elif isinstance(obj, list):
        return [convert_numpy_types(i) for i in obj]
    elif isinstance(obj, dict):
        return {k: convert_numpy_types(v) for k, v in obj.items()}
    return obj


def _nearest_origins_by_haversine(origins_df: pd.DataFrame, fac_coords: np.ndarray) -> list[int]:
    """
    Map each uploaded facility coordinate to the nearest demand point index
    using haversine (great-circle) distance in km. Duplicates are de-duplicated
    with order preserved.
    """
    demand_coords = origins_df[["Latitude", "Longitude"]].to_numpy(dtype=float)
    nearest = []
    for (f_lat, f_lon) in fac_coords:  # (Latitude, Longitude)
        best_idx = None
        best_d = float("inf")
        for i, (o_lat, o_lon) in enumerate(demand_coords):
            d = haversine(f_lat, f_lon, o_lat, o_lon)  # km
            if d < best_d:
                best_d = d
                best_idx = i
        nearest.append(best_idx)

    # de-duplicate while preserving order
    seen = set()
    unique = []
    for i in nearest:
        if i not in seen:
            seen.add(i)
            unique.append(i)
    return unique


def _opened_indices_from_result(res, fac2cli, fallback_len: int | None = None) -> list[int]:
    """
    Try to recover the set of opened facility indices from spopt result.
    Prefer explicit attributes, fall back to fac2cli (non-empty assignment).
    """
    # 1) explicit open set
    if hasattr(res, "open_facilities") and res.open_facilities is not None:
        try:
            return sorted({int(i) for i in np.ravel(res.open_facilities)})
        except Exception:
            pass

    # 2) y variables
    if hasattr(res, "y") and res.y is not None:
        try:
            opened = []
            for j, yj in enumerate(res.y):
                val = getattr(yj, "value", lambda: yj)()
                if val is None:
                    val = 0.0
                if float(val) > 0.5:
                    opened.append(j)
            if opened:
                return sorted(set(opened))
        except Exception:
            pass

    # 3) fallback: any assigned clients
    opened = [j for j, cli in enumerate(fac2cli) if isinstance(cli, list) and len(cli) > 0]
    if opened:
        return opened

    # 4) as a last resort
    if fallback_len:
        return list(range(fallback_len))
    return []


def recommend_task4(
    selected_dropdown,
    P_FACILITIES,
    dm,
    uploaded_data_json,
    facilit,
    origins,
    wei,
    addresses,
    mode="pmedian"
):
    """
    EXPLORE with preloaded OD (dm) + user-uploaded facilities.

    - Clients:    demand points (origins rows)
    - Candidates: demand points themselves (OD columns)
    - Uploaded facilities mapped to nearest demand candidates and forced-open via
      predefined_facilities_arr (1=fixed open).
    - We then open **P_FACILITIES MORE** facilities in addition to the forced ones.

    Supports:
      - pmedian: efficiency objective (uses weights)
      - pcenter: equity objective (ignores weights), but still respects predefined facilities.
    """
    job = get_current_job()
    job_id = job.id

    try:
        # ---------- Inputs ----------
        origins_df = pd.DataFrame(origins)  # must contain Latitude, Longitude
        if not {"Latitude", "Longitude"}.issubset(origins_df.columns):
            raise KeyError("origins must include 'Latitude' and 'Longitude'")

        # Uploaded candidate rows
        destinations = pd.read_json(StringIO(uploaded_data_json))
        if isinstance(destinations, str):
            destinations = pd.DataFrame(json.loads(destinations))
        if not {"Latitude", "Longitude"}.issubset(destinations.columns):
            raise KeyError("Uploaded CSV must include 'Latitude' and 'Longitude' columns")

        # Facility flags for uploaded rows
        facilit_df = pd.read_json(StringIO(facilit))
        if isinstance(facilit_df, str):
            facilit_df = pd.DataFrame(json.loads(facilit_df))
        if "facility" not in facilit_df.columns:
            raise KeyError("Uploaded facilities file must include a 'facility' column (1=open, 0=candidate)")

        # Map uploaded coords to nearest demand points (HAVERSINE)
        uploaded_coords = destinations[["Latitude", "Longitude"]].to_numpy(dtype=float)
        mapped_indices = _nearest_origins_by_haversine(origins_df, uploaded_coords)

        # Candidate-level forced-open mask (length == #candidates == len(origins))
        n_candidates = len(origins_df)
        facility_mask = np.zeros(n_candidates, dtype=int)
        for idx_upl, row in facilit_df.iterrows():
            if idx_upl < len(mapped_indices):
                cand_idx = mapped_indices[idx_upl]
                if int(row.get("facility", 0)) == 1:
                    facility_mask[cand_idx] = 1

        num_existing = int(facility_mask.sum())
        P_FACILITIES = int(P_FACILITIES)

        # *** KEY CHANGE: open forced + P new (clamped to candidate count) ***
        p_to_open = num_existing + P_FACILITIES
        if p_to_open > n_candidates:
            p_to_open = n_candidates  # cannot open more than candidates

        # Weights vector for clients
        weights = pd.DataFrame(wei).to_numpy(dtype=float).ravel()

        # ---------- OD / cost matrix preprocessing ----------
        distance_matrix = pd.DataFrame(dm).astype(float)

        # Replace problematic zeros or 10000s (except diagonal) with the smallest nonzero
        val = distance_matrix.values
        pos_mask = val > 0
        min_nonzero = float(val[pos_mask].min()) if pos_mask.any() else 1.0

        replace_mask = ((distance_matrix == 0) | (distance_matrix == 10000))
        np.fill_diagonal(replace_mask.values, False)  # keep diagonal
        distance_matrix[replace_mask] = min_nonzero

        distance_matrix = distance_matrix.round(0)
        od_matrix = distance_matrix.to_numpy(dtype=float)
        od_matrix = np.where(np.isfinite(od_matrix), od_matrix, min_nonzero)

        # CO2 scaling
        COTWO_PER_KM = 0.15
        cost_matrix = np.where(od_matrix < 0, 0.0, od_matrix) * COTWO_PER_KM  # kg

        # ---------- Solve ----------
        solver = highs_solver()
        mode = (mode or "pmedian").lower()

        if mode == "pmedian":
            mdl = PMedian.from_cost_matrix(
                cost_matrix=cost_matrix,
                weights=weights,
                p_facilities=p_to_open,
                predefined_facilities_arr=facility_mask,
                name="p-median-explore-preloaded"
            )
            res = mdl.solve(solver)

            total_kg = float(res.problem.objective.value())
            total_km = total_kg / COTWO_PER_KM

            # Weighted mean distance (km)
            total_distance = 0.0
            total_weight = 0.0
            for fac, cli in enumerate(res.fac2cli):
                for i in cli:
                    w_i = float(weights[i])
                    total_distance += od_matrix[i, fac] * w_i
                    total_weight += w_i
            mean_km = float(total_distance / total_weight) if total_weight > 0 else 0.0
            mean_kg = mean_km * COTWO_PER_KM

            title = "P-median (efficiency)"
            metrics_html = (
                f"A total minimized weighted CO₂ emissions of {total_kg:.2f} Kg was observed. <br>"
                f"A total minimized weighted distance of {total_km:.2f} Km was observed. <br>"
                f"A mean kg/km CO₂ emissions of {mean_kg:.2f} was observed <br>"
                f"A mean distance of {mean_km:.2f} km was observed <br>"
            )

        elif mode == "pcenter":
            mdl = PCenter.from_cost_matrix(
                cost_matrix=cost_matrix,
                p_facilities=p_to_open,
                predefined_facilities_arr=facility_mask,
                name="p-center-explore-preloaded"
            )
            res = mdl.solve(solver)

            max_kg = float(res.problem.objective.value())
            max_km = max_kg / COTWO_PER_KM

            # Context mean (unweighted) of assigned costs
            assigned_costs = [cost_matrix[i, f] for f, cli in enumerate(res.fac2cli) for i in cli]
            assigned_costs = np.asarray(assigned_costs, dtype=float)
            mean_kg = float(assigned_costs.mean()) if assigned_costs.size else 0.0
            mean_km = mean_kg / COTWO_PER_KM

            title = "P-center (equity)"
            metrics_html = (
                f"Max (minimized) client CO₂ W = {max_kg:.2f} kg ({max_km:.2f} km).<br>"
                f"Mean CO₂ per client (for context) = {mean_kg:.2f} kg ({mean_km:.2f} km).<br>"
            )
        else:
            raise ValueError("mode must be 'pmedian' or 'pcenter'")

        # ---------- Reporting (include ALL opened facilities) ----------
        fac2cli = res.fac2cli
        opened_idx = _opened_indices_from_result(res, fac2cli, fallback_len=len(origins_df))
        opened_idx = sorted(set(opened_idx))

        facility_list = f"Please find your results below <br><b>{title}</b><br>" + metrics_html

        total_assigned = sum(len(cli) for cli in fac2cli if isinstance(cli, list))
        for fac in opened_idx:
            share = 0.0
            if fac < len(fac2cli) and isinstance(fac2cli[fac], list) and total_assigned > 0:
                share = (len(fac2cli[fac]) / total_assigned) * 100.0
            facility_list += f"Facility {fac} serving {share:.2f}% of customers; <br>"

        # Indices for uploaded fixed sites (blue markers)
        uploaded_fixed_indices = []
        for idx_upl, row in facilit_df.iterrows():
            if int(row.get("facility", 0)) == 1 and idx_upl < len(mapped_indices):
                uploaded_fixed_indices.append(mapped_indices[idx_upl])
        nearest_existing_indices = sorted(set(uploaded_fixed_indices))

        # ---------- Store ----------
        result_data = {
            "presult": facility_list,
            "addresses": addresses,                               # full candidate list with 'index'
            "facil": opened_idx,                                  # opened facility indices (forced + new)
            "nearest_origin_indexes": nearest_existing_indices,    # for blue markers
            "mode": mode
        }
        result_data = convert_numpy_types(result_data)
        redis_conn.set(f"result_data_for_job_{job_id}", json.dumps(result_data))

        return "Task complete"

    except Exception as e:
        error_message = f"Unexpected error: {str(e)}\n{traceback.format_exc()}"
        redis_conn.set(f"error_for_job_{job_id}", error_message)
        print(error_message)
        return "Task failed"
