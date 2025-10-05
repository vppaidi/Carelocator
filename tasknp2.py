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
import datetime;
import redis
import json
from flask_rq2 import RQ
from rq import Queue
from redis import Redis
from rq import get_current_job


REDIS_URL = os.environ.get("REDIS_URL")
# TLS URL (rediss://:key@host:6380/0) works automatically; disable cert checks if needed:
redis_conn = Redis.from_url(REDIS_URL)

def recommend_task2(selected_dropdown, P_FACILITIES, dm, wei, addresses, mode="pmedian"):
    job = get_current_job()
    job_id = job.id

    try:
        # ---------- inputs ----------
        # cost matrix in distance/time units (clients x candidates)
        od_df = pd.DataFrame(dm)
        cost_matrix = od_df.to_numpy(dtype=float)

        # need/pop weights (clients,)
        w = pd.DataFrame(wei).to_numpy(dtype=float).ravel()
        

        # clamp any tiny negative noise to 0
        if (cost_matrix < 0).any():
            cost_matrix = np.where(cost_matrix < 0, 0.0, cost_matrix)

        # optional CO2 scaling (if your matrix is in km and you want kg)
        COTWO_PER_KM = 0.15  # kg per km
        cm = cost_matrix * COTWO_PER_KM

        solver = highs_solver()  # <-- use HiGHS_CMD directly

        # ---------- solve ----------
        mode = (mode or "pmedian").lower()

        if mode == "pmedian":
            mdl = PMedian.from_cost_matrix(
                cost_matrix=cm,          # need-weighted objective via weights
                weights=w,
                p_facilities=P_FACILITIES,
                name="p-median-network-distance",
            )
            res = mdl.solve(solver)

            total_kg = float(res.problem.objective.value())     # sum_i w_i * assigned_cost_i
            total_km = total_kg / COTWO_PER_KM
            mean_kg  = float(res.mean_dist)                     # per client (unweighted)
            mean_km  = mean_kg / COTWO_PER_KM

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
                cost_matrix=cm,          # p-center ignores weights by definition
                p_facilities=P_FACILITIES,
                name="p-center-network-distance",
            )
            res = mdl.solve(solver)

            max_kg = float(res.problem.objective.value())       # minimized maximum client cost
            max_km = max_kg / COTWO_PER_KM

            fac2cli = res.fac2cli
            # mean for context only
            assigned_costs = [cm[i, f] for f, cli in enumerate(fac2cli) for i in cli]
            assigned_costs = np.asarray(assigned_costs, dtype=float)
            mean_kg = float(assigned_costs.mean()) if assigned_costs.size else 0.0
            mean_km = mean_kg / COTWO_PER_KM

            title = "P-center (equity)"
            metrics_html = (
                f"Max (minimized) client CO₂ W = {max_kg:.2f} kg ({max_km:.2f} km).<br>"
                f"Mean CO₂ per client (for context) = {mean_kg:.2f} kg "
                f"({mean_km:.2f} km).<br>"
            )

        else:
            raise ValueError("mode must be 'pmedian' or 'pcenter'")

        # ---------- reporting ----------
        facility_list = f"Please find your results below <br><b>{title}</b><br>" + metrics_html

        total_clients = sum(len(cli) for cli in fac2cli)
        opened_idx = []
        for fac, cli in enumerate(fac2cli):
            if len(cli):
                opened_idx.append(fac)
                share = (len(cli) / total_clients * 100.0) if total_clients else 0.0
                facility_list += f"facility {fac} serving {share:.2f}% of customers; <br>"

        presult = facility_list
        facil = opened_idx

        # if `addresses` is a list of candidate dicts with lat/lon, filter opened ones
        addresses2 = []
        try:
            for idx in facil:
                if 0 <= idx < len(addresses):
                    a = dict(addresses[idx])
                    a["idx"] = idx
                    addresses2.append(a)
        except Exception:
            addresses2 = []

        # ---------- return/store ----------
        result_data = {
            "presult": presult,
            "facil": facil,
            "addresses": addresses,          # original list
            "addresses2": addresses2,        # opened facilities only
            "nearest_origin_indexes": [],    # not used here
            "mode": mode
        }

        redis_conn.set(f"result_data_for_job_{job_id}", json.dumps(result_data))
        return "Task complete"

    except RuntimeError:
        error_message = "Runtime Error: Please check your input"
        redis_conn.set(f"error_for_job_{job_id}", error_message)
        return "Task failed"

    except Exception as e:
        error_message = f"Unexpected error: {str(e)}"
        redis_conn.set(f"error_for_job_{job_id}", error_message)
        return "Task failed"