"""Crash-safety test: SIGKILL a real worker process in the middle of sending,
start a fresh worker, and prove that no job is lost.

This is the executable version of the answer in ANSWERS.md.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from jobqueue.providers import MOCK_SENT_KEY, MOCK_STARTED_KEY
from jobqueue.tasks import enqueue_email
from tests.conftest import TEST_REDIS_URL, wait_until

ROOT = Path(__file__).resolve().parent.parent
N_JOBS = 12
VISIBILITY_TIMEOUT_S = 5


def _start_worker() -> subprocess.Popen:
    env = {
        **os.environ,
        "JOBQUEUE_REDIS_URL": TEST_REDIS_URL,
        # short visibility timeout so the test does not wait 5 minutes
        "JOBQUEUE_VISIBILITY_TIMEOUT_S": str(VISIBILITY_TIMEOUT_S),
        "JOBQUEUE_RATE_WINDOW_S": "1",
        "JOBQUEUE_RATE_JITTER_S": "0",
        "JOBQUEUE_BACKOFF_CAP_S": "1",
        "JOBQUEUE_RATE_LIMIT": "1000",
        "MOCK_PROVIDER_DELAY_S": "1.5",   # each send takes 1.5 s -> easy to catch mid-run
    }
    return subprocess.Popen(
        [
            sys.executable, "-m", "celery", "-A", "jobqueue.celery_app:app", "worker",
            "-Q", "emails", "-c", "4", "-l", "WARNING",
            "--without-gossip", "--without-mingle", "--without-heartbeat",
        ],
        cwd=ROOT, env=env, start_new_session=True,  # own process group -> we can kill all of it
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _sigkill(proc: subprocess.Popen) -> None:
    """kill -9 the worker main process AND its pool children (like a node dying)."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait(timeout=10)


@pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX process groups + SIGKILL; run in WSL, Docker or Linux/macOS")
def test_sigkill_mid_run_loses_no_jobs(redis_client):
    ids = [f"crash-{i:02d}" for i in range(N_JOBS)]
    for job_id in ids:
        enqueue_email(to=f"{job_id}@example.com", subject="s", body="b", job_id=job_id)

    worker1 = _start_worker()
    worker2 = None
    try:
        # Wait until a second wave of sends has started, i.e. work is in flight.
        wait_until(lambda: redis_client.hlen(MOCK_STARTED_KEY) >= 5, timeout=60, message="worker busy")
        _sigkill(worker1)  # <- the crash

        started = set(redis_client.hkeys(MOCK_STARTED_KEY))
        sent_at_crash = set(redis_client.hkeys(MOCK_SENT_KEY))
        in_flight = started - sent_at_crash
        assert in_flight, "test is vacuous: nothing was in flight when the worker was killed"
        assert len(sent_at_crash) < N_JOBS

        worker2 = _start_worker()  # operator / supervisor restarts the worker
        wait_until(
            lambda: redis_client.hlen(MOCK_SENT_KEY) == N_JOBS,
            timeout=120, interval=0.5, message="all jobs delivered after the crash",
        )

        sent = {k: int(v) for k, v in redis_client.hgetall(MOCK_SENT_KEY).items()}
        assert set(sent) == set(ids)                       # nothing lost
        assert all(v >= 1 for v in sent.values())
        # The jobs that were mid-flight at the crash were redelivered and completed:
        assert in_flight <= set(sent)
    finally:
        for proc in (worker1, worker2):
            if proc is not None and proc.poll() is None:
                _sigkill(proc)
