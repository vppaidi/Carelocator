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
from solvers import highs_solver
from spopt.locate import PMedian, PCenter  # <-- add PCenter
import rq
import osmnx as ox
import networkx as nx
import geopandas as gpd
from shapely.geometry import Point
import time
from io import StringIO
import csv
import datetime
import redis
import json
from flask_rq2 import RQ
from rq import Queue
from redis import Redis
from rq import get_current_job


REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
# Redis.from_url understands rediss:// and TLS on port 6380
redis_conn = Redis.from_url(REDIS_URL)

def pfac_task(selected_dropdown, uploaded_data_json, facilit, P_FACILITIES, origins, wei, addresses, mode="pmedian"):
    """
    Exploit with uploaded (existing + candidate) facilities.

    Clients  = 'origins' rows (demand)
    Candidates = 'destinations' (uploaded_data_json) columns
    'facilit' includes 'facility' 0/1 flags for uploaded rows; we force-open
    those via predefined_facilities_arr and add their count to p.

    mode:
      - "pmedian": efficiency (need-weighted sum distance)
      - "pcenter": equity (minimize maximum distance; ignores weights)
    """
    job = get_current_job()
    job_id = job.id

    try:
        selected_option = selected_dropdown

        # --- Read uploaded candidates (destinations) ---
        destinations = pd.read_json(StringIO(uploaded_data_json))
        if isinstance(destinations, str):
            destinations = pd.DataFrame(json.loads(destinations))

        # --- Facility open flags (for uploaded candidates) ---
        facilit_df = pd.read_json(StringIO(facilit))
        if isinstance(facilit_df, str):
            facilit_df = pd.DataFrame(json.loads(facilit_df))
        nearest_origin_indexes = facilit_df[facilit_df['facility'] == 1].index.tolist()

        facilit_series = facilit_df["facility"]
        value_counts = facilit_series.value_counts()
        count_of_ones = int(value_counts.get(1, 0))

        facility_mask = facilit_series.to_numpy(dtype=int)  # only for uploaded block

        # --- Demand & weights ---
        origins = pd.DataFrame(origins)
        weights = pd.DataFrame(wei).to_numpy(dtype=float).ravel()

        # Increase p by the number of forced-open uploaded facilities
        P_FACILITIES = int(P_FACILITIES) + count_of_ones

        # --- Road network for the selected place ---
        G = ox.graph_from_place(selected_option, network_type='drive')

        # --- OD (meters): origins x destinations ---
        start_time = time.time()
        st = datetime.datetime.now()
        print("start time:-", st)

        origins_gdf = gpd.GeoDataFrame(origins, geometry=gpd.points_from_xy(origins.Longitude, origins.Latitude))
        destinations_gdf = gpd.GeoDataFrame(destinations, geometry=gpd.points_from_xy(destinations.Longitude, destinations.Latitude))

        distance_matrix = np.zeros((len(origins_gdf), len(destinations_gdf)), dtype=float)

        for i in range(len(origins_gdf)):
            for j in range(len(destinations_gdf)):
                orig_point = origins_gdf.iloc[i].geometry
                dest_point = destinations_gdf.iloc[j].geometry

                orig_node = ox.distance.nearest_nodes(G, orig_point.x, orig_point.y)
                dest_node = ox.distance.nearest_nodes(G, dest_point.x, dest_point.y)

                try:
                    path_length = nx.shortest_path_length(G, orig_node, dest_node, weight='length')
                except nx.NetworkXNoPath:
                    print("No path found between nodes")
                    path_length = 10000.0

                distance_matrix[i, j] = float(path_length)

        od_df = pd.DataFrame(distance_matrix)
        print(f"This is the OD Matrix: {od_df}")

        end_time = time.time()
        et = datetime.datetime.now()
        print("end time:-", et)
        print(f"Total time: {end_time - start_time:.2f} seconds")

        # --- Convert to km, then to CO2 kg ---
        od_matrix = od_df.to_numpy(dtype=float) / 1000.0
        COTWO_PER_KM = 0.15
        cost_matrix = np.where(od_matrix < 0, 0.0, od_matrix) * COTWO_PER_KM

        # --- Solve ---
        solver = highs_solver()
        mode = (mode or "pmedian").lower()

        if mode == "pmedian":
            mdl = PMedian.from_cost_matrix(
                cost_matrix=cost_matrix,
                weights=weights,
                p_facilities=P_FACILITIES,
                predefined_facilities_arr=facility_mask,  # force-open uploaded=1
                name="p-median-network-distance"
            )
            res = mdl.solve(solver)

            total_kg = float(res.problem.objective.value())
            total_km = total_kg / COTWO_PER_KM
            mean_kg = float(res.mean_dist)
            mean_km = mean_kg / COTWO_PER_KM

            fac2cli = res.fac2cli
            title = "P-median (efficiency)"
            metrics_html = (
                f"A total minimized weighted CO₂ emissions of {total_kg:.2f} Kg was observed.  <br>"
                f"A total minimized weighted distance of {total_km:.2f} Km was observed.  <br>"
                f"A mean kg/km CO₂ emissions of {mean_kg:.2f} was observed  <br>"
                f"A mean distance of {mean_km:.2f} Km was observed  <br>"
            )

        elif mode == "pcenter":
            # p-center ignores weights; still honor forced-open uploaded sites
            mdl = PCenter.from_cost_matrix(
                cost_matrix=cost_matrix,
                p_facilities=P_FACILITIES,
                predefined_facilities_arr=facility_mask,
                name="p-center-network-distance"
            )
            res = mdl.solve(solver)

            max_kg = float(res.problem.objective.value())   # minimized worst-case client cost
            max_km = max_kg / COTWO_PER_KM

            fac2cli = res.fac2cli
            # context mean (unweighted)
            assigned_costs = [cost_matrix[i, f] for f, cli in enumerate(fac2cli) for i in cli]
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

        # --- Reporting ---
        facility_list = "Please find your results below <br><b>{}</b><br>{}".format(title, metrics_html)

        total_size = sum(len(cli) for cli in fac2cli)
        facil = []
        for fac, cli in enumerate(fac2cli):
            if len(cli) != 0:
                per = (len(cli) / total_size * 100.0) if total_size else 0.0
                per = round(per, 2)
                facility_list += f"facility {fac} serving {per}% of customers; <br>"
                facil.append(fac)

        presult = facility_list

        # Mark opened facility coordinates for the map
        addresses2 = []
        for idx, address in enumerate(addresses):
            if idx in facil:
                a = dict(address)
                a["idx"] = idx
                addresses2.append(a)

        # --- Store result ---
        result_data = {
           "presult": presult,
           "addresses2": addresses2,
           "facil": facil,
           "nearest_origin_indexes": nearest_origin_indexes,
           "mode": mode
        }

        redis_conn.set(f"result_data_for_job_{job_id}", json.dumps(result_data))
        return "Task complete"

    except RuntimeError as e:
        error_message = str("Runtime Error: Please check your input")
        print("This is an run e***************************")
        print(error_message)
        redis_conn.set(f"error_for_job_{job_id}", error_message)
        return "Task failed"

    except Exception as e:
        error_message = f"Unexpected error: There is an exception raised. {str(e)}"
        print("This is an exception***************************")
        redis_conn.set(f"error_for_job_{job_id}", error_message)
        return "Task failed"




