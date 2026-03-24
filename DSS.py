# -*- coding: utf-8 -*-
"""
Main Flask app for CareLocator
"""
from flask import Flask, render_template, session, redirect, url_for, request, jsonify, send_from_directory, flash
from flask_wtf import FlaskForm
from wtforms import StringField, BooleanField, DateTimeField, RadioField, SelectField, TextAreaField, SubmitField
from wtforms.validators import DataRequired

import urllib.parse
import sys
import gc
import os
import io
import csv
import copy
import time
import json
import datetime
import pandas as pd
import numpy as np
import requests
import re
import uuid

from rq import Queue
from redis import Redis
from rq.job import Job
from rq.exceptions import NoSuchJobError
from flask_session import Session

# ---------- Task modules ----------
# Explore (on-the-fly OD via OSMnx)
from tasknp_test import recommend_task
# Explore (preloaded OD)
from tasknp2 import recommend_task2
# Explore with uploaded + base (on-the-fly OD)
from tasknp3 import recommend_task3
# Explore with uploaded + preloaded OD
from tasknp4 import recommend_task4
# Exploit (uploaded facilities only)
from tasknn import pfac_task
# Exploit (uploaded with preloaded OD)
from tasknn2 import pfac_task2

# -----------------------------------------------------------------------------
# Flask app
# -----------------------------------------------------------------------------
app = Flask(__name__)
app.config['SECRET_KEY'] = 'mysecretkey'
# Prevent accidental huge uploads (adjust if you need more)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB

# -----------------------------------------------------------------------------
# Unified Redis connection (for web + workers) via REDIS_URL
# -----------------------------------------------------------------------------

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
redis_conn = Redis.from_url(REDIS_URL)
queue = Queue(connection=redis_conn)
print(f"[DSS] Using Redis at {REDIS_URL}")

# -----------------------------------------------------------------------------
# Server-side sessions in Redis (optional but recommended for multi-user)
# -----------------------------------------------------------------------------
app.config.update(
    SESSION_TYPE="redis",
    SESSION_REDIS=redis_conn,
    SESSION_PERMANENT=False,
    PERMANENT_SESSION_LIFETIME=3600,  # 1 hour
)
Session(app)

# -----------------------------------------------------------------------------
# Data (GitHub-only)
# -----------------------------------------------------------------------------
GITHUB_USER   = "vppaidi"
GITHUB_REPO   = "Carelocator"
GITHUB_BRANCH = "main"
GITHUB_RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}/{GITHUB_BRANCH}"

file_name = "datacsv"
file_path = f"./{file_name}.csv"
locations = pd.read_csv(file_path, delimiter=';')

predata = ['Blekinge.csv', 'Borlänge.csv', 'Borås.csv', 'Dalarnas.csv', 
            'Eskilstuna.csv', 'Falun.csv', 'Gotlands.csv', 'Gävle.csv', 'Gävleborgs.csv', 'Göteborg.csv', 
            'Hallands.csv', 'Halmstad.csv', 'Haninge.csv', 'Helsingborg.csv', 'Huddinge.csv', 'Jämtlands.csv', 'Järfälla.csv', 
            'Jönköping.csv', 'Jönköpings.csv', 'Kalmar.csv', 'Kalmars.csv', 'Karlskrona.csv', 'Karlstad.csv', 'Kristianstad.csv', 'Kronobergs.csv', 
            'Kungsbacka.csv', 'Linköping.csv', 'Ludvika.csv', 'Luleå.csv', 'Lund.csv', 'Malmö.csv', 'Mölndal.csv', 'Nacka.csv', 
            'Norrbottens.csv', 'Norrköping.csv', 'Skellefteå.csv', 'Skåne.csv', 'Sollentuna.csv', 'Solna.csv', 'Stockholm.csv', 
            'Stockholms.csv', 'Sundsvall.csv', 'Sweden.csv', 'Södermanlands.csv', 'Södertälje.csv', 'Täby.csv', 'Umeå.csv', 'Uppsala.csv', 'Uppsalas.csv',
            'Värmlands.csv', 'Västerbottens.csv', 'Västernorrlands.csv', 'Västerås.csv', 'Västmanlands.csv', 'Västra Götalands.csv', 
            'Växjö.csv', 'Örebro.csv','Örebros.csv' 'Östergötlands.csv']


# -----------------------------------------------------------------------------
# WTForm placeholder
# -----------------------------------------------------------------------------
class InfoForm(FlaskForm):
    pass

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _normalize_numeric(df: pd.DataFrame, cols):
    for col in cols:
        if col in df.columns:
            if col.lower() in ('latitude', 'longitude'):
                df[col] = pd.to_numeric(df[col], errors='coerce')
            else:
                df[col] = (
                    df[col].astype(str).str.replace(',', '.', regex=False)
                          .pipe(pd.to_numeric, errors='coerce')
                          .fillna(0.0)
                )
    return df

