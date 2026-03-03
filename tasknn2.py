# -*- coding: utf-8 -*-
"""
Created on Tue Aug 15 11:16:04 2023

@author: vpp
"""

import os
import json
import traceback
from io import StringIO

import numpy as np
import pandas as pd

from solvers import highs_solver
from spopt.locate import PMedian, PCenter

from redis import Redis
from rq import get_current_job

from worker3 import haversine


REDIS_URL = os.environ.get("REDIS_URL")
redis_conn = Redis.from_url(REDIS_URL)


def _load_dm(dm) -> pd.DataFrame:
    """
    Load the precomputed OD matrix.

    In production (DSS_prod.py), pfac_task2 receives dm as a PATH string (csv/parquet).
    We support:
      - dm as a path: "*.parquet" or "*.csv"
      - dm as an in-memory object: list-of-records, list-of-lists, numpy array, etc.
    """
    # Path case (production)
    if isinstance(dm, str):
        path = dm
        if not os.path.exists(path):
            raise FileNotFoundError(f"Precomputed OD file not found in worker: {os.path.abspath(path)}")

        if path.lower().endswith(".parquet"):
            df = pd.read_parquet(path)
        else:
            # CSV: OD matrices in your app are typically headerless numeric grids
            df = pd.read_csv(path, header=None, dtype=np.float32, low_memory=False)

        # Ensure numeric
        df = df.apply(pd.to_numeric, errors="coerce").fillna(0.0)
        return df

    # DataFrame case
    if isinstance(dm, pd.DataFrame):
        return dm

    # In-memory case (records / array-like)
    df = pd.DataFrame(dm)
    df = df.apply(pd.to_numeric, errors="coerce").fillna(0.0)
    return df


