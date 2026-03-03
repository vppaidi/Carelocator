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

    Production flow: dm is a PATH string (csv/parquet).
    Also supports legacy in-memory payloads.
    """
    if isinstance(dm, str):
        path = dm
        if not os.path.exists(path):
            raise FileNotFoundError(f"Precomputed OD file not found in worker: {os.path.abspath(path)}")

        if path.lower().endswith(".parquet"):
            df = pd.read_parquet(path)
        else:
            # headerless numeric OD grid
            df = pd.read_csv(path, header=None, dtype=np.float32, low_memory=False)

        # Ensure numeric float32
        df = df.apply(pd.to_numeric, errors="coerce").fillna(0.0).astype(np.float32, copy=False)
        return df

    if isinstance(dm, pd.DataFrame):
        return dm.apply(pd.to_numeric, errors="coerce").fillna(0.0).astype(np.float32, copy=False)

    df = pd.DataFrame(dm)
    df = df.apply(pd.to_numeric, errors="coerce").fillna(0.0).astype(np.float32, copy=False)
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
    - Candidate facilities are uploaded points; each is mapped to nearest origin index,
      and those OD columns are used as candidate cost columns.
    - 'facilit' may include a 'facility' column marking predefined/open candidates.
    - mode ∈ {'pmedian','pcenter'}.
    """
    job = get_current_job()
    job_id = job.id if job else "unknown"

    try:
        # ---------- Inputs & parsing ----------
        origins_df = pd.DataFrame(origins)

        destinations = pd.read_json(StringIO(uploaded_data_json))
        if isinstance(destinations, str):
            destinations = pd.DataFrame(json.loads(destinations))

        facilit_df = pd.read_json(StringIO(facilit))
        if isinstance(facilit_df, str):
            facilit_df = pd.DataFrame(json.loads(facilit_df))

        # Facility vector aligned with uploaded rows (candidates)
        if "facility" in facilit_df.columns:
            facilit_vec = (
                pd.to_numeric(facilit_df["facility"], errors="coerce")
                .fillna(0)
                .astype(int)
                .to_numpy()
            )
            nearest_origin_indexes = facilit_df[facilit_df["facility"] == 1].index.tolist()
        else:
            facilit_vec = np.zeros(len(destinations), dtype=int)
            nearest_origin_indexes = []

        # IMPORTANT: do NOT change user's P requirement; just add predefined count
        count_of_ones = int((facilit_vec == 1).sum())
        P_FACILITIES = int(P_FACILITIES) + count_of_ones

        # weights (clients,) float32 1D
        weights = np.ravel(pd.DataFrame(wei).to_numpy(dtype=np.float32)).astype(np.float32, copy=False)

        # ---------- Map uploaded candidates to OD columns ----------
        nearest_origins = []
        for _, dest_row in destinations.iterrows():
            min_distance = float("inf")
            nearest_origin_index = None

            dlat = float(dest_row["Latitude"])
            dlon = float(dest_row["Longitude"])

            for idx, orig_row in origins_df.iterrows():
                dist = haversine(
                    dlat, dlon,
                    float(orig_row["Latitude"]), float(orig_row["Longitude"])
                )
                if dist < min_distance:
                    min_distance = dist
                    nearest_origin_index = idx

            nearest_origins.append(nearest_origin_index)

        indices = [int(i) for i in nearest_origins if i is not None]
        if not indices:
            raise ValueError("No candidate-to-origin mapping could be computed (indices list is empty).")

        # ---------- Load OD matrix ----------
        distance_matrix = _load_dm(dm)

        # Keep your rounding convention (stays float32)
        distance_matrix = distance_matrix.round(0)

        n_cli = int(distance_matrix.shape[0])
        if len(weights) != n_cli:
            raise ValueError(
                f"OD/weights mismatch: OD has {n_cli} client rows but weights has {len(weights)}."
            )

        # ---------- Build cost matrix efficiently (avoid full NxN scaling) ----------
        cotwo = np.float32(0.15)  # kg per km

        # float32 numpy view (avoid copy if possible)
        od_matrix = distance_matrix.to_numpy(dtype=np.float32, copy=False)

        # Slice candidate columns first, then scale (saves huge memory)
        cost_matrix = od_matrix[:, indices].astype(np.float32, copy=False)
        cost_matrix *= cotwo

        # ---------- Solve ----------
        solver = highs_solver()
        mode = (mode or "pmedian").strip().lower()

        if mode == "pmedian":
            mdl = PMedian.from_cost_matrix(
                cost_matrix=cost_matrix,
                weights=weights,
                p_facilities=P_FACILITIES,
                predefined_facilities_arr=facilit_vec,
                name="p-median-exploit-preloaded",
            )
            res = mdl.solve(solver)

            total_kg = float(res.problem.objective.value())
            total_km = total_kg / float(cotwo)
            mean_kg = float(res.mean_dist)  # unweighted mean (per client) in kg units
            mean_km = mean_kg / float(cotwo)

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
                name="p-center-exploit-preloaded",
            )
            res = mdl.solve(solver)

            max_kg = float(res.problem.objective.value())
            max_km = max_kg / float(cotwo)

            assigned_costs = [cost_matrix[i, f] for f, cli in enumerate(res.fac2cli) for i in cli]
            assigned_costs = np.asarray(assigned_costs, dtype=np.float32)
            mean_kg = float(assigned_costs.mean()) if assigned_costs.size else 0.0
            mean_km = mean_kg / float(cotwo)

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
                facility_list += f"facility {fac} serving {per:.2f}% of customers; <br>"
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
            "mode": mode,
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