def _choose_weights(scope_rows: pd.DataFrame, weight_system: str) -> pd.DataFrame:
    weight_system = (weight_system or 'Population').strip().lower()
    base_col = 'Pop' if weight_system == 'population' else 'Weights'
    if base_col not in scope_rows.columns:
        raise ValueError(f"Column '{base_col}' not found for the selected scope")
    _normalize_numeric(scope_rows, [base_col])
    out = scope_rows[[base_col]].rename(columns={base_col: 'Weights'}).reset_index(drop=True)
    return out

def _radius_for_scope(selected_radio: str) -> int:
    return {'option1': 20000, 'option2': 5000, 'option3': 1000}.get(selected_radio or '', 5000)

def _load_preloaded_od(name: str):
    """
    Legacy loader for RECOMMEND routes: load and return a DataFrame for the
    precomputed OD matrix. (Caution: loading very large files can be heavy.)
    """
    import os

    if not isinstance(name, str) or not name.strip():
        return None

    # Try simple exact filenames first (prefer parquet)
    for ext in (".parquet", ".csv"):
        p = os.path.join(".", f"{name}{ext}")
        if os.path.exists(p):
            if p.lower().endswith(".parquet"):
                return pd.read_parquet(p)
            return pd.read_csv(p, header=None, dtype=np.float32, low_memory=False)

    # Fallback: recursive search in common roots
    roots = [".", "static/od", "static/predata", "predata", "od", "static"]
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _, files in os.walk(root):
            for fn in files:
                if not fn.lower().endswith((".csv", ".parquet")):
                    continue
                if name.lower() in fn.lower():
                    path = os.path.join(dirpath, fn)
                    if path.lower().endswith(".parquet"):
                        return pd.read_parquet(path)
                    return pd.read_csv(path, header=None, dtype=np.float32, low_memory=False)

    return None
    
def _find_preloaded_path_only(name: str) -> str | None:
    """
    PFAC-only helper: return a file path (str) to a precomputed OD matrix
    without loading it into memory.
    """
    import os, unicodedata

    def norm(s: str) -> str:
        s = (s or "").strip()
        return unicodedata.normalize("NFKD", s).encode("ASCII", "ignore").decode("ASCII").lower()

    def stem_no_ext(fn: str) -> str:
        return os.path.splitext(os.path.basename(fn))[0]

    if not isinstance(name, str) or not name.strip():
        return None

    raw = name.strip()
    base_key = norm(stem_no_ext(raw))
    keys = {base_key}
    keys.add(base_key[:-1] if base_key.endswith("s") else base_key + "s")
    keys.add((base_key + " lan").strip())
    keys.add((base_key.replace(" lan", "")).strip())

    # Prefer exact files in cwd
    for fn in ([f"{raw}.parquet", f"{raw}.csv"] if not raw.lower().endswith((".csv",".parquet")) else [raw]):
        p = os.path.join(".", fn)
        if os.path.exists(p):
            return p

    # Recursive search
    roots = [".", "static/od", "static/predata", "predata", "od", "static"]
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _, files in os.walk(root):
            for fn in files:
                if not fn.lower().endswith((".csv", ".parquet")):
                    continue
                if norm(stem_no_ext(fn)) in keys:
                    return os.path.join(dirpath, fn)
    return None



# ---- Payload stash in Redis to avoid huge client sessions -------------------
_PAYLOAD_TTL = 2 * 60 * 60  # 2 hours

def _stash_payload(obj: dict) -> str:
    """Store a dict in Redis, return a short key to keep in session."""
    key = f"payload:{uuid.uuid4().hex}"
    redis_conn.setex(key, _PAYLOAD_TTL, json.dumps(obj))
    return key

def _pop_payload(key: str) -> dict | None:
    """Fetch and delete payload from Redis."""
    if not key:
        return None
    raw = redis_conn.get(key)
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        payload = None
    # delete right away to free memory
    try:
        redis_conn.delete(key)
    except Exception:
        pass
    return payload

# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.route('/', methods=['GET', 'POST'])
def index():
    form = InfoForm()
    data = None
    options = 'Borlänge'

    if request.method == 'POST':
        selected_radio = request.form.get('radio')
        session['selected_radio'] = selected_radio

        if selected_radio and selected_radio.endswith('3'):
            selected_dropdown = request.form.get('city')
        else:
            selected_dropdown = request.form.get('dropdown' + selected_radio[-1]) if selected_radio else None

        session['s_option'] = selected_dropdown

        option = request.form.get('option')
        if option == 'option10':  # EXPLOIT
            return render_template('upload.html')
        elif option == 'option11':  # EXPLORE
            return render_template('recommend.html')
        else:
            flash('Please choose EXPLOIT or EXPLORE before submitting.')
            return redirect(url_for('index'))

    return render_template('index.html', form=form, options=options, data=data)