def pfac_task2(
    selected_dropdown,
    P_FACILITIES,
    uploaded_data_json,
    facilit,
    dm,
    origins,
    wei,
    addresses,
    mode="pmedian"
):
    """
    EXPLOIT with preloaded OD matrix:
    - Clients are rows of OD (existing demand points).
    - Candidate facilities are the uploaded points. We map each uploaded point
      to the nearest 'origin' index, and take those OD columns as the
      candidate cost columns.
    - 'facilit' marks which uploaded points are predefined/open (1) vs optional (0),
      if a 'facility' column exists in that uploaded file.
    - mode ∈ {'pmedian','pcenter'}.
    """
    job = get_current_job()
    job_id = job.id if job else "unknown"

    try:
        # ---------- Inputs & parsing ----------
        origins_df = pd.DataFrame(origins)

        # Uploaded destinations (candidate points)
        destinations = pd.read_json(StringIO(uploaded_data_json))
        if isinstance(destinations, str):
            destinations = pd.DataFrame(json.loads(destinations))

        # Uploaded CSV content (may or may not contain 'facility' column)
        facilit_df = pd.read_json(StringIO(facilit))
        if isinstance(facilit_df, str):
            facilit_df = pd.DataFrame(json.loads(facilit_df))

        # Facility vector aligned with uploaded rows (candidates)
        # Default: no predefined facilities
        if 'facility' in facilit_df.columns:
            facilit_vec = pd.to_numeric(facilit_df['facility'], errors='coerce').fillna(0).astype(int).to_numpy()
            nearest_origin_indexes = facilit_df[facilit_df['facility'] == 1].index.tolist()
        else:
            facilit_vec = np.zeros(len(destinations), dtype=int)
            nearest_origin_indexes = []

        # If some are pre-open, add them to P
        count_of_ones = int((facilit_vec == 1).sum())
        P_FACILITIES = int(P_FACILITIES) + count_of_ones

        # weights (clients,) as numpy 1D
        weights = pd.DataFrame(wei).to_numpy(dtype=float)
        weights = np.ravel(weights).astype(float, copy=False)

        # ---------- Map uploaded candidates to OD columns ----------
        # For each uploaded candidate, find nearest origin index
        nearest_origins = []
        for _, dest_row in destinations.iterrows():
            min_distance = float('inf')
            nearest_origin_index = None

            for idx, orig_row in origins_df.iterrows():
                dist = haversine(
                    float(dest_row['Latitude']), float(dest_row['Longitude']),
                    float(orig_row['Latitude']), float(orig_row['Longitude'])
                )
                if dist < min_distance:
                    min_distance = dist
                    nearest_origin_index = idx

            nearest_origins.append(nearest_origin_index)

        # ---------- Load OD matrix ----------
        distance_matrix = _load_dm(dm)

        # Keep existing convention: round to integers
        distance_matrix = distance_matrix.round(decimals=0)

        od_matrix = distance_matrix.to_numpy(dtype=float)

        # CO2 scaling
        cotwo = 0.15  # kg per km
        cm = od_matrix * cotwo

        # Candidate columns = mapped origin indices
        indices = [int(i) for i in nearest_origins if i is not None]
        if not indices:
            raise ValueError("No candidate-to-origin mapping could be computed (indices list is empty).")

        cost_matrix = cm[:, indices]  # shape: (n_clients, n_candidates)

        # ---------- Solve ----------
        solver = highs_solver()
        mode = (mode or "pmedian").strip().lower()

        if mode == "pmedian":
            mdl = PMedian.from_cost_matrix(
                cost_matrix=cost_matrix,
                weights=weights,
                p_facilities=P_FACILITIES,
                predefined_facilities_arr=facilit_vec,
                name="p-median-exploit-preloaded"
            )
            res = mdl.solve(solver)

            total_kg = float(res.problem.objective.value())
            total_km = total_kg / cotwo
            mean_kg = float(res.mean_dist)   # unweighted mean (per client) in kg
            mean_km = mean_kg / cotwo

            title = "P-median (efficiency)"
            metrics_html = (
                f"A total minimized weighted CO2 of {total_kg:.2f} Kg was observed.<br>"
                f"A total minimized weighted distance of {total_km:.2f} Km was observed.<br>"
                f"A mean kg/km CO2 emissions of {mean_kg:.2f} was observed<br>"
                f"A mean distance of {mean_km:.2f} km was observed<br>"
            )
            fac2cli = res.fac2cli

        elif mode == "pcenter":
            mdl = PCenter.from_cost_matrix(
                cost_matrix=cost_matrix,
                p_facilities=P_FACILITIES,
                predefined_facilities_arr=facilit_vec,
                name="p-center-exploit-preloaded"
            )
            res = mdl.solve(solver)

            max_kg = float(res.problem.objective.value())
            max_km = max_kg / cotwo

            # contextual mean (unweighted) of assigned costs
            assigned_costs = [cost_matrix[i, f] for f, cli in enumerate(res.fac2cli) for i in cli]
            assigned_costs = np.asarray(assigned_costs, dtype=float)
            mean_kg = float(assigned_costs.mean()) if assigned_costs.size else 0.0
            mean_km = mean_kg / cotwo

            title = "P-center (equity)"
            metrics_html = (
                f"Max (minimized) client CO2 W = {max_kg:.2f} Kg ({max_km:.2f} Km).<br>"
                f"Mean CO2 per client (context) = {mean_kg:.2f} Kg ({mean_km:.2f} Km).<br>"
            )
            fac2cli = res.fac2cli

        else:
            raise ValueError("mode must be 'pmedian' or 'pcenter'")

        # ---------- Reporting (WEIGHT-based shares) ----------
        facility_list = "Please find your results below <br>" + f"<b>{title}</b><br>" + metrics_html

        total_w = float(np.sum(weights)) if weights is not None else 0.0
        facil = []

        for fac, cli in enumerate(fac2cli):
            if len(cli) != 0:
                cli_idx = np.asarray(cli, dtype=int)
                served_w = float(np.sum(weights[cli_idx])) if (total_w > 0 and cli_idx.size) else 0.0
                per = (served_w / total_w * 100.0) if total_w > 0 else 0.0
                per = round(per, 2)
                facility_list += f"facility {fac} serving {per}% of customers; <br>"
                facil.append(fac)

        presult = facility_list

        # Filter passed-in addresses to opened facilities (by index)
        addresses2 = []
        for idx, address in enumerate(addresses):
            if idx in facil:
                a = dict(address)
                a["idx"] = idx
                addresses2.append(a)

        # ---------- Store result ----------
        result_data = {
            "presult": presult,
            "addresses2": addresses2,
            "facil": facil,
            "nearest_origin_indexes": nearest_origin_indexes,
            "mode": mode
        }

        redis_conn.set(f"result_data_for_job_{job_id}", json.dumps(result_data))
        return "Task complete"

    except RuntimeError:
        error_message = "Runtime Error: Please check your input"
        redis_conn.set(f"error_for_job_{job_id}", error_message)
        return "Task failed"

    except Exception:
        error_message = traceback.format_exc()
        redis_conn.set(f"error_for_job_{job_id}", error_message)
        print(error_message)
        return "Task failed"
