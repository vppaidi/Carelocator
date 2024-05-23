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


REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
REDIS_DB = int(os.getenv('REDIS_DB', 0))
REDIS_PASSWORD = os.getenv('REDIS_PASSWORD', None)
REDIS_SSL = os.getenv('REDIS_SSL', 'False') == 'True'

redis_conn = redis.StrictRedis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, password=REDIS_PASSWORD, ssl=REDIS_SSL)

def recommend_task2(selected_dropdown, P_FACILITIES, dm, wei, addresses):
    
    job = get_current_job()
    job_id = job.id
    
    try:
            
        # Retrieve the stored data from session
        selected_option = selected_dropdown
        
        # Access the uploaded data from the session variable
        #uploaded_json_data = session.get('uploaded_data')
        
        
    #    if isinstance(destinations, str):
    #       destinations = pd.DataFrame(json.loads(destinations))
        
    
        
        P_FACILITIES = P_FACILITIES
        
        od_matrix = pd.DataFrame(dm)   
         
        weights = pd.DataFrame(wei)
        
        weights = weights.to_numpy()
        
            
        #od_matrix = np.zeros((len(origins_gdf), len(destinations_gdf)))
        od_matrix = pd.DataFrame(dm)
        # Convert the matrix to a DataFrame for easier viewing and manipulation
        #od_matrix = distance_matrix
        
           
       
    # Print the OD matrix DataFrame
        #print(od_matrix)
        
        od_matrix = od_matrix.to_numpy()
        #od_matrix=od_matrix/1000
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
        dis_obj=pmp_obj/cotwo
        dis_obj=round(dis_obj,2)
        pmp_mean = round(pmedian.mean_dist, 2)
        dis_mean = pmp_mean/cotwo
        dis_mean=round(dis_mean,2)
        print(pmp_mean)
        
        facility_list =str(f"Please find your results below <br>")
        
        facility_list += str(f"A total minimized weighted CO2 emissions of {pmp_obj} Kg was observed.  <br>")
        facility_list += str(f"A total minimized weighted distance of {dis_obj} Km was observed. <br>")
        facility_list += str(f"A mean kg/km CO2 emissions of {pmp_mean} was observed  <br>")
        facility_list += str(f"A mean distance of {dis_mean} km was observed  <br>")
        
        
        total_size = 0
        for fac,cli in enumerate(pmedian.fac2cli):
            total_size += len(cli)
    
        print(total_size)
        
        faci=1
        facil = []
        for fac, cli in enumerate(pmedian.fac2cli):
            
            if len(cli) != 0:
                per = (len(cli)/total_size) * 100
                per = round(per,2)
                facility_list += str(f"facility {fac} serving {per}% of customers; <br>")
                facil.append(fac)
                faci=faci+1
                continue
            faci=faci+1
            
    
        print (facil)
        #session['presult'] = facility_list
        presult = facility_list
        
        addresses3 = []
        print(presult)
                   
        addresses2=addresses3
        #session['addresses2']=addresses2
        print(addresses2)
        nearest_origin_indexes = []
        # Once your results are ready:
        result_data = {
           "presult": presult,
           "facil": facil,
           "addresses": addresses,
           "nearest_origin_indexes": nearest_origin_indexes,
           # ... other data ...
        }
           
        job = get_current_job()
        job_id = job.id
        
        redis_conn.set(f"result_data_for_job_{job_id}", json.dumps(result_data))
    
        #return render_template('pfac.html')
        
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

