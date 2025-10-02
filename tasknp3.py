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
from io import StringIO
# from worker import worker_function
# from multiprocessing import Pool, cpu_count

REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
REDIS_DB = int(os.getenv('REDIS_DB', 0))
REDIS_PASSWORD = os.getenv('REDIS_PASSWORD', None)
REDIS_SSL = os.getenv('REDIS_SSL', 'False') == 'True'

redis_conn = redis.StrictRedis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, password=REDIS_PASSWORD, ssl=REDIS_SSL)


def recommend_task3(selected_dropdown, uploaded_data_json, facilit, P_FACILITIES, origins, wei, addresses, mode="pmedian"):
    """
    Explore with user-uploaded candidates + base candidates (origins).
    - Clients = origins (rows)
    - Candidates = [uploaded (up)] + [origins] (columns)
    - 'facilit' contains a 'facility' 0/1 column for uploaded rows;
      we force-open those via 'predefined_facilities_arr' and add their count to p.

    Supports:
      mode="pmedian"  -> efficiency (need-weighted sum distance)
      mode="pcenter"  -> equity (minimize maximum distance; ignores weights)
    """

    job = get_current_job()
    job_id = job.id

    try:
        nearest_origin_indexes = []

        selected_option = selected_dropdown

        # Uploaded candidates (first block of columns)
        up = pd.read_json(StringIO(uploaded_data_json))

        # Base candidates / also client set
        origins = pd.DataFrame(origins)
        num_rows = len(origins)
        print("This is the number of rows in origins", num_rows)

        # Zero pad for the origins block when we build the predefined mask
        zero_df = pd.DataFrame([[0] for _ in range(num_rows)])

        # Read the 'facility' flag vector for uploaded rows (0/1)
        facilit = pd.read_json(StringIO(facilit))
        if isinstance(facilit, str):
            facilit = pd.DataFrame(json.loads(facilit))
        facilit = facilit["facility"]
        print("this is facilit")
        print(facilit)

        facnum = len(facilit)  # number of uploaded rows (columns at the front)
        # The UI shows uploaded candidates differently in the map
        nearest_origin_indexes = list(range(facnum))

        # Predefined facilities array: [uploaded flags] + [zeros for origins]
        facility = pd.concat([facilit, zero_df], ignore_index=True)

        # Increase p by the count of uploaded rows (keeps your legacy behavior)
        P_FACILITIES = int(P_FACILITIES) + facnum

        # Build full candidate set: uploaded first, then origins
        destinations = pd.concat([up, origins], ignore_index=True)
        print(len(destinations))

        # Weights (need/pop) for clients
        weights = pd.DataFrame(wei).to_numpy(dtype=float).ravel()

        # Road network
        G = ox.graph_from_place(selected_option, network_type='drive')

        # Shortest-path OD (meters) from each client (origin) to each candidate (destination)
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

                # Nearest nodes
                orig_node = ox.distance.nearest_nodes(G, orig_point.x, orig_point.y)
                dest_node = ox.distance.nearest_nodes(G, dest_point.x, dest_point.y)

                try:
                    # length in meters
                    path_length = nx.shortest_path_length(G, orig_node, dest_node, weight='length')
                except nx.NetworkXNoPath:
                    print("No path found")
                    path_length = 10000.0

                distance_matrix[i, j] = float(path_length)

        # Replace zeros (self) with large number
        #distance_matrix[distance_matrix == 0] = 100000.0
        distance_matrix = np.round(distance_matrix, 0)

        print(f"This is the OD Matrix shape: {distance_matrix.shape}")

        end_time = time.time()
        et = datetime.datetime.now()
        print("end time:-", et)
        print(f"Total time: {end_time - start_time:.2f} seconds")

        # Convert to km and then to CO2 kg
        od_matrix = distance_matrix / 1000.0
        COTWO_PER_KM = 0.15
        cost_matrix = od_matrix * COTWO_PER_KM
        cost_matrix = np.where(cost_matrix < 0, 0.0, cost_matrix)

        # Predefined mask as numpy
        facility_mask = facility.to_numpy().astype(int).ravel()

        # ---------- Solve ----------
        solver = HiGHS_CMD(msg=False)
        mode = (mode or "pmedian").lower()

        if mode == "pmedian":
            mdl = PMedian.from_cost_matrix(
                cost_matrix=cost_matrix,
                weights=weights,
                p_facilities=P_FACILITIES,
                predefined_facilities_arr=facility_mask,
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
                f"A mean distance of {mean_km:.2f} km was observed  <br>"
            )

        elif mode == "pcenter":
            # p-center typically ignores weights.
            # We keep your 'pre-open' semantics via predefined_facilities_arr.
            mdl = PCenter.from_cost_matrix(
                cost_matrix=cost_matrix,
                p_facilities=P_FACILITIES,
                predefined_facilities_arr=facility_mask,  # <-- force-open uploaded
                name="p-center-network-distance"
            )
            res = mdl.solve(solver)

            max_kg = float(res.problem.objective.value())  # minimized maximum client cost
            max_km = max_kg / COTWO_PER_KM

            fac2cli = res.fac2cli

            # For context only: mean (unweighted) of assigned costs
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

        # ---------- Reporting ----------
        facility_list = str(f"Please find your results below <br><b>{title}</b><br>") + metrics_html

        total_size = sum(len(cli) for cli in fac2cli)
        facil = []
        for fac, cli in enumerate(fac2cli):
            if len(cli) != 0:
                per = (len(cli) / total_size * 100.0) if total_size else 0.0
                per = round(per, 2)
                facility_list += str(f"facility {fac} serving {per:.2f}% of customers; <br>")
                facil.append(fac)

        presult = facility_list

        # Build addresses2 (opened only) if you want, not required by result2.html
        # addresses3 = []
        # for idx, address in enumerate(addresses):
        #     if idx in facil:
        #         a = dict(address)
        #         a["idx"] = idx
        #         addresses3.append(a)
        # addresses2 = addresses3

        result_data = {
            "presult": presult,
            "addresses": addresses,             # all candidates (uploaded + base)
            "facil": facil,                     # opened facility indices
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

