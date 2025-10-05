import os
from redis import Redis
from rq import Worker, Queue, Connection

def get_redis_connection():
    # Primary: lower-case env var per your setup
    url = os.getenv("redis_url")
    # Backup: allow uppercase if it ever exists (won't override the primary)
    if not url:
        url = os.getenv("REDIS_URL")
    if not url:
        url = "redis://localhost:6379/0"
        print("[bkworker] WARNING: 'redis_url' not set; defaulting to", url)

    # Use Redis URL directly; enable SSL if scheme is rediss://
    return Redis.from_url(url, ssl=url.lower().startswith("rediss://"))

if __name__ == "__main__":
    queues_csv = os.getenv("QUEUES", "default")
    queue_names = [q.strip() for q in queues_csv.split(",") if q.strip()]
    worker_name = os.getenv("WORKER_NAME")  # optional label for the process

    print(f"[bkworker] Queues={queue_names} | WorkerName={worker_name or '-'}")
    with Connection(get_redis_connection()):
        worker = Worker([Queue(name) for name in queue_names], name=worker_name)
        worker.work()
