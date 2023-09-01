from flask import Flask, render_template, session, redirect, url_for, session, request, jsonify, flash

from flask_wtf import FlaskForm
from wtforms import (StringField, BooleanField, DateTimeField,
                     RadioField,SelectField,
                     TextAreaField,SubmitField)
from wtforms.validators import DataRequired
import sys
import gc
import subprocess
from subprocess import run,PIPE
import os
import pandas as pd
import numpy as np #(You are here)
import requests
import pulp
from spopt.locate import PMedian
from flask import send_from_directory
import postg
import osmnx as ox
import networkx as nx
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point
import time
import io
import csv
import datetime;
import json
from rq import Queue
from redis import Redis
from rq.job import Job
# This is used to explore only for newly available locations
from tasknp_test import redis_conn, recommend_task

# This is used to explore only with pre-loaded datasets
from tasknp2 import redis_conn, recommend_task2

# This is to explore with existing locations
from tasknp3 import redis_conn, recommend_task3

# This is to use pre-loaded datasets and with existing locations
from tasknp4 import redis_conn, recommend_task4

#This is used to exploit using given facilities
from tasknn import redis_conn, pfac_task
from rq.exceptions import NoSuchJobError


app = Flask(__name__)



queue = Queue(connection=redis_conn)

# Configure a secret SECRET_KEY
app.config['SECRET_KEY'] = 'mysecretkey'

# Define the expected header name
EXPECTED_HEADER_NAME = 'dest'

# Define the expected header name
EXPECTED_HEADER_NAME2 = 'facility'

addresses = []
addresses2 = []

#Loading data
#locations = pd.read_csv('datacsv.csv')

# Fixed parameters from the provided URL
username = "vppaidi"
repository = "facilitydss"
branch = "4fbfe93bb199e44d6e405a71971770d85fd9aaeb"

# Construct the GitHub raw URL
base_url = "https://raw.githubusercontent.com"

file_name = "datacsv"
file_path = f"{base_url}/{username}/{repository}/{branch}/{file_name}.csv"
locations = pd.read_csv(file_path)


# Now create a WTForm Class
# Lots of fields available:
# http://wtforms.readthedocs.io/en/stable/fields.html
class InfoForm(FlaskForm):
    '''
    This general class gets a lot of form about puppies.
    Mainly a way to go through many of the WTForms Fields.
    '''

@app.route('/', methods=['GET', 'POST'])
def index():
    
    
    # Create instance of the form.
    
    form = InfoForm()
    data = None  # Initialize 'data' with a default value
    options = 'Borlänge'  # The options for the dropdown menu
   

        
    if request.method == 'POST':

                
        selected_radio = request.form.get('radio')
        selected_dropdown  = request.form.get('dropdown' + selected_radio[-1])
        
        session['s_option'] = selected_dropdown
        
        # Print the selected options
        print("Selected Radio Option:", selected_radio)
        print("Selected Dropdown Option:", session.get('s_option'))
        
        option = request.form.get('option')
        # Now option contains the value of the selected radio button
        # Do something with the option here
        print(option)
        
        if option == 'option10':
            return render_template('upload.html')
        
        elif option == 'option11':
            return render_template('recommend.html')

        
    return render_template('index.html', form=form, options=options, data=data)

