# -*- coding: utf-8 -*-
"""
Created on Tue Aug 15 11:16:04 2023

@author: vpp
"""

from flask import Flask, render_template, session, redirect, url_for, session, request, jsonify
#from flask_sqlalchemy import SQLAlchemy
#from sqlalchemy import text
#from sqlalchemy import create_engine

import os
import pandas as pd
import numpy as np
import pulp
from spopt.locate import PMedian, PCenter
from solvers import highs_solver

import osmnx as ox
import networkx as nx
import pandas as pd
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
import gc
import traceback


REDIS_URL = os.environ.get("REDIS_URL")
redis_conn = Redis.from_url(REDIS_URL)


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
            od_df = pd.read_csv(dm_or_path, header=None, dtype=np.float32, low_memory=False)
    else:
        od_df = pd.DataFrame(dm_or_path)

    # Convert to numeric but do NOT silently hide broken data forever
    od_df = od_df.apply(pd.to_numeric, errors="coerce")

    if od_df.isna().any().any():
        bad_count = int(od_df.isna().sum().sum())
        raise ValueError(f"OD matrix contains {bad_count} non-numeric/NaN values")

    od_df = od_df.astype(np.float32, copy=False)
    return od_df


def recommend_task2(selected_dropdown, P_FACILITIES, dm_or_path, wei, addresses, mode="pmedian"):
    job = get_current_job()
    job_id = job.id

    try:
        od_df = _load_dm_from_any(dm_or_path)
        cost_matrix = od_df.to_numpy(dtype=np.float32, copy=False)

        w = pd.DataFrame(wei).to_numpy(dtype=np.float32, copy=False).ravel()

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

        if cost_matrix.shape[0] == 0 or cost_matrix.shape[1] == 0:
            raise ValueError(f"Empty cost matrix: {cost_matrix.shape}")

        if (cost_matrix < 0).any():
            np.maximum(cost_matrix, 0.0, out=cost_matrix)

        COTWO_PER_KM = np.float32(0.15)

        # single extra matrix copy only
        cm = cost_matrix.astype(np.float32, copy=True)
        cm *= COTWO_PER_KM

        mode = (mode or "pmedian").strip().lower()

        print(
            f"[tasknp2] selected={selected_dropdown}, shape={cm.shape}, "
            f"len(weights)={len(w)}, len(addresses)={len(addresses)}, "
            f"p={P_FACILITIES}, mode={mode}, "
            f"min={float(cm.min())}, max={float(cm.max())}",
            flush=True
        )

        solver = highs_solver()
        print(f"[tasknp2] solver={type(solver).__name__}", flush=True)

        if mode == "pmedian":
            mdl = PMedian.from_cost_matrix(
                cost_matrix=cm,
                weights=w,
                p_facilities=P_FACILITIES,
                name="p-median-network-distance",
            )

            try:
                res = mdl.solve(solver)
            except Exception as e:
                raise RuntimeError(
                    f"PMedian solve failed with solver={type(solver).__name__}, "
                    f"shape={cm.shape}, p={P_FACILITIES}: {e}"
                ) from e

            total_kg = float(res.problem.objective.value())
            total_km = total_kg / float(COTWO_PER_KM)
            mean_kg = float(res.mean_dist)
            mean_km = mean_kg / float(COTWO_PER_KM)

            fac2cli = res.fac2cli
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

            try:
                res = mdl.solve(solver)
            except Exception as e:
                raise RuntimeError(
                    f"PCenter solve failed with solver={type(solver).__name__}, "
                    f"shape={cm.shape}, p={P_FACILITIES}: {e}"
                ) from e

            max_kg = float(res.problem.objective.value())
            max_km = max_kg / float(COTWO_PER_KM)

            fac2cli = res.fac2cli
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

        del od_df, cost_matrix, cm, w
        gc.collect()

        return "Task complete"

    except Exception:
        error_message = traceback.format_exc()
        redis_conn.set(f"error_for_job_{job_id}", error_message)
        return "Task failed"
