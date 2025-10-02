# -*- coding: utf-8 -*-
"""
Created on Tue Aug 15 11:16:04 2023

@author: vpp
"""

from flask import Flask, render_template, session, redirect, url_for, session, request, jsonify
# from flask_sqlalchemy import SQLAlchemy
# from sqlalchemy import text
# from sqlalchemy import create_engine
import rq
import os
import pandas as pd
import numpy as np
import pulp
from pulp import HiGHS_CMD
from spopt.locate import PMedian, PCenter  # <-- add PCenter
# redis cloud loading
from dotenv import load_dotenv
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
from worker3 import haversine
from io import StringIO

# Load environment variables from .env file
# load_dotenv()

REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
REDIS_DB = int(os.getenv('REDIS_DB', 0))
REDIS_PASSWORD = os.getenv('REDIS_PASSWORD', None)
REDIS_SSL = os.getenv('REDIS_SSL', 'False') == 'True'

redis_conn = redis.StrictRedis(
    host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB,
    password=REDIS_PASSWORD, ssl=REDIS_SSL
)

def pfac_task2(
    selected_dropdown,
    P_FACILITIES,
    uploaded_data_json,
    facilit,
    dm,
    origins,
    wei,
    addresses,
    mode="pmedian"  # <-- new parameter
):
    """
    EXPLOIT with preloaded OD matrix:
    - Clients are rows of OD (existing demand points).
    - Candidate facilities are the uploaded points. We map each uploaded point
      to the nearest 'origin' index, and take those OD columns as the
      candidate cost columns. 'facilit' marks which uploaded points are
      predefined/open (1) vs optional (0).
    - Now supports mode ∈ {'pmedian','pcenter'}.
    """
    job = get_current_job()
    job_id = job.id

    try:
        # ---------- Inputs & parsing ----------
        selected_option = selected_dropdown

        origins = pd.DataFrame(origins)

        # Uploaded destinations (candidate points)
        destinations = pd.read_json(StringIO(uploaded_data_json))
        if isinstance(destinations, str):
            destinations = pd.DataFrame(json.loads(destinations))

        facilit_df = pd.read_json(StringIO(facilit))
        if isinstance(facilit_df, str):
            facilit_df = pd.DataFrame(json.loads(facilit_df))

        # Indices of uploaded rows that are flagged as existing facilities (=1)
        nearest_origin_indexes = facilit_df[facilit_df['facility'] == 1].index.tolist()

        # Facility vector (0/1) aligned with uploaded rows (candidates)
        facilit_vec = facilit_df["facility"].to_numpy()

        # If some are pre-open, add them to P
        count_of_ones = int((facilit_vec == 1).sum())
        P_FACILITIES = int(P_FACILITIES) + count_of_ones

        # weights (clients,) as numpy
        weights = pd.DataFrame(wei).to_numpy()
    
        
        # Ensure shape (n_clients,)
        weights = np.ravel(weights).astype(float, copy=False)
        print(weights)

        # ---------- Build mapping from uploaded candidates to OD columns ----------
        # For each uploaded candidate, find the nearest origin index
        nearest_origins = []
        for _, dest_row in destinations.iterrows():
            min_distance = float('inf')
            nearest_origin_index = None
            for idx, orig_row in origins.iterrows():
                dist = haversine(
                    dest_row['Latitude'], dest_row['Longitude'],
                    orig_row['Latitude'], orig_row['Longitude']
                )
                if dist < min_distance:
                    min_distance = dist
                    nearest_origin_index = idx
            nearest_origins.append((nearest_origin_index, None))

        # ---------- Cost matrix from preloaded OD (dm) ----------
        distance_matrix = pd.DataFrame(dm)

        # Keep your existing convention: replace 0 by a very large number
        # (important for avoiding self-assignment when rows=cols share indices)
        # distance_matrix[distance_matrix == 0] = 100000
        distance_matrix = distance_matrix.round(decimals=0)

        od_matrix = distance_matrix.to_numpy(dtype=float)

        # CO2 scaling
        cotwo = 0.15  # kg per km
        cm = od_matrix * cotwo

        # Candidate columns = mapped origin indices
        indices = [i[0] for i in nearest_origins]
        cost_matrix = cm[:, indices]  # shape: (n_clients, n_candidates)

        # ---------- Solve ----------
        solver = HiGHS_CMD(msg=False)
        mode = (mode or "pmedian").strip().lower()

        if mode == "pmedian":
            mdl = PMedian.from_cost_matrix(
                cost_matrix=cost_matrix,
                weights=weights,
                p_facilities=P_FACILITIES,
                predefined_facilities_arr=facilit_vec,  # lock uploaded 1's open
                name="p-median-exploit-preloaded"
            )
            res = mdl.solve(solver)

            total_kg = float(res.problem.objective.value())
            total_km = total_kg / cotwo
            mean_kg = float(res.mean_dist)      # unweighted per-client mean in kg
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
                predefined_facilities_arr=facilit_vec,  # lock uploaded 1's open (supported in spopt)
                name="p-center-exploit-preloaded"
            )
            res = mdl.solve(solver)

            max_kg = float(res.problem.objective.value())  # minimized maximum (worst) client cost
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

        # ---------- Reporting ----------
        facility_list = "Please find your results below <br>" + f"<b>{title}</b><br>" + metrics_html

        total_size = sum(len(cli) for cli in fac2cli)
        facil = []
        for fac, cli in enumerate(fac2cli):
            if len(cli) != 0:
                per = (len(cli) / total_size * 100.0) if total_size else 0.0
                per = round(per, 2)
                facility_list += f"facility {fac} serving {per}% of customers; <br>"
                facil.append(fac)

        presult = facility_list

        # Filter the passed-in addresses to opened facilities (by index)
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

    except Exception as e:
        error_message = f"Unexpected error: {str(e)}"
        redis_conn.set(f"error_for_job_{job_id}", error_message)
        return "Task failed"

