#!/bin/sh

gunicorn DSS:app > gunicorn.log 2>&1 &
python bkworker.py > worker.log 2>&1 &
