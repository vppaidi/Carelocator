# -*- coding: utf-8 -*-
"""
Created on Mon Sep  4 14:09:34 2023

@author: vpp
"""
import os
from redis import Redis
from rq import Worker, Queue, Connection

# Configuration for Redis connection
redis_url = os.getenv('REDISTOGO_URL', 'redis://localhost:6379')  # Use your Redis URL here
conn = Redis.from_url(redis_url)

# Main execution when the script is run
if __name__ == '__main__':
    with Connection(conn):
        # If you want to run a worker for specific queues, you can specify those queue names like: `queues = [Queue('high'), Queue('default')]`
        worker = Worker(Queue('default'))
        worker.work()