@app.route('/upload', methods=['GET','POST'])
def upload():
    
        
    #selected_dropdown = "default_value"
    selected_dropdown =session.get('s_option')
    
    uploaded_data_json = '{""}'
    facilit = '{""}'
    P_FACILITIES = 0
    
    selected_option = request.form.get('fileOption')
    print(selected_option)
    
    if request.method == 'POST':
        
        
        selected_option = request.form.get('fileOption')
        print(selected_option)
    
        if selected_option == 'optionA':
            file = request.files['fileA']
            P_FACILITIES = request.form.get('facilities')  # Get the selected option
            P_FACILITIES = int(P_FACILITIES)
            session['P_FACILITIES']= P_FACILITIES
            print(P_FACILITIES)
            
            
            #file = request.files['file']
            if not file:
                return {"error": "No file"}
            
            
            stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
            csv_input = csv.DictReader(stream)

            addresses = []
        
            for row in csv_input:
                # Removing the BOM character
                address = row['\ufeffAddress'.strip()]  # Use the header name to access the correct field
                
                # Photon API endpoint
                url = 'https://photon.komoot.io/api/?q=' + address
            
                response = requests.get(url)
                
                if response.status_code == 200 and 'application/json' in response.headers.get('Content-Type'):
                    data = response.json()
                    
                    # Check if results are available
                    if data and 'features' in data and len(data['features']) > 0:
                        latitude = data['features'][0]['geometry']['coordinates'][1]  # Photon's order is [lon, lat]
                        longitude = data['features'][0]['geometry']['coordinates'][0]
                        addresses.append({'address': address, 'lat': latitude, 'lon': longitude})
                    else:
                        print(f"No results found for address: {address}")
                else:
                    print(f"Failed request for address: {address}. Status: {response.status_code}. Response: {response.text}")
                # Delay for one second
                time.sleep(1)  # Add this line to introduce a delay
            #print(addresses)                    
           
            df = pd.DataFrame(addresses)
            df = df.rename(columns={'lat': 'Latitude', 'lon': 'Longitude'})
            print(df.head())
            uploaded_data =df[['Latitude','Longitude']]
            df = df[['Latitude','Longitude']]
            
            
            file.seek(0)
            dff = pd.read_csv(file)
            facilit = dff
            print(facilit)
            
            if len(df) != len(dff):
                return jsonify({"message": "All Addresses are not identified. Try to upload coordinates using the alternate option", "status": "error"})

            facilit = facilit.to_json() 
            

            uploaded_data_json = uploaded_data.to_json()
            
            
            session['uploaded_data_json'] = uploaded_data_json
            session['facilit'] = facilit
            session['addresses'] = addresses
            
            return render_template('pfac.html', addresses = addresses)
        
        else:
            file = request.files['fileB']
            P_FACILITIES = request.form.get('facilities')  # Get the selected option
            P_FACILITIES = int(P_FACILITIES)
            session['P_FACILITIES']= P_FACILITIES
            print(P_FACILITIES)
            
            
            #file = request.files['file']
            if not file:
                return {"error": "No file"}
            addresses = []  # Initialize the addresses list here
            
            stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
            csv_input = csv.DictReader(stream)
        
            for row in csv_input:
                # Assuming your CSV columns are named 'Latitude', 'Longitude', and 'Address'
                #print(row)
                latitude = row['\ufeffLatitude'].strip() 
                longitude = row['Longitude'].strip()
                            
                addresses.append({'lat': latitude, 'lon': longitude})
                
            df = pd.DataFrame(addresses)
            df = df.rename(columns={'lat': 'Latitude', 'lon': 'Longitude'})
            #print(df.head())
            uploaded_data =df[['Latitude','Longitude']]
            df = df[['Latitude','Longitude']]
                
            
            file.seek(0)
            dff = pd.read_csv(file)
            facilit = dff

            facilit = facilit.to_json() 
            
            uploaded_data_json = uploaded_data.to_json()
           
            session['uploaded_data_json'] = uploaded_data_json
            session['facilit'] = facilit
            session['addresses'] = addresses
                  
            return render_template('pfac.html', addresses = addresses)
  
@app.route('/download_example')
def download_example():

    # Send the example file for download
    return send_from_directory('static', 'examples/dest.csv', as_attachment=True)

   
