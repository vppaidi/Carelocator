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
import gc

# --- Redis connection ---------------------------------------------------------
REDIS_URL = os.environ.get("REDIS_URL")
# TLS URL (rediss://:key@host:6380/0) works automatically; disable cert checks if needed:
redis_conn = Redis.from_url(REDIS_URL)
def _write_facility_distance_csv(job_id, fac2cli, distance_matrix_km, weights=None,
                                 facility_coords=None, population_coords=None,
                                 facility_index_map=None):
    """
    Write one row per assigned population point.

    This avoids storing large allocation tables in Redis or HTML.
    """
    import os
    import csv
    import numpy as np

    out_dir = os.path.join("static", "results")
    os.makedirs(out_dir, exist_ok=True)

    out_path = os.path.join(out_dir, f"facility_distances_{job_id}.csv")

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        writer.writerow([
            "facility_solver_index",
            "facility_actual_index",
            "facility_lat",
            "facility_lon",
            "population_index",
            "population_lat",
            "population_lon",
            "distance_km",
            "weight",
            "co2_kg"
        ])

        for fac_solver_idx, cli in enumerate(fac2cli):
            if not cli:
                continue

            actual_fac_idx = (
                facility_index_map[fac_solver_idx]
                if facility_index_map is not None and fac_solver_idx < len(facility_index_map)
                else fac_solver_idx
            )

            fac_lat = ""
            fac_lon = ""
            if facility_coords is not None and actual_fac_idx < len(facility_coords):
                fac_lat = facility_coords[actual_fac_idx].get("lat", facility_coords[actual_fac_idx].get("Latitude", ""))
                fac_lon = facility_coords[actual_fac_idx].get("lon", facility_coords[actual_fac_idx].get("Longitude", ""))

            for pop_idx in cli:
                pop_idx = int(pop_idx)

                dist_km = float(distance_matrix_km[pop_idx, fac_solver_idx])
                weight = float(weights[pop_idx]) if weights is not None else 1.0
                co2_kg = dist_km * 0.15

                pop_lat = ""
                pop_lon = ""
                if population_coords is not None and pop_idx < len(population_coords):
                    pop_lat = population_coords[pop_idx].get("lat", population_coords[pop_idx].get("Latitude", ""))
                    pop_lon = population_coords[pop_idx].get("lon", population_coords[pop_idx].get("Longitude", ""))

                writer.writerow([
                    fac_solver_idx,
                    actual_fac_idx,
                    fac_lat,
                    fac_lon,
                    pop_idx,
                    pop_lat,
                    pop_lon,
                    round(dist_km, 4),
                    round(weight, 4),
                    round(co2_kg, 6)
                ])

    redis_conn.set(f"distance_csv_for_job_{job_id}", out_path)
    return out_path

def convert_numpy_types(obj):
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


def _load_dm_from_any(dm_or_path):
    if isinstance(dm_or_path, str):
        if not os.path.exists(dm_or_path):
            raise FileNotFoundError(f"Precomputed OD file not found: {dm_or_path}")

        if dm_or_path.lower().endswith(".parquet"):
            od_df = pd.read_parquet(dm_or_path)
        else:
            od_df = pd.read_csv(dm_or_path, header=None, dtype=np.float32, low_memory=False)
    else:
        od_df = pd.DataFrame(dm_or_path)

    od_df = od_df.apply(pd.to_numeric, errors="coerce").fillna(0.0).astype(np.float32, copy=False)
    return od_df


def _nearest_origins_by_haversine(origins_df: pd.DataFrame, fac_coords: np.ndarray) -> list[int]:
    demand_coords = origins_df[["Latitude", "Longitude"]].to_numpy(dtype=float)
    nearest = []

    for (f_lat, f_lon) in fac_coords:
        best_idx = None
        best_d = float("inf")
        for i, (o_lat, o_lon) in enumerate(demand_coords):
            d = haversine(f_lat, f_lon, o_lat, o_lon)
            if d < best_d:
                best_d = d
                best_idx = i
        nearest.append(best_idx)

    seen = set()
    unique = []
    for i in nearest:
        if i not in seen:
            seen.add(i)
            unique.append(i)
    return unique


def _opened_indices_from_result(res, fac2cli, fallback_len: int | None = None) -> list[int]:
    if hasattr(res, "open_facilities") and res.open_facilities is not None:
        try:
            return sorted({int(i) for i in np.ravel(res.open_facilities)})
        except Exception:
            pass

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

    opened = [j for j, cli in enumerate(fac2cli) if isinstance(cli, list) and len(cli) > 0]
    if opened:
        return opened

    if fallback_len:
        return list(range(fallback_len))
    return []