# -----------------------------------------------------------------------------
# Upload (EXPLOIT)
# -----------------------------------------------------------------------------
@app.route('/upload', methods=['GET','POST'])
def upload():
    """
    EXPLOIT flow upload. Store uploaded candidate sites and the chosen mode/weights.
    Large payloads go into Redis (payload key stored in session).
    """
    selected_dropdown = session.get('s_option')

    # objective mode & weight system for /pfac
    mode = (request.form.get('mode') or 'pmedian').strip().lower()
    session['mode'] = mode

    weight_system = (request.form.get('weightSystem') or 'Population').strip()
    session['weightSystem'] = weight_system

    selected_option = request.form.get('fileOption')  # optionA (addresses) or other (coords)

    if request.method == 'POST':
        selected_option = request.form.get('fileOption')
        if selected_option == 'optionA':
            # ----------------------- ADDRESSES CSV -----------------------
            file = request.files.get('fileA')
            P_FACILITIES = request.form.get('facilities')
            try:
                P_FACILITIES = int(P_FACILITIES)
                if not (0 <= P_FACILITIES <= 300):
                    raise ValueError("Value out of range")
            except ValueError as e:
                return f"Invalid input: {e}", 400
            session['P_FACILITIES'] = P_FACILITIES

            if not file:
                return {"error": "No file"}, 400

            # Read once as DataFrame
            df = pd.read_csv(file)
            # Try common header variants for Address
            addr_col = None
            for c in df.columns:
                if c.strip().lower() in ('address', '\ufeffaddress'):
                    addr_col = c
                    break
            if addr_col is None:
                return "CSV must contain an 'Address' column.", 400

            addresses = []
            for address in df[addr_col].astype(str).fillna('').str.strip().tolist():
                if not address:
                    continue
                url = 'https://photon.komoot.io/api/?q=' + address
                try:
                    response = requests.get(url)
                    if response.status_code == 200 and 'application/json' in response.headers.get('Content-Type',''):
                        data = response.json()
                        if data and 'features' in data and len(data['features']) > 0:
                            lat = data['features'][0]['geometry']['coordinates'][1]
                            lon = data['features'][0]['geometry']['coordinates'][0]
                            addresses.append({'address': address, 'lat': float(lat), 'lon': float(lon)})
                except Exception:
                    pass
                time.sleep(1)

            uploaded_df = pd.DataFrame(addresses)
            if not uploaded_df.empty:
                uploaded_df.rename(columns={'lat':'Latitude','lon':'Longitude'}, inplace=True)
                uploaded_data_json = uploaded_df[['Latitude','Longitude']].to_json()
            else:
                uploaded_data_json = pd.DataFrame(columns=['Latitude','Longitude']).to_json()

            # facilit_json is the original CSV content
            facilit_json = df.to_json()

            # Stash payload in Redis (not in session cookie!)
            payload_key = _stash_payload({
                "uploaded_data_json": uploaded_data_json,
                "facilit_json": facilit_json,
                "addresses": addresses
            })
            session['upload_payload_key'] = payload_key

            # Free memory aggressively
            del df, uploaded_df
            gc.collect()

            return render_template('pfac.html', addresses=addresses)

        else:
            # ----------------------- COORDINATES CSV -----------------------
            file = request.files.get('fileB')
            P_FACILITIES = request.form.get('facilities')
            try:
                P_FACILITIES = int(P_FACILITIES)
                if not (0 <= P_FACILITIES <= 300):
                    raise ValueError("Value out of range")
            except ValueError as e:
                return f"Invalid input: {e}", 400
            session['P_FACILITIES'] = P_FACILITIES

            if not file:
                return {"error": "No file"}, 400

            df = pd.read_csv(file)
            # Find Latitude/Longitude columns with or without BOM
            lat_col = None
            lon_col = None
            for c in df.columns:
                lc = c.strip().lower()
                if lc in ('latitude', '\ufefflatitude'):
                    lat_col = c
                if lc == 'longitude':
                    lon_col = c
            if not lat_col or not lon_col:
                return "CSV must contain 'Latitude' and 'Longitude' columns.", 400

            _normalize_numeric(df, [lat_col, lon_col])
            addresses = []
            for lat, lon in df[[lat_col, lon_col]].itertuples(index=False):
                if pd.notna(lat) and pd.notna(lon):
                    addresses.append({'lat': float(lat), 'lon': float(lon)})

            uploaded_df = pd.DataFrame(addresses).rename(columns={'lat':'Latitude','lon':'Longitude'})
            uploaded_data_json = uploaded_df[['Latitude','Longitude']].to_json()

            facilit_json = df.to_json()

            payload_key = _stash_payload({
                "uploaded_data_json": uploaded_data_json,
                "facilit_json": facilit_json,
                "addresses": addresses
            })
            session['upload_payload_key'] = payload_key

            del df, uploaded_df
            gc.collect()

            return render_template('pfac.html', addresses=addresses)

    # GET
    return render_template('upload.html')