@app.route('/pfac', methods=['GET','POST'])
def pfac():
    
    P_FACILITIES = session.get('P_FACILITIES')
    selected_dropdown = session.get('s_option')
    facilit = session.get('facilit')
    print("This is selected option", selected_dropdown)
    uploaded_data_json = session.get('uploaded_data_json')
    
       
    

    # Filter rows where 'Name' is 'Sweden' and select 'Latitude' and 'Longitude' columns
    origins = locations[locations['Name'] == selected_dropdown][['Latitude', 'Longitude']].reset_index(drop=True)
    
    print(type(origins))    
    
    print(origins.head(5))
    origins = origins.to_dict(orient='records')
    
    
    wei = locations[locations['Name'] == selected_dropdown][['Weights']].reset_index(drop=True)
    
    
    print(wei.head(5))
    wei = wei.to_dict(orient='records')
    
    addresses = session.get('addresses')
      
    
    #G_d = dict(G=nx.to_dict_of_dicts(G))
        
    #job = pfac_task.queue(selected_dropdown, uploaded_data_json, facilit, P_FACILITIES, origins, wei, addresses)
    job = queue.enqueue(pfac_task, selected_dropdown, uploaded_data_json, facilit, P_FACILITIES, origins, wei, addresses, job_timeout=17200)
    return jsonify({"message": "Task queued!", "job_id": job.get_id()}), 200


