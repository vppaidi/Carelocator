#!/bin/bash

# Activate virtual environment
source antenv/bin/activate

# If using gunicorn
gunicorn DSS:app --bind 0.0.0.0:8000 > gunicorn.log 2>&1 &

# Start background worker
python bkworker.py > worker.log 2>&1 &
