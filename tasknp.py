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
from spopt.locate import PMedian

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
from worker import worker_function
from multiprocessing import Pool, cpu_count




redis_conn = redis.Redis()

def recommend_task(selected_dropdown, P_FACILITIES, origins, wei, addresses):
    
            
    # Retrieve the stored data from session
    selected_option = selected_dropdown
    
    # Access the uploaded data from the session variable
    #uploaded_json_data = session.get('uploaded_data')
    
    
#    if isinstance(destinations, str):
#       destinations = pd.DataFrame(json.loads(destinations))
    
    
    
    P_FACILITIES = P_FACILITIES
    
    origins = pd.DataFrame(origins)
    
    destinations = origins
    
    weights = pd.DataFrame(wei)
    
    weights = weights.to_numpy()
    
    #G = nx.from_dict_of_dicts(G_d["G"], create_using=nx.MultiDiGraph())
    # Load the road network graph
    #G = ox.io.load_graphml('sweden_road_network.graphml')
    #G = ox.io.load_graphml('Borlänge_road_network.graphml')
    G = ox.io.load_graphml(f'{selected_option}.graphml')
    
    # Measure the start time
    start_time = time.time()
    # ct stores current time
    st = datetime.datetime.now()
    print("start time:-", st)
   
    # Create GeoDataFrames

    origins_gdf = gpd.GeoDataFrame(origins, geometry=gpd.points_from_xy(origins.Longitude, origins.Latitude))
    destinations_gdf = gpd.GeoDataFrame(destinations, geometry=gpd.points_from_xy(destinations.Longitude, destinations.Latitude))
    
    
    # Initialize a matrix to store the results
    distance_matrix = np.zeros((len(origins_gdf), len(destinations_gdf)))
    
    # For each pair of origin and destination, find the shortest path
    for i in range(len(origins_gdf)):
        for j in range(len(destinations_gdf)):
            orig_point = origins_gdf.iloc[i].geometry
            dest_point = destinations_gdf.iloc[j].geometry
    
            # Get the nearest nodes to the points
            orig_node = ox.distance.nearest_nodes(G, orig_point.x, orig_point.y)
            dest_node = ox.distance.nearest_nodes(G, dest_point.x, dest_point.y)
    
            # Calculate the shortest path length
            try:
                path_length = nx.shortest_path_length(G, orig_node, dest_node, weight='length')
            except nx.NetworkXNoPath:
                print(f"No path found ")
                path_length = 10000
    
            # Store the result in the matrix
            distance_matrix[i, j] = path_length

       # Concatenate the results to get the final matrix
    
    distance_matrix[distance_matrix == 0] = 100000
    distance_matrix = distance_matrix.round(decimals = 0)
    od_matrix = pd.DataFrame(distance_matrix)
    # Convert the matrix to a DataFrame for easier viewing and manipulation
    #od_matrix = distance_matrix
    
    print(f"This is the OD Matrix: {od_matrix}")
    # Measure the end time
    end_time = time.time()
    # ct stores current time
    et = datetime.datetime.now()
    print("end time:-", et)
    
    # Calculate the time it took
    total_time = end_time - start_time
    
    
    # Estimate the total time
    total_pairs = len(origins_gdf) * len(destinations_gdf)
    estimated_total_time = total_pairs * total_time
    
    #single_pair = total_time/total_pairs
    
    print(f"Total time: {total_time} seconds")
    
    
#    print(f"Single pair time: {single_pair} seconds")
   
   
# Print the OD matrix DataFrame
    #print(od_matrix)
    
    od_matrix = od_matrix.to_numpy()
    od_matrix=od_matrix/1000
    #np.fill_diagonal(od_matrix, 10000)
    cotwo=0.15
    cost_matrix=od_matrix*cotwo
    
    
    
    # Initiating PMedian
    pmedian_from_cm = PMedian.from_cost_matrix(
    cost_matrix,
    weights,
    p_facilities=P_FACILITIES,
    name="p-median-network-distance"
    )
        
    pmedian = pmedian_from_cm.solve(pulp.PULP_CBC_CMD(msg=False))
        
    # Printing results
    pmp_obj = round(pmedian.problem.objective.value(), 2)
    pmp_mean = round(pmedian.mean_dist, 2)
    print(pmp_mean)
    
    facility_list =str(f"Please find your results below <br>")
    
    facility_list += str(f"A total minimized weighted CO2 emissions of {pmp_obj} was observed. <br>")
    facility_list += str(f"A mean kg/km CO2 emissions of {pmp_mean} was observed per each population point.<br>")
    
    
    faci=1
    facil = []
    for fac, cli in enumerate(pmedian.fac2cli):
        if len(cli) != 0:
            facility_list += str(f"facility {fac} serving {len(cli)} population points; <br>")
            facil.append(fac)
            faci=faci+1
            continue
        faci=faci+1
        

    print (facil)
    #session['presult'] = facility_list
    presult = facility_list
    
    
    
    addresses3 = []

    # Iterate over the indices of addresses
    for idx, address in enumerate(addresses):
        
        # Check if the index exists in facil
        if idx in facil:
            #print(idx)
            # Modify the address dictionary to include the idx value
            address["idx"] = idx+1
            addresses3.append(address)
            
    addresses2=addresses3
    #session['addresses2']=addresses2
    #print(addresses2)
    
    # Once your results are ready:
    result_data = {
       "presult": presult,
       "addresses": addresses,
       "facil": facil,
       # ... other data ...
    }
       
    job = get_current_job()
    job_id = job.id
    
    redis_conn.set(f"result_data_for_job_{job_id}", json.dumps(result_data))

    #return render_template('pfac.html')
    
    return "Task complete"
 
 