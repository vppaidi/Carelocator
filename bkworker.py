import os
from redis import Redis
from rq import Worker, Queue, Connection

# Configuration for Redis connection
REDIS_HOST = os.getenv('REDIS_HOST')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))  # Default to port 6379 if not specified
REDIS_DB = int(os.getenv('REDIS_DB', 0))         # Default to DB 0 if not specified
REDIS_PASSWORD = os.getenv('REDIS_PASSWORD')
REDIS_SSL = os.getenv('REDIS_SSL', 'False').lower() == 'true'  # Convert to a boolean

# Establish the Redis connection
if REDIS_SSL:
    conn = Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, password=REDIS_PASSWORD, ssl=True)
else:
    conn = Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, password=REDIS_PASSWORD)

# Main execution when the script is run
if __name__ == '__main__':
    with Connection(conn):
        try:
            print("Starting bkworker...")
            bkworker = Worker(Queue('default'))
            bkworker.work()
        except Exception as e:
            print(f"Error: {e}")
