# -*- coding: utf-8 -*-
"""
Created on Tue Aug 15 11:16:04 2023

@author: vpp
"""

from flask import Flask, render_template, session, redirect, url_for, session, request, jsonify
# from flask_sqlalchemy import SQLAlchemy
# from sqlalchemy import text
# from sqlalchemy import create_engine

import os
import pandas as pd
import numpy as np
import pulp
from solvers import highs_solver
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
from multiprocessing import Pool, cpu_count
from worker import calculate_path
from io import StringIO

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

def recommend_task(selected_dropdown, P_FACILITIES, origins, wei, addresses, mode="pmedian"):
    """
    Builds an OD (clients x candidates) from the selected place with OSMnx,
    then solves p-median (efficiency) or p-center (equity).

    Parameters
    ----------
    selected_dropdown : str
        Place name (e.g., municipality) for OSMnx graph_from_place.
    P_FACILITIES : int
        Number of facilities to open.
    origins : list(dict)
        [{'Latitude':..., 'Longitude':...}, ...] candidate/client points.
        Typically locations[Name == selected_dropdown][['Latitude','Longitude']]
    wei : list(dict)
        [{'Weights': w_i}, ...] need/pop weights for each client.
    addresses : list(dict)
        [{'index': i, 'lat': ..., 'lon': ...}, ...] candidate coords aligned to OD columns.
    mode : str
        "pmedian" (default) or "pcenter".
    """
    job = get_current_job()
    job_id = job.id

    try:
        selected_option = selected_dropdown
        P_FACILITIES = int(P_FACILITIES)

        # ---------- Inputs ----------
        origins = pd.DataFrame(origins)
        destinations = origins.copy()  # clients=candidates (square OD)
        w = pd.DataFrame(wei).to_numpy(dtype=float).ravel()  # (n_clients,)

        # ---------- Graph ----------
        # You already do this for the selected place:
        G = ox.graph_from_place(selected_option, network_type='drive')

        # ---------- Build OD matrix (meters) ----------
        start_time = time.time()
        origins_gdf = gpd.GeoDataFrame(origins, geometry=gpd.points_from_xy(origins.Longitude, origins.Latitude))
        destinations_gdf = gpd.GeoDataFrame(destinations, geometry=gpd.points_from_xy(destinations.Longitude, destinations.Latitude))

        distance_matrix = np.zeros((len(origins_gdf), len(destinations_gdf)), dtype=float)

        for i in range(len(origins_gdf)):
            for j in range(len(destinations_gdf)):
                orig_point = origins_gdf.iloc[i].geometry
                dest_point = destinations_gdf.iloc[j].geometry

                weight_key = 'length'  # edge length in meters

                # Get nearest nodes
                orig_node = ox.distance.nearest_nodes(G, orig_point.x, orig_point.y)
                dest_node = ox.distance.nearest_nodes(G, dest_point.x, dest_point.y)

                try:
                    path = ox.distance.shortest_path(G, orig_node, dest_node, weight=weight_key, cpus=2)
                    path_edges = list(zip(path[:-1], path[1:]))
                    path_length = sum([G[u][v][0][weight_key] for u, v in path_edges])
                except nx.NetworkXNoPath:
                    path_length = 10000.0  # large penalty
                except Exception:
                    path_length = 10000.0

                distance_matrix[i, j] = float(path_length)

        # Replace any zero self-distances (should be 0 if i==j) with large cost
        #distance_matrix[distance_matrix == 0] = 100000.0

        # ---------- Convert to km and CO2 ----------
        od_matrix_km = distance_matrix / 1000.0
        COTWO_PER_KM = 0.15  # kg per km
        cm = od_matrix_km * COTWO_PER_KM  # cost matrix in kg

        # clamp any negative noise (unlikely)
        cm = np.where(cm < 0, 0.0, cm)

        # ---------- Solve ----------
        solver = highs_solver()
        mode = (mode or "pmedian").lower()

        if mode == "pmedian":
            mdl = PMedian.from_cost_matrix(
                cost_matrix=cm,
                weights=w,
                p_facilities=P_FACILITIES,
                name="p-median-network-distance"
            )
            res = mdl.solve(solver)

            total_kg = float(res.problem.objective.value())
            total_km = total_kg / COTWO_PER_KM
            mean_kg = float(res.mean_dist)
            mean_km = mean_kg / COTWO_PER_KM

            fac2cli = res.fac2cli
            _write_facility_distance_csv(
                job_id=job_id,
                fac2cli=fac2cli,
                distance_matrix_km=od_matrix_km,
                weights=w,
                facility_coords=addresses,
                population_coords=addresses,
                facility_index_map=None
            )
            title = "P-median (efficiency)"
            metrics_html = (
                f"A total minimized need-weighted CO2 of {total_kg:.2f} Kg "
                f"({total_km:.2f} Km) was observed.  <br>"
                f"A mean CO2 per client of {mean_kg:.2f} Kg "
                f"({mean_km:.2f} Km) was observed  <br>"
            )

        elif mode == "pcenter":
            mdl = PCenter.from_cost_matrix(
                cost_matrix=cm,  # p-center ignores weights
                p_facilities=P_FACILITIES,
                name="p-center-network-distance"
            )
            res = mdl.solve(solver)

            max_kg = float(res.problem.objective.value())  # minimized maximum client cost (kg)
            max_km = max_kg / COTWO_PER_KM

            fac2cli = res.fac2cli
            _write_facility_distance_csv(
                job_id=job_id,
                fac2cli=fac2cli,
                distance_matrix_km=od_matrix_km,
                weights=w,
                facility_coords=addresses,
                population_coords=addresses,
                facility_index_map=None
            )
            # mean for context only (unweighted)
            assigned_costs = [cm[i, f] for f, cli in enumerate(fac2cli) for i in cli]
            assigned_costs = np.asarray(assigned_costs, dtype=float)
            mean_kg = float(assigned_costs.mean()) if assigned_costs.size else 0.0
            mean_km = mean_kg / COTWO_PER_KM

            title = "P-center (equity)"
            metrics_html = (
                f"Max (minimized) client CO2 W = {max_kg:.2f} Kg ({max_km:.2f} Km).<br>"
                f"Mean CO2 per client (for context) = {mean_kg:.2f} Kg "
                f"({mean_km:.2f} Km).<br>"
            )

        else:
            raise ValueError("mode must be 'pmedian' or 'pcenter'")

        # ---------- Reporting ----------
        facility_list = str(f"Please find your results below <br><b>{title}</b><br>") + metrics_html

        total_size = sum(len(cli) for cli in fac2cli)
        facil = []
        for fac, cli in enumerate(fac2cli):
            if len(cli) != 0:
                share = (len(cli) / total_size * 100.0) if total_size else 0.0
                facility_list += str(f"facility {fac} serving {share:.2f}% of customers; <br>")
                facil.append(fac)

        presult = facility_list
        nearest_origin_indexes = []

        # ---------- Store ----------
        result_data = {
            "presult": presult,
            "addresses": addresses,  # all candidates
            "facil": facil,          # opened facilities (column indices)
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
        error_message = f"Unexpected error: There is an exception raised. {str(e)}"
        redis_conn.set(f"error_for_job_{job_id}", error_message)
        return "Task failed"