@app.route('/recommend', methods=['GET', 'POST'])
def recommend():

    #P_FACILITIES = session.get('P_FACILITIES')
    selected_dropdown = session.get('s_option')
   # facilit = session.get('facilit')
    print("This is selected option", selected_dropdown)
    #uploaded_data_json = session.get('uploaded_data_json')
    
    
    
    if request.method == 'POST':
        file = request.files.get('csvFile', None)  # Use .get() to safely retrieve the file
        P_FACILITIES = request.form.get('facilities')  # Get the selected option
        P_FACILITIES = int(P_FACILITIES)
        session['P_FACILITIES']= P_FACILITIES
        print(P_FACILITIES)
        # If only P_Facilities were selected without upload
        if not file:
            if selected_dropdown == 'Sweden':
                         
                
                              
                file_name = selected_dropdown
                file_path = f"{base_url}/{username}/{repository}/{branch}/{file_name}.csv"
                result = pd.read_csv(file_path, header=None)
                
                #print(result.head(5))
                dm = result.to_dict(orient='records')
                
                
                res = locations[locations['Name'] == selected_dropdown][['Latitude', 'Longitude']].reset_index(drop=True)
                           
                addresses = []

                                    
                for idx, (latitude, longitude) in enumerate(res.values):
                    latitude_str = str(latitude).strip()
                    longitude_str = str(longitude).strip()
                    
                    addresses.append({'index': idx, 'lat': latitude_str, 'lon': longitude_str})
                    
                    
                #origins = pd.DataFrame(res, columns=['Latitude', 'Longitude'])
                #session['addresses'] = addresses
                print(addresses)
                
                
                wei = locations[locations['Name'] == selected_dropdown][['Weights']].reset_index(drop=True)
                #print(wei.head(5))
                wei = wei.to_dict(orient='records')
                job = queue.enqueue(recommend_task2, selected_dropdown, P_FACILITIES, dm, wei, addresses, job_timeout=17200)
                return jsonify({"message": "Task queued!", "job_id": job.get_id()}), 200
            else:
              
                origins = locations[locations['Name'] == selected_dropdown][['Latitude', 'Longitude']].reset_index(drop=True)
                    
                addresses = []
    
                for idx, (latitude, longitude) in enumerate(origins.values):
                    latitude_str = str(latitude).strip()
                    longitude_str = str(longitude).strip()
                    addresses.append({'index': idx, 'lat': latitude_str, 'lon': longitude_str})
                
                print(addresses) 
               # Converting origins to make it serializable
                origins = origins.to_dict(orient='records')
                
    
                wei = locations[locations['Name'] == selected_dropdown][['Weights']].reset_index(drop=True)
                
                wei = wei.to_dict(orient='records')
                
              
                job = queue.enqueue(recommend_task, selected_dropdown, P_FACILITIES, origins, wei, addresses, job_timeout=17200)
                return jsonify({"message": "Task queued!", "job_id": job.get_id()}), 200
        # when file is uploaded
        else:
            print("file uploaded")
            
            
            if selected_dropdown == 'Sweden':
               #print(selected_dropdown) 
               
               file_name = selected_dropdown
               file_path = f"{base_url}/{username}/{repository}/{branch}/{file_name}.csv"
               result = pd.read_csv(file_path, header=None)
               
               #print(result.head(5))
               dm = result.to_dict(orient='records')
               
               addresses = []
               stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
               csv_input = csv.DictReader(stream)
               
               idx = 0  # Initialize index counter outside the loop

               for row in csv_input:
                   # Removing the BOM character
                   address = row['\ufeffAddress'.strip()]  # Use the header name to access the correct field
                                
                   # Photon API endpoint
                   url = 'https://photon.komoot.io/api/?q=' + address
                
                   response = requests.get(url)
                    
                   if response.status_code == 200 and 'application/json' in response.headers.get('Content-Type'):
                       data = response.json()
                        
                       # Check if results are available
                       if data and 'features' in data and len(data['features']) > 0:
                           latitude = data['features'][0]['geometry']['coordinates'][1]  # Photon's order is [lon, lat]
                           longitude = data['features'][0]['geometry']['coordinates'][0]
                           addresses.append({'index': idx, 'lat': latitude, 'lon': longitude})  # Add index to the dictionary
                           idx += 1  # Increment the index counter
                       else:
                           print(f"No results found for address: {address}")
                   else:
                       print(f"Failed request for address: {address}. Status: {response.status_code}. Response: {response.text}")
                    
                    # Delay for one second
                   time.sleep(1)
           
               df = pd.DataFrame(addresses)
               num_rows=len(df)
               df = df.rename(columns={'lat': 'Latitude', 'lon': 'Longitude'})
               #print(df.head())
               uploaded_data =df[['Latitude','Longitude']]
               df = df[['Latitude','Longitude']]
               
               file.seek(0)
               dff = pd.read_csv(file)
               facilit = dff
               #print(facilit)
               facilit = facilit.to_json() 
               
               uploaded_data_json = uploaded_data.to_json()
               
               wei = locations[locations['Name'] == selected_dropdown][['Weights']].reset_index(drop=True)
               #print(wei.head(5))
               wei = wei.to_dict(orient='records')
               
               
               origins = locations[locations['Name'] == selected_dropdown][['Latitude', 'Longitude']].reset_index(drop=True)
               for idx, (latitude, longitude) in enumerate(origins.values):
                   latitude_str = str(latitude).strip()
                   longitude_str = str(longitude).strip()
                   idx=idx+num_rows
                   addresses.append({'index': idx, 'lat': latitude_str, 'lon': longitude_str})
                   idx=idx-num_rows
              
               origins = origins.to_dict(orient='records')
               
               job = queue.enqueue(recommend_task4, selected_dropdown, P_FACILITIES, dm, uploaded_data_json, facilit, origins, wei, addresses, job_timeout=97200)
               return jsonify({"message": "Task queued!", "job_id": job.get_id()}), 200
            else:
                    
                addresses = []
                stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
                csv_input = csv.DictReader(stream)
                
                idx = 0  # Initialize index counter outside the loop

                for row in csv_input:
                    # Removing the BOM character
                    address = row['\ufeffAddress'.strip()]  # Use the header name to access the correct field
                                 
                    # Photon API endpoint
                    url = 'https://photon.komoot.io/api/?q=' + address
                 
                    response = requests.get(url)
                     
                    if response.status_code == 200 and 'application/json' in response.headers.get('Content-Type'):
                        data = response.json()
                         
                        # Check if results are available
                        if data and 'features' in data and len(data['features']) > 0:
                            latitude = data['features'][0]['geometry']['coordinates'][1]  # Photon's order is [lon, lat]
                            longitude = data['features'][0]['geometry']['coordinates'][0]
                            addresses.append({'index': idx, 'lat': latitude, 'lon': longitude})  # Add index to the dictionary
                            idx += 1  # Increment the index counter
                        else:
                            print(f"No results found for address: {address}")
                    else:
                        print(f"Failed request for address: {address}. Status: {response.status_code}. Response: {response.text}")
                     
                     # Delay for one second
                    time.sleep(1)
                
                df = pd.DataFrame(addresses) 
                num_rows=len(df)
                df = df.rename(columns={'lat': 'Latitude', 'lon': 'Longitude'})
                #print(df.head())
                uploaded_data =df[['Latitude','Longitude']]
                df = df[['Latitude','Longitude']]
                
                
                file.seek(0)
                dff = pd.read_csv(file)
                facilit = dff
                #print(facilit)
                facilit = facilit.to_json() 
                
                uploaded_data_json = uploaded_data.to_json()
                        
                            
                
                res = locations[locations['Name'] == selected_dropdown][['Latitude', 'Longitude']].reset_index(drop=True)
                
                for idx, (latitude, longitude) in enumerate(res.values):
                    latitude_str = str(latitude).strip()
                    longitude_str = str(longitude).strip()
                    idx=idx+num_rows
                    addresses.append({'index': idx, 'lat': latitude_str, 'lon': longitude_str})
                    idx=idx-num_rows
                
               
                
                session['uploaded_data_json'] = uploaded_data_json
                session['facilit'] = facilit
                #session['addresses'] = addresses
                
                
                origins = locations[locations['Name'] == selected_dropdown][['Latitude', 'Longitude']].reset_index(drop=True)
                #print(origins.head(5))
                origins = origins.to_dict(orient='records')
               
                wei = locations[locations['Name'] == selected_dropdown][['Weights']].reset_index(drop=True)
                #print(wei.head(5))
                wei = wei.to_dict(orient='records')
                
                job = queue.enqueue(recommend_task3, selected_dropdown, uploaded_data_json, facilit, P_FACILITIES, origins, wei, addresses, job_timeout=97200)
                return jsonify({"message": "Task queued!", "job_id": job.get_id()}), 200
    
    

