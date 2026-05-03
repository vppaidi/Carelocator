# -*- coding: utf-8 -*-

import os
import gc
import json
import traceback
import resource

import numpy as np
import pandas as pd
from redis import Redis
from rq import get_current_job
from spopt.locate import PMedian, PCenter

from solvers import highs_solver


REDIS_URL = os.environ.get("REDIS_URL")
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

def _convert_numpy_types(obj):
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, list):
        return [_convert_numpy_types(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _convert_numpy_types(v) for k, v in obj.items()}
    return obj


def _rss_mb():
    """
    Max resident set size in MB.
    On Linux, ru_maxrss is in KB.
    """
    try:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except Exception:
        return -1.0


def _load_dm_from_any(dm_or_path):
    """
    Accept either:
    - path to parquet/csv
    - legacy in-memory payload
    """
    if isinstance(dm_or_path, str):
        if not os.path.exists(dm_or_path):
            raise FileNotFoundError(f"Precomputed OD file not found: {dm_or_path}")

        if dm_or_path.lower().endswith(".parquet"):
            od_df = pd.read_parquet(dm_or_path)
        else:
            od_df = pd.read_csv(
                dm_or_path,
                header=None,
                dtype=np.float32,
                low_memory=False
            )
    else:
        od_df = pd.DataFrame(dm_or_path)

    od_df = od_df.apply(pd.to_numeric, errors="coerce")

    if od_df.isna().any().any():
        bad_count = int(od_df.isna().sum().sum())
        raise ValueError(f"OD matrix contains {bad_count} non-numeric/NaN values")

    return od_df.astype(np.float32, copy=False)


def recommend_task2(selected_dropdown, P_FACILITIES, dm_or_path, wei, addresses, mode="pmedian"):
    job = get_current_job()
    job_id = job.id

    try:
        print(f"[tasknp2] RSS start: {_rss_mb():.1f} MB", flush=True)

        # ----- Load OD matrix -----
        od_df = _load_dm_from_any(dm_or_path)
        cost_matrix = od_df.to_numpy(dtype=np.float32, copy=False)

        # weights
        w = pd.DataFrame(wei).to_numpy(dtype=np.float32, copy=False).ravel()

        # We do not need the DataFrame anymore
        del od_df
        gc.collect()

        if cost_matrix.ndim != 2:
            raise ValueError(f"OD matrix must be 2D, got shape {cost_matrix.shape}")

        n_clients, n_candidates = cost_matrix.shape

        if len(w) != n_clients:
            raise ValueError(
                f"Weight/OD mismatch: len(weights)={len(w)} but OD rows={n_clients}"
            )

        if len(addresses) != n_candidates:
            raise ValueError(
                f"Address/OD mismatch: len(addresses)={len(addresses)} but OD cols={n_candidates}"
            )

        P_FACILITIES = int(P_FACILITIES)
        if not (1 <= P_FACILITIES <= n_candidates):
            raise ValueError(
                f"P_FACILITIES must be between 1 and {n_candidates}, got {P_FACILITIES}"
            )

        if not np.isfinite(cost_matrix).all():
            raise ValueError("OD matrix contains NaN or inf")

        if not np.isfinite(w).all():
            raise ValueError("Weights contain NaN or inf")

        if n_clients == 0 or n_candidates == 0:
            raise ValueError(f"Empty cost matrix: {cost_matrix.shape}")

        # Clean negatives in place
        if (cost_matrix < 0).any():
            np.maximum(cost_matrix, 0.0, out=cost_matrix)

        mode = (mode or "pmedian").strip().lower()
        COTWO_PER_KM = np.float32(0.15)

        print(
            f"[tasknp2] selected={selected_dropdown}, shape={cost_matrix.shape}, "
            f"len(weights)={len(w)}, len(addresses)={len(addresses)}, "
            f"p={P_FACILITIES}, mode={mode}, "
            f"min={float(cost_matrix.min())}, max={float(cost_matrix.max())}, "
            f"RSS before scale={_rss_mb():.1f} MB",
            flush=True
        )

        # Scale in place to avoid another large copy
        cost_matrix *= COTWO_PER_KM
        cm = cost_matrix

        print(f"[tasknp2] RSS after scale: {_rss_mb():.1f} MB", flush=True)

        solver = highs_solver()
        print(f"[tasknp2] solver={type(solver).__name__}", flush=True)

        if mode == "pmedian":
            mdl = PMedian.from_cost_matrix(
                cost_matrix=cm,
                weights=w,
                p_facilities=P_FACILITIES,
                name="p-median-network-distance",
            )

            print(f"[tasknp2] RSS before PMedian solve: {_rss_mb():.1f} MB", flush=True)

            try:
                res = mdl.solve(solver)
            except Exception as e:
                raise RuntimeError(
                    f"PMedian solve failed with solver={type(solver).__name__}, "
                    f"shape={cm.shape}, p={P_FACILITIES}: {e}"
                ) from e

            print(f"[tasknp2] RSS after PMedian solve: {_rss_mb():.1f} MB", flush=True)

            total_kg = float(res.problem.objective.value())
            total_km = total_kg / float(COTWO_PER_KM)
            mean_kg = float(res.mean_dist)
            mean_km = mean_kg / float(COTWO_PER_KM)

            fac2cli = res.fac2cli
            distance_matrix_km = cm / np.float32(0.15)

            _write_facility_distance_csv(
                job_id=job_id,
                fac2cli=fac2cli,
                distance_matrix_km=distance_matrix_km,
                weights=w,
                facility_coords=addresses,
                population_coords=addresses,
                facility_index_map=None
            )
            title = "P-median (efficiency)"
            metrics_html = (
                f"A total minimized need-weighted CO₂ of {total_kg:.2f} kg "
                f"({total_km:.2f} km) was observed.<br>"
                f"A mean CO₂ per client of {mean_kg:.2f} kg "
                f"({mean_km:.2f} km) was observed.<br>"
            )

        elif mode == "pcenter":
            mdl = PCenter.from_cost_matrix(
                cost_matrix=cm,
                p_facilities=P_FACILITIES,
                name="p-center-network-distance",
            )

            print(f"[tasknp2] RSS before PCenter solve: {_rss_mb():.1f} MB", flush=True)

            try:
                res = mdl.solve(solver)
            except Exception as e:
                raise RuntimeError(
                    f"PCenter solve failed with solver={type(solver).__name__}, "
                    f"shape={cm.shape}, p={P_FACILITIES}: {e}"
                ) from e

            print(f"[tasknp2] RSS after PCenter solve: {_rss_mb():.1f} MB", flush=True)

            max_kg = float(res.problem.objective.value())
            max_km = max_kg / float(COTWO_PER_KM)

            fac2cli = res.fac2cli
            distance_matrix_km = cm / np.float32(0.15)

            _write_facility_distance_csv(
                job_id=job_id,
                fac2cli=fac2cli,
                distance_matrix_km=distance_matrix_km,
                weights=w,
                facility_coords=addresses,
                population_coords=addresses,
                facility_index_map=None
            )
            assigned_costs = [cm[i, f] for f, cli in enumerate(fac2cli) for i in cli]
            assigned_costs = np.asarray(assigned_costs, dtype=np.float32)
            mean_kg = float(assigned_costs.mean()) if assigned_costs.size else 0.0
            mean_km = mean_kg / float(COTWO_PER_KM)

            title = "P-center (equity)"
            metrics_html = (
                f"Max (minimized) client CO₂ W = {max_kg:.2f} kg ({max_km:.2f} km).<br>"
                f"Mean CO₂ per client (for context) = {mean_kg:.2f} kg "
                f"({mean_km:.2f} km).<br>"
            )
        else:
            raise ValueError("mode must be 'pmedian' or 'pcenter'")

        facility_list = f"Please find your results below <br><b>{title}</b><br>" + metrics_html

        total_clients = sum(len(cli) for cli in fac2cli)
        facil = []

        for fac, cli in enumerate(fac2cli):
            if len(cli) > 0:
                facil.append(fac)
                share = (len(cli) / total_clients * 100.0) if total_clients else 0.0
                facility_list += f"facility {fac} serving {share:.2f}% of customers; <br>"

        result_data = {
            "presult": facility_list,
            "facil": facil,
            "addresses": addresses,
            "nearest_origin_indexes": [],
            "mode": mode,
        }

        result_data = _convert_numpy_types(result_data)
        redis_conn.set(f"result_data_for_job_{job_id}", json.dumps(result_data))

        # cleanup
        del cm, cost_matrix, w
        gc.collect()

        print(f"[tasknp2] RSS end: {_rss_mb():.1f} MB", flush=True)
        return "Task complete"

    except Exception:
        error_message = traceback.format_exc()
        redis_conn.set(f"error_for_job_{job_id}", error_message)
        raise