# -----------------------------------------------------------------------------
# PFAC (EXPLOIT)
# -----------------------------------------------------------------------------
@app.route('/pfac', methods=['GET','POST'])
def pfac():
    """
    EXPLOIT: run optimization using uploaded facilities (and optionally preloaded OD).
    Heavy upload payload is fetched from Redis, not session.
    """
    P_FACILITIES = session.get('P_FACILITIES')
    selected_dropdown = session.get('s_option')
    print(f"[DSS] >>> Selected dropdown (scope) = '{selected_dropdown}' <<<")

    mode = session.get('mode', 'pmedian')
    weight_system = session.get('weightSystem', 'Population')

    selected_radio = session.get('selected_radio')
    session['radius'] = _radius_for_scope(selected_radio)

    # Retrieve payload from Redis
    payload_key = session.pop('upload_payload_key', None)
    payload = _pop_payload(payload_key) if payload_key else None
    if not payload:
        return jsonify({"error":"Uploaded payload expired or missing. Please re-upload your CSV."}), 400

    uploaded_data_json = payload.get('uploaded_data_json')
    facilit_json = payload.get('facilit_json')
    addresses = payload.get('addresses', [])

    # Build origins & weights for the selected scope
    scope_rows = locations[locations['Name'] == selected_dropdown].copy()
    if scope_rows.empty:
        return jsonify({"error": f"No rows found in datacsv.csv for Name == '{selected_dropdown}'"}), 404
    _normalize_numeric(scope_rows, ['Latitude','Longitude','Pop','Weights'])

    origins_df = scope_rows[['Latitude', 'Longitude']].reset_index(drop=True)
    origins = origins_df.to_dict(orient='records')

    try:
        wei_df = _choose_weights(scope_rows, weight_system)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    wei = wei_df.to_dict(orient='records')

    # --- Precomputed OD for PFAC: pass PATH (not DataFrame) to the worker ---
    STOCKHOLMS_PARQUET_PATH = "Stockholms.parquet"  # set absolute path if needed
    dm_path = None

    if selected_dropdown and selected_dropdown.strip().lower() in ["stockholm", "stockholms", "stockholms lan"]:
        if os.path.exists(STOCKHOLMS_PARQUET_PATH):
            dm_path = STOCKHOLMS_PARQUET_PATH
            print(f"[DSS] Using parquet for Stockholms: {dm_path}")
        else:
            dm_path = _find_preloaded_path_only(selected_dropdown)
    else:
        dm_path = _find_preloaded_path_only(selected_dropdown)

    if dm_path is not None:
        job = queue.enqueue(
            pfac_task2,
            selected_dropdown, P_FACILITIES, uploaded_data_json, facilit_json,
            dm_path,  # << pass PATH string
            origins, wei, addresses,
            mode=mode,
            job_timeout=97200
        )
        return jsonify({"message": "Task queued!", "job_id": job.get_id(), "dm_path": dm_path}), 200
    else:
        job = queue.enqueue(
            pfac_task,
            selected_dropdown, uploaded_data_json, facilit_json, P_FACILITIES,
            origins, wei, addresses,
            mode=mode,
            job_timeout=97200
        )
        return jsonify({"message": "Task queued!", "job_id": job.get_id()}), 200