def recommend_task4(
    selected_dropdown,
    P_FACILITIES,
    dm_or_path,
    uploaded_data_json,
    facilit,
    origins,
    wei,
    addresses,
    mode="pmedian"
):
    job = get_current_job()
    job_id = job.id

    try:
        origins_df = pd.DataFrame(origins)
        if not {"Latitude", "Longitude"}.issubset(origins_df.columns):
            raise KeyError("origins must include 'Latitude' and 'Longitude'")

        destinations = pd.read_json(StringIO(uploaded_data_json))
        if isinstance(destinations, str):
            destinations = pd.DataFrame(json.loads(destinations))
        if not {"Latitude", "Longitude"}.issubset(destinations.columns):
            raise KeyError("Uploaded CSV must include 'Latitude' and 'Longitude' columns")

        facilit_df = pd.read_json(StringIO(facilit))
        if isinstance(facilit_df, str):
            facilit_df = pd.DataFrame(json.loads(facilit_df))
        if "facility" not in facilit_df.columns:
            raise KeyError("Uploaded facilities file must include a 'facility' column (1=open, 0=candidate)")

        uploaded_coords = destinations[["Latitude", "Longitude"]].to_numpy(dtype=float)
        mapped_indices = _nearest_origins_by_haversine(origins_df, uploaded_coords)

        od_df = _load_dm_from_any(dm_or_path)
        od_matrix = od_df.to_numpy(dtype=np.float32, copy=False)

        if od_matrix.ndim != 2:
            raise ValueError(f"OD matrix must be 2D, got shape {od_matrix.shape}")

        n_clients, n_candidates = od_matrix.shape

        weights = pd.DataFrame(wei).to_numpy(dtype=np.float32, copy=False).ravel()

        if len(weights) != n_clients:
            raise ValueError(
                f"Weight/OD mismatch: len(weights)={len(weights)} but OD rows={n_clients}"
            )

        if len(origins_df) != n_candidates:
            raise ValueError(
                f"Candidate/OD mismatch: len(origins)={len(origins_df)} but OD cols={n_candidates}"
            )

        facility_mask = np.zeros(n_candidates, dtype=int)
        for idx_upl, row in facilit_df.iterrows():
            if idx_upl < len(mapped_indices):
                cand_idx = mapped_indices[idx_upl]
                if 0 <= cand_idx < n_candidates and int(row.get("facility", 0)) == 1:
                    facility_mask[cand_idx] = 1

        num_existing = int(facility_mask.sum())
        P_FACILITIES = int(P_FACILITIES)
        p_to_open = min(num_existing + P_FACILITIES, n_candidates)

        pos_mask = od_matrix > 0
        min_nonzero = float(od_matrix[pos_mask].min()) if pos_mask.any() else 1.0

        replace_mask = ((od_matrix == 0) | (od_matrix == 10000))
        if od_matrix.shape[0] == od_matrix.shape[1]:
            np.fill_diagonal(replace_mask, False)

        od_matrix = od_matrix.copy()
        od_matrix[replace_mask] = min_nonzero
        od_matrix = np.where(np.isfinite(od_matrix), od_matrix, min_nonzero)
        od_matrix = np.where(od_matrix < 0, 0.0, od_matrix).astype(np.float32, copy=False)

        COTWO_PER_KM = np.float32(0.15)
        cost_matrix = od_matrix.copy()
        cost_matrix *= COTWO_PER_KM

        solver = highs_solver()
        mode = (mode or "pmedian").strip().lower()

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
            total_km = total_kg / float(COTWO_PER_KM)

            total_distance = 0.0
            total_weight = 0.0
            
            for fac, cli in enumerate(res.fac2cli):
                for i in cli:
                    w_i = float(weights[i])
                    total_distance += float(od_matrix[i, fac]) * w_i
                    total_weight += w_i

            mean_km = float(total_distance / total_weight) if total_weight > 0 else 0.0
            mean_kg = mean_km * float(COTWO_PER_KM)

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
            max_km = max_kg / float(COTWO_PER_KM)

            assigned_costs = [cost_matrix[i, f] for f, cli in enumerate(res.fac2cli) for i in cli]
            assigned_costs = np.asarray(assigned_costs, dtype=np.float32)
            mean_kg = float(assigned_costs.mean()) if assigned_costs.size else 0.0
            mean_km = mean_kg / float(COTWO_PER_KM)

            title = "P-center (equity)"
            metrics_html = (
                f"Max (minimized) client CO₂ W = {max_kg:.2f} kg ({max_km:.2f} km).<br>"
                f"Mean CO₂ per client (for context) = {mean_kg:.2f} kg ({mean_km:.2f} km).<br>"
            )
        else:
            raise ValueError("mode must be 'pmedian' or 'pcenter'")

        fac2cli = res.fac2cli
        _write_facility_distance_csv(
            job_id=job_id,
            fac2cli=fac2cli,
            distance_matrix_km=od_matrix,
            weights=weights,
            facility_coords=addresses,
            population_coords=addresses,
            facility_index_map=None
        )
        opened_idx = _opened_indices_from_result(res, fac2cli, fallback_len=len(origins_df))
        opened_idx = sorted(set(opened_idx))

        facility_list = f"Please find your results below <br><b>{title}</b><br>" + metrics_html

        total_assigned = sum(len(cli) for cli in fac2cli if isinstance(cli, list))
        for fac in opened_idx:
            share = 0.0
            if fac < len(fac2cli) and isinstance(fac2cli[fac], list) and total_assigned > 0:
                share = (len(fac2cli[fac]) / total_assigned) * 100.0
            facility_list += f"Facility {fac} serving {share:.2f}% of customers; <br>"

        uploaded_fixed_indices = []
        for idx_upl, row in facilit_df.iterrows():
            if int(row.get("facility", 0)) == 1 and idx_upl < len(mapped_indices):
                uploaded_fixed_indices.append(mapped_indices[idx_upl])
        nearest_existing_indices = sorted(set(uploaded_fixed_indices))

        result_data = {
            "presult": facility_list,
            "addresses": addresses,
            "facil": opened_idx,
            "nearest_origin_indexes": nearest_existing_indices,
            "mode": mode
        }

        result_data = convert_numpy_types(result_data)
        redis_conn.set(f"result_data_for_job_{job_id}", json.dumps(result_data))

        del od_df, od_matrix, cost_matrix, weights
        gc.collect()

        return "Task complete"

    except Exception:
        error_message = traceback.format_exc()
        redis_conn.set(f"error_for_job_{job_id}", error_message)
        return "Task failed"
