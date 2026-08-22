"""
In-memory tracker for background upload jobs (see /admin/upload in app.py).

This is process-local: fine for the single-worker deployment this app
runs as (Procfile has no -w flag, so gunicorn defaults to one worker), but
progress polls would miss the job if the app ever runs with multiple
worker processes. Not worth a DB-backed job table for a single-admin tool.
"""
import threading
import time
import uuid

_jobs = {}
_lock = threading.Lock()
_MAX_JOB_AGE_SECONDS = 30 * 60


def new_job(job_type):
    job_id = uuid.uuid4().hex
    with _lock:
        _prune()
        _jobs[job_id] = {
            "type": job_type, "status": "processing",
            "processed": 0, "total": 0,
            "summary": None, "error": None,
            "created_at": time.time(),
        }
    return job_id


def update_job(job_id, **fields):
    with _lock:
        if job_id in _jobs:
            _jobs[job_id].update(fields)


def get_job(job_id):
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def _prune():
    """Drop finished jobs older than _MAX_JOB_AGE_SECONDS - called while
    already holding _lock."""
    cutoff = time.time() - _MAX_JOB_AGE_SECONDS
    stale = [jid for jid, j in _jobs.items() if j["created_at"] < cutoff]
    for jid in stale:
        del _jobs[jid]