# -----------------------------------------------------------------------------
# RECOMMEND (EXPLORE) 
# -----------------------------------------------------------------------------
@app.route('/recommend', methods=['GET', 'POST'])
def recommend():
    selected_dropdown = session.get('s_option')
    selected_radio = session.get('selected_radio')
    if not selected_dropdown:
        return "No scope selected on index page.", 400

    session['radius'] = _radius_for_scope(selected_radio)

    P_FACILITIES = request.form.get('facilities', '').strip()
    try:
        P_FACILITIES = int(P_FACILITIES)
        if not (1 <= P_FACILITIES <= 250):
            raise ValueError
    except Exception:
        return "Invalid number of facilities. Enter an integer between 1 and 200.", 400
    session['P_FACILITIES'] = P_FACILITIES

    mode = (request.form.get("mode") or "pmedian").strip().lower()
    weight_system = (request.form.get('weightSystem') or 'Population').strip()

    scope_rows = locations[locations['Name'] == selected_dropdown].copy()
    if scope_rows.empty:
        return f"No rows found in datacsv.csv for Name == '{selected_dropdown}'", 404
    _normalize_numeric(scope_rows, ['Latitude','Longitude','Pop','Weights'])

    origins = scope_rows[['Latitude','Longitude']].reset_index(drop=True).to_dict(orient='records')
    addresses_base = [
        {'index': i, 'lat': str(lat).strip(), 'lon': str(lon).strip()}
        for i, (lat, lon) in enumerate(scope_rows[['Latitude','Longitude']].reset_index(drop=True).values)
    ]

    try:
        wei_df = _choose_weights(scope_rows, weight_system)
    except Exception as e:
        return str(e), 400
    wei = wei_df.to_dict(orient='records')

        # --- Precomputed OD for EXPLORE: pass PATH (not DataFrame) to worker ---
    STOCKHOLMS_PARQUET_PATH = "Stockholms.parquet"  # set absolute path if needed
    dm_path = None

    if selected_dropdown and selected_dropdown.strip().lower() in ["stockholm", "stockholms", "stockholms lan", "stockholms län"]:
        if os.path.exists(STOCKHOLMS_PARQUET_PATH):
            dm_path = STOCKHOLMS_PARQUET_PATH
            print(f"[DSS] Using parquet for Stockholms: {dm_path}")
        else:
            dm_path = _find_preloaded_path_only(selected_dropdown)
    else:
        dm_path = _find_preloaded_path_only(selected_dropdown)

    has_preloaded_od = dm_path is not None

    file = request.files.get('csvFile', None)
    if not file:
        if has_preloaded_od:
            job = queue.enqueue(
                recommend_task2,
                selected_dropdown, P_FACILITIES, dm_path, wei, addresses_base,
                mode=mode,
                job_timeout=97200
            )
            return jsonify({
                "message": "Task queued!",
                "job_id": job.get_id(),
                "addr": [],
                "dm_path": dm_path
            }), 200
        else:
            job = queue.enqueue(
                recommend_task,
                selected_dropdown, P_FACILITIES, origins, wei, addresses_base,
                mode=mode,
                job_timeout=97200
            )
            return jsonify({"message": "Task queued!", "job_id": job.get_id(), "addr": []}), 200
    
    # UPLOAD path (kept as-is)
    csv_type = (request.form.get('csvType') or 'csv_c').strip().lower()
    if csv_type not in ('csv_c', 'csv_d'):
        return "csvType must be 'csv_c' (addresses) or 'csv_d' (coordinates).", 400

    addr_markers = []
    if csv_type == 'csv_c':
        dfu = pd.read_csv(file)
        addr_col = None
        for c in dfu.columns:
            if c.strip().lower() in ('address', '\ufeffaddress'):
                addr_col = c
                break
        if addr_col is None:
            return "CSV must contain an 'Address' column.", 400

        idx = 0
        for address in dfu[addr_col].astype(str).fillna('').str.strip().tolist():
            if not address:
                continue
            url = 'https://photon.komoot.io/api/?q=' + address
            try:
                resp = requests.get(url)
                if resp.status_code == 200 and 'application/json' in resp.headers.get('Content-Type',''):
                    data = resp.json()
                    if data and 'features' in data and len(data['features']) > 0:
                        lat = data['features'][0]['geometry']['coordinates'][1]
                        lon = data['features'][0]['geometry']['coordinates'][0]
                        addr_markers.append({'index': idx, 'lat': lat, 'lon': lon})
                        idx += 1
            except Exception:
                pass
            time.sleep(1)

        up_df = pd.DataFrame(addr_markers).rename(columns={'lat':'Latitude','lon':'Longitude'})
        uploaded_data_json = up_df[['Latitude','Longitude']].to_json()
        facilit_json = dfu.to_json()
        del dfu, up_df
        gc.collect()

    else:
        dfu = pd.read_csv(file)
        lat_col = None
        lon_col = None
        for c in dfu.columns:
            lc = c.strip().lower()
            if lc in ('latitude', '\ufefflatitude'):
                lat_col = c
            if lc == 'longitude':
                lon_col = c
        if not lat_col or not lon_col:
            return "CSV must contain 'Latitude' and 'Longitude' columns.", 400

        _normalize_numeric(dfu, [lat_col, lon_col])
        idx = 0
        uploaded_pts = []
        for lat, lon in dfu[[lat_col, lon_col]].itertuples(index=False):
            if pd.notna(lat) and pd.notna(lon):
                uploaded_pts.append({'index': idx, 'lat': float(lat), 'lon': float(lon)})
                idx += 1

        addr_markers = uploaded_pts[:]
        up_df = pd.DataFrame(uploaded_pts).rename(columns={'lat':'Latitude','lon':'Longitude'})
        uploaded_data_json = up_df[['Latitude','Longitude']].to_json()
        facilit_json = dfu.to_json()
        del dfu, up_df
        gc.collect()

    if has_preloaded_od:
        job = queue.enqueue(
            recommend_task4,
            selected_dropdown, P_FACILITIES, dm_path,
            uploaded_data_json, facilit_json, origins, wei, addresses_base,
            mode=mode,
            job_timeout=97200
        )
    else:
        job = queue.enqueue(
            recommend_task3,
            selected_dropdown, uploaded_data_json, facilit_json, P_FACILITIES,
            origins, wei, addresses_base,
            mode=mode,
            job_timeout=97200
        )

    return jsonify({"message": "Task queued!", "job_id": job.get_id(), "addr": addr_markers}), 200