@app.route('/task-status/<job_id>', methods=['GET'])
def get_task_status(job_id):
    try:
        job = Job.fetch(job_id, connection=redis_conn)  # Use the established Redis connection here.

        if job.is_failed:
            response = {"state": "failed", "message": str(job.exc_info)}
        elif job.is_finished:
            response = {"state": "finished", "result": job.result}
        else:
            response = {"state": job.get_status()}
    except NoSuchJobError:  # If the job doesn't exist, catch the exception.
        response = {"state": "unknown", "message": "Job not found"}

    return jsonify(response), 200


@app.route('/result/<job_id>')
def result(job_id):
    
    #redis_conn = Redis()
    
    result_data_json = redis_conn.get(f"result_data_for_job_{job_id}")
    if result_data_json is None:
        return "No results found", 404

    result_data = json.loads(result_data_json)
    
    presult=result_data["presult"] 
    addresses2=result_data["addresses2"]
    #print(addresses2)
    data = presult
    addresses2 = addresses2

    
    # Your result displaying code here
    return render_template('result.html', data = data, addresses2=addresses2)
    
@app.route('/result2/<job_id>')
def result2(job_id):
    
    #redis_conn = Redis()
    
    result_data_json = redis_conn.get(f"result_data_for_job_{job_id}")
    if result_data_json is None:
        return "No results found", 404

    result_data = json.loads(result_data_json)
    presult=result_data["presult"] 
    facil=result_data["facil"]
    addresses = result_data["addresses"]
    
    #addresses = session.get('addresses')
    addresses3 = []
    
    for address in addresses:
        #print(address['index'])
        print(address)
        # Check if the index exists in facil
        if address['index'] in facil:
            # Avoid adding 'idx' to addresses3
            updated_address = address.copy()
            if 'idx' in updated_address:
                del updated_address['idx']
            addresses3.append(updated_address)
    
    #print(addresses3)
    
    addresses2 = addresses3
    
    data = presult
    facil = facil

    
    # Your result displaying code here
    return render_template('result2.html', data = data, addresses2=addresses2)


@app.route('/browser-closing', methods=['POST'])
def browser_closing():
  
    gc.collect()

    
    return "Cleanup done", 200

if __name__ == '__main__':
   
    app.run(False)

