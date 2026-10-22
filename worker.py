import json
import logging
import time
import threading
from redis import Redis
from rq import Queue, Worker, get_current_job
from config.settings import settings
from pipeline.cache import cache_key
from app.metrics import (
    increment_counter,
    observe_histogram,
    TICKET_PROCESSING_TIME_SECONDS,
    TICKET_PROCESSING_TOTAL,
    QUEUE_PROCESSING_JOBS,
    QUEUE_FAILED_JOBS_TOTAL,
    TICKET_RISK_SCORE_DISTRIBUTION,
    COMPLIANCE_PASS_TOTAL,
    COMPLIANCE_FAIL_TOTAL,
    set_gauge,
    QUEUE_DEPTH_JOBS,
    TICKET_INGESTION_TOTAL,
    WORKER_STATUS,
)

logger = logging.getLogger(__name__)

# Use the same Redis connection pattern as in main.py for consistency
redis_connection = Redis(host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=0)
task_queue = Queue("aetcg_jobs", connection=redis_connection)


HEARTBEAT_KEY = "worker:health_signal"
HEARTBEAT_TTL_SEC = 20       # the API treats a heartbeat older than 15 s as dead
HEARTBEAT_INTERVAL_SEC = 5


def write_health_signal(connection=None) -> int:
    """Write the current epoch second under HEARTBEAT_KEY with a short TTL.

    Commit 37bba43 swapped setex() for set() but kept the argument order of
    setex(name, time, value), so the key held the value 20 with an expiry of
    <current epoch> seconds (about 56 years). The API read '20' as the
    timestamp and reported the worker dead forever.
    """
    conn = connection or redis_connection
    now = int(time.time())
    conn.set(HEARTBEAT_KEY, now, ex=HEARTBEAT_TTL_SEC)
    return now


def update_worker_health_signal():
    """Periodically update worker health signal in Redis to indicate liveness and job processing capability."""
    while True:
        try:
            write_health_signal()
            time.sleep(HEARTBEAT_INTERVAL_SEC)
        except Exception as e:
            logger.error(f"Failed to update worker health signal: {e}")
            time.sleep(5)  # Wait before retrying

def process_pipeline_task(payload: dict):
    from pipeline.orchestrator import StateMachineOrchestrator
    start_time = time.time()

    # Mark worker as processing a job
    set_gauge(WORKER_STATUS, 1, {"worker_id": "worker_1"})
    set_gauge(QUEUE_PROCESSING_JOBS, 1, {"queue_name": "aetcg_jobs"})

    try:
        orchestrator = StateMachineOrchestrator()
        current = get_current_job()
        result = orchestrator.execute_workflow(payload, job_id=current.id if current else None)

        # Record successful processing metrics
        processing_time = time.time() - start_time
        observe_histogram(TICKET_PROCESSING_TIME_SECONDS, processing_time, {"status": "success"})
        increment_counter(TICKET_PROCESSING_TOTAL, {"status": "success"})

        # Business metrics (risk distribution, category, compliance) are
        # owned by the orchestrator, which has the typed values; counting
        # them again here doubled every figure on the dashboard.

        # Cache the final state result for API lookup (same key function as the API)
        redis_connection.set(cache_key(payload), json.dumps(result), ex=600)

        logger.info(json.dumps({
            "event": "task_processed_successfully",
            "processing_time_ms": processing_time * 1000,
            "job_id": result.get("job_id") if isinstance(result, dict) else None
        }))
        return result

    except Exception as e:
        # Record failure metrics
        processing_time = time.time() - start_time
        observe_histogram(TICKET_PROCESSING_TIME_SECONDS, processing_time, {"status": "failure"})
        increment_counter(QUEUE_FAILED_JOBS_TOTAL, {"queue_name": "aetcg_jobs", "failure_reason": "processing_error"})
        increment_counter(TICKET_PROCESSING_TOTAL, {"status": "failure"})

        logger.error(json.dumps({
            "event": "task_processing_failed",
            "error": str(e),
            "processing_time_ms": processing_time * 1000,
            "ticket_id": payload.get("ticket_id")
        }))
        raise

    finally:
        # A completed job does not make the worker unhealthy.
        set_gauge(WORKER_STATUS, 1, {"worker_id": "worker_1"})
        set_gauge(QUEUE_PROCESSING_JOBS, 0, {"queue_name": "aetcg_jobs"})

def queue_triage_job(payload: dict):
    # Ingestion is counted once, by the API, after the enqueue succeeds.
    job = task_queue.enqueue(process_pipeline_task, payload)

    # Update queue depth gauge (approximate - in practice you'd get this from Redis)
    # For simplicity, we'll increment/decrement based on enqueue/dequeue
    # A more accurate implementation would query Redis for the actual queue length
    set_gauge(QUEUE_DEPTH_JOBS, task_queue.count, {"queue_name": "aetcg_jobs"})

    return job

def main():
    """Main worker entrypoint for running as a background process."""
    # Internal execution target wrapper matching automated Docker environment activations
    logging.basicConfig(level=logging.INFO)
    print(f"[*] Booting AETCG Background Worker Network Layer connecting to Redis on {settings.REDIS_HOST}...")

    # Initialize worker metrics
    set_gauge(WORKER_STATUS, 1, {"worker_id": "worker_1"})
    set_gauge(QUEUE_DEPTH_JOBS, 0, {"queue_name": "aetcg_jobs"})  # Will be updated as jobs come in

    # Start health signal thread to signal worker liveness
    health_signal_thread = threading.Thread(target=update_worker_health_signal, daemon=True)
    health_signal_thread.start()

    worker = Worker([task_queue], connection=redis_connection)
    worker.work()

if __name__ == "__main__":
    main()