# -----------------------------------------------------------------------------
# CUSTOM
# -----------------------------------------------------------------------------
@app.route('/custom', methods=['GET'])
def custom_page():
    return render_template('custom.html')

@app.route('/custom/run', methods=['POST'])
def custom_run():
    """
    Custom flow with optional multipliers and optional uploaded facilities.
    IMPORTANT: pass ONLY the base candidate addresses (addresses_base) to the task,
    so nearest_origin_indexes aligns with candidate indices and blue markers show
    correctly for uploaded sites flagged as facility=1.
    """
    try:
        # --- Scope selection ---
        selected_radio = (request.form.get('radio') or '').strip()
        if selected_radio == 'option1':        # National
            selected_name = request.form.get('dropdown1', '').strip() or 'Sweden'
        elif selected_radio == 'option2':      # Regional
            selected_name = request.form.get('dropdown2', '').strip()
        elif selected_radio == 'option3':      # Municipality
            selected_name = request.form.get('city', '').strip()
        else:
            return jsonify({"error": "Please select National, Regional or Municipality."}), 400
        if not selected_name:
            return jsonify({"error": "Please choose a name for the selected scope."}), 400

        session['selected_radio'] = selected_radio
        session['s_option'] = selected_name
        session['radius'] = _radius_for_scope(selected_radio)

        # --- datacsv subset by Name ---
        loc = locations.copy()
        _normalize_numeric(loc, ['Pop','Weights','Latitude','Longitude'])
        sub = loc.loc[loc['Name'] == selected_name].copy()
        if sub.empty:
            return jsonify({"error": f"No rows found in datacsv.csv for Name == '{selected_name}'"}), 404

        # Default multiplier = 1
        sub['__mult__'] = 1.0

        # --- OPTIONAL multipliers CSV (name, factor) ---
        mult_file = request.files.get('multCsv')
        if mult_file and mult_file.filename:
            try:
                up = pd.read_csv(mult_file, header=None)
            except Exception:
                mult_file.seek(0)
                up = pd.read_csv(mult_file)
            if up.shape[1] < 2:
                return jsonify({"error": "CSV must have at least two columns: name, custom weight"}), 400
            up = up.iloc[:, :2].copy()
            up.columns = ['__name__', '__factor__']
            up['__name__'] = up['__name__'].astype(str).str.strip()
            up['__factor__'] = (
                up['__factor__'].astype(str).str.replace(',', '.', regex=False)
                                  .replace(['', 'nan', 'None'], np.nan)
            )
            up['__factor__'] = pd.to_numeric(up['__factor__'], errors='coerce').fillna(0.0)

            # unify column variants
            rename_map = {}
            if 'Kommunnamn' in sub.columns and 'kommunnamn' not in sub.columns:
                rename_map['Kommunnamn']='kommunnamn'
            if 'Lannamn' in sub.columns and 'lannamn' not in sub.columns:
                rename_map['Lannamn']='lannamn'
            if 'lan' in sub.columns and 'lannamn' not in sub.columns and 'Lannamn' not in sub.columns:
                rename_map['lan']='lannamn'
            if 'kommun' in sub.columns and 'kommunnamn' not in sub.columns and 'Kommunnamn' not in sub.columns:
                rename_map['kommun']='kommunnamn'
            if 'DeSO' in sub.columns and 'deso' not in sub.columns:
                rename_map['DeSO']='deso'
            if 'DESO' in sub.columns and 'deso' not in sub.columns:
                rename_map['DESO']='deso'
            sub.rename(columns=rename_map, inplace=True)

            up_text  = up[~up['__name__'].str.contains(r'\d', regex=True)].copy()
            up_alnum = up[ up['__name__'].str.contains(r'\d', regex=True)].copy()

            # TEXT → kommunnamn OR lannamn
            if not up_text.empty:
                m_txt = up_text.rename(columns={'__name__':'__key__', '__factor__':'__f__'})
                if 'kommunnamn' in sub.columns:
                    sub = sub.merge(m_txt[['__key__','__f__']], left_on='kommunnamn', right_on='__key__', how='left')
                    sub['__mult__'] = sub['__mult__'] * sub['__f__'].fillna(1.0)
                    sub.drop(columns=['__key__','__f__'], inplace=True, errors='ignore')
                if 'lannamn' in sub.columns:
                    sub = sub.merge(m_txt[['__key__','__f__']], left_on='lannamn', right_on='__key__', how='left')
                    sub['__mult__'] = sub['__mult__'] * sub['__f__'].fillna(1.0)
                    sub.drop(columns=['__key__','__f__'], inplace=True, errors='ignore')

            # ALPHANUM → deso
            if not up_alnum.empty and 'deso' in sub.columns:
                m_deso = up_alnum.rename(columns={'__name__':'deso', '__factor__':'__fd__'})
                sub = sub.merge(m_deso[['deso','__fd__']], on='deso', how='left')
                sub['__mult__'] = sub['__mult__'] * sub['__fd__'].fillna(1.0)
                sub.drop(columns=['__fd__'], inplace=True, errors='ignore')

        # --- Choose operational weights (Population/CNI) ---
        weight_system = (request.form.get('weightSystem') or 'Population').strip().lower()
        base_col = 'Pop' if weight_system == 'population' else 'Weights'
        if base_col not in sub.columns:
            return jsonify({"error": f"Column '{base_col}' not found for the selected scope"}), 400
        sub['__adj_w__'] = (sub[base_col].fillna(0.0) * sub['__mult__'])

        # weights & candidate addresses (MUST align with OD candidate order)
        wei = sub[['__adj_w__']].rename(columns={'__adj_w__':'Weights'}).reset_index(drop=True).to_dict(orient='records')
        if not {'Latitude','Longitude'}.issubset(sub.columns):
            return jsonify({"error":"Latitude/Longitude columns missing for the selected scope"}), 400
        origins = sub[['Latitude','Longitude']].reset_index(drop=True).to_dict(orient='records')
        addresses_base = [
            {'index': i, 'lat': str(lat).strip(), 'lon': str(lon).strip()}
            for i, (lat, lon) in enumerate(sub[['Latitude','Longitude']].reset_index(drop=True).values)
        ]

        # --- OPTIONAL existing facilities upload (like /recommend) ---
        uploaded_markers = []       # for immediate client plotting, if needed
        uploaded_data_json = None   # coords of uploaded sites
        facilit_json = None         # full uploaded CSV (used by tasks)

        site_enabled = request.files.get('siteCsv') is not None and request.files['siteCsv'].filename != ''
        site_csv_type = (request.form.get('siteCsvType') or 'csv_c').strip().lower()

        if site_enabled:
            site_file = request.files['siteCsv']
            if site_csv_type == 'csv_c':
                # Addresses → geocode
                stream = io.StringIO(site_file.stream.read().decode("UTF8"), newline=None)
                csv_input = csv.DictReader(stream)
                idx = 0
                for row in csv_input:
                    address = row.get('\ufeffAddress', row.get('Address', '')).strip()
                    if not address:
                        continue
                    url = 'https://photon.komoot.io/api/?q=' + address
                    try:
                        resp = requests.get(url)
                        if resp.status_code == 200 and 'application/json' in resp.headers.get('Content-Type',''):
                            data = resp.json()
                            if data and 'features' in data and len(data['features'])>0:
                                lat = data['features'][0]['geometry']['coordinates'][1]
                                lon = data['features'][0]['geometry']['coordinates'][0]
                                uploaded_markers.append({'index': idx, 'lat': lat, 'lon': lon})
                                idx += 1
                    except Exception:
                        pass
                    time.sleep(1)
                df_up = pd.DataFrame(uploaded_markers).rename(columns={'lat':'Latitude','lon':'Longitude'})
                uploaded_data_json = df_up[['Latitude','Longitude']].to_json()
                site_file.seek(0)
                dff = pd.read_csv(site_file)
                facilit_json = dff.to_json()
            else:
                # Coordinates CSV
                stream = io.StringIO(site_file.stream.read().decode("UTF8"), newline=None)
                csv_input = csv.DictReader(stream)
                idx = 0
                for row in csv_input:
                    lat = row.get('\ufeffLatitude', row.get('Latitude','')).strip()
                    lon = row.get('Longitude','').strip()
                    if lat and lon:
                        uploaded_markers.append({'index': idx, 'lat': lat, 'lon': lon})
                        idx += 1
                df_up = pd.DataFrame(uploaded_markers).rename(columns={'lat':'Latitude','lon':'Longitude'})
                uploaded_data_json = df_up[['Latitude','Longitude']].to_json()
                site_file.seek(0)
                dff = pd.read_csv(site_file)
                facilit_json = dff.to_json()

        # --- Preloaded vs on-the-fly OD (GitHub-only) ---
        dm_df = _load_preloaded_od(selected_name)
        has_preloaded = dm_df is not None

        mode = (request.form.get('mode') or 'pmedian').strip().lower()
        try:
            P_FACILITIES = int(request.form.get('facilities') or '3')
            if not (1 <= P_FACILITIES <= 250):
                raise ValueError
        except Exception:
            return jsonify({"error":"# facilities must be an integer between 1 and 200"}), 400

        # >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
        # KEY: pass ONLY addresses_base to tasks (never a mixed list)
        # >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>

        if not site_enabled:
            if has_preloaded:
                dm = dm_df.to_dict(orient='records')
                del dm_df; gc.collect()
                job = queue.enqueue(
                    recommend_task2,
                    selected_name, P_FACILITIES, dm, wei, addresses_base,
                    mode,
                    job_timeout=97200
                )
            else:
                job = queue.enqueue(
                    recommend_task,
                    selected_name, P_FACILITIES, origins, wei, addresses_base,
                    mode,
                    job_timeout=97200
                )
        else:
            if not uploaded_data_json or not facilit_json:
                return jsonify({"error":"Uploaded facilities CSV could not be parsed."}), 400
            if has_preloaded:
                dm = dm_df.to_dict(orient='records')
                del dm_df; gc.collect()
                job = queue.enqueue(
                    recommend_task4,
                    selected_name, P_FACILITIES, dm,
                    uploaded_data_json, facilit_json, origins, wei, addresses_base,
                    mode,
                    job_timeout=97200
                )
            else:
                job = queue.enqueue(
                    recommend_task3,
                    selected_name, uploaded_data_json, facilit_json, P_FACILITIES,
                    origins, wei, addresses_base,
                    mode,
                    job_timeout=97200
                )

        return jsonify({"message":"Task queued!", "job_id": job.get_id(), "addr": uploaded_markers}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# -----------------------------------------------------------------------------
# Download examples
# -----------------------------------------------------------------------------
@app.route('/download_example')
def download_example():
    return send_from_directory('static', 'examples/dest.csv', as_attachment=True)

@app.route('/download_example4')
def download_example4():
    return send_from_directory('static', 'examples/dest4.csv', as_attachment=True)

@app.route('/download_example3')
def download_example3():
    return send_from_directory('static', 'examples/coordinates.csv', as_attachment=True)

@app.route('/download_example2')
def download_example2():
    return send_from_directory('static', 'examples/coordinates2.csv', as_attachment=True)

# -----------------------------------------------------------------------------
# RQ helpers
# -----------------------------------------------------------------------------
@app.route('/task-status/<job_id>', methods=['GET'])
def get_task_status(job_id):
    try:
        job = Job.fetch(job_id, connection=redis_conn)
        if job.is_failed:
            response = {"state": "failed", "message": str(job.exc_info)}
        elif job.is_finished:
            response = {"state": "finished", "result": job.result}
        else:
            response = {"state": job.get_status()}
    except NoSuchJobError:
        response = {"state": "unknown", "message": "Job not found"}
    return jsonify(response), 200

@app.route('/fetch-error/<job_id>', methods=['GET'])
def fetch_error(job_id):
    error_message = redis_conn.get(f"error_for_job_{job_id}").decode('utf-8') if redis_conn.get(f"error_for_job_{job_id}") else None
    return jsonify({'error_message': error_message})

# -----------------------------------------------------------------------------
# Result pages
# -----------------------------------------------------------------------------
@app.route('/result/<job_id>')
def result(job_id):
    error_message = redis_conn.get(f"error_for_job_{job_id}")
    if error_message:
        try:
            return jsonify({"error": error_message.decode('utf-8')}), 500
        except Exception:
            return jsonify({"error": str(error_message)}), 500

    result_data_json = redis_conn.get(f"result_data_for_job_{job_id}")
    if result_data_json is None:
        return "No results found", 404

    if result_data_json:
        result_data = json.loads(result_data_json)
        presult = result_data.get("presult","")
        addresses2 = result_data.get("addresses2",[])
        nearest_origin_indexes = result_data.get("nearest_origin_indexes",[])

        radius = session.get('radius', 5000)
        session.clear()
        return render_template('result.html', data=presult, addresses2=addresses2,
                               radius=radius, nearest_origin_indexes=nearest_origin_indexes)
    else:
        return jsonify({"error": "No result or error found for the given job ID."}), 404

@app.route('/result2/<job_id>')
def result2(job_id):
    error_message = redis_conn.get(f"error_for_job_{job_id}")
    if error_message:
        try:
            return jsonify({"error": error_message.decode('utf-8')}), 500
        except Exception:
            return jsonify({"error": str(error_message)}), 500

    result_data_json = redis_conn.get(f"result_data_for_job_{job_id}")
    if result_data_json is None:
        return "No results found", 404

    if result_data_json:
        result_data = json.loads(result_data_json)
        presult = result_data.get("presult","")
        facil = result_data.get("facil",[])
        addresses = result_data.get("addresses",[])
        nearest_origin_indexes = result_data.get("nearest_origin_indexes",[])

        addresses3 = []
        for address in addresses:
            if address.get('index') in facil:
                updated = dict(address)
                updated.pop('idx', None)
                addresses3.append(updated)

        addresses2 = addresses3
        radius = session.get('radius', 5000)
        session.clear()
        return render_template('result2.html', data=presult, addresses2=addresses2,
                               radius=radius, nearest_origin_indexes=nearest_origin_indexes)
    else:
        return jsonify({"error": "No result or error found for the given job ID."}), 404

@app.route('/browser-closing', methods=['POST'])
def browser_closing():
    gc.collect()
    return "Cleanup done", 200

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=8000)
