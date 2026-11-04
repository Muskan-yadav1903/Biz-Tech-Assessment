# Section 2 — Rate-Limited Async Job Queue

Transactional-email queue built on **Celery + Redis** that respects a provider limit of
**200 emails/minute**, survives bursts of 2,000 jobs, retries with exponential backoff,
dead-letters permanently failed jobs, and does not lose jobs when a worker is `kill -9`'d.

|                    Document |                                                               Contents |
|-----------------------------|------------------------------------------------------------------------|
| [`DESIGN.md`](../DESIGN.md) | Architecture choice (Celery vs Django Q vs custom), rate-limiter trade-offs, failure behavior |
| [`ANSWERS.md`](../ANSWERS.md) (Section 2) | Written SIGKILL answer (names the exact Celery settings) |

## Quick start (≈ 2 minutes)

Requirements: Python 3.11+ and either Docker **or** a local `redis-server`.

```bash
cd section2_job_queue

# 1. Redis (pick one)
docker compose up -d --wait redis          # Docker
# redis-server --daemonize yes             # …or a local install

# 2. Python environment
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Run all tests (~30 s)
.venv/bin/python -m pytest
```

Or just `make test` (does all three steps).

> **Windows:** Redis does not run natively on Windows, so start it with Docker Desktop
> (`docker compose up -d --wait redis`) or run everything inside WSL2. The Celery *prefork* pool
> is unsupported on Windows, so start a worker with `-P threads`
> (`celery -A jobqueue.celery_app:app worker -Q emails -P threads -c 8`). The SIGKILL test needs POSIX
> process groups and is skipped on native Windows; the other 8 tests run. For the full suite use WSL2, Linux or macOS.

> Tests use **Redis DB 15** and refuse to run against DB 0, because each test flushes its DB.
> Different Redis? `export JOBQUEUE_TEST_REDIS_URL=redis://host:6379/15`.

## Run it for real

```bash
# terminal 1 – worker
make worker            # celery -A jobqueue.celery_app:app worker -Q emails -c 8

# terminal 2 – simulate the flash sale (2,000 jobs; drains at 200/min ≈ 10 min)
make burst
```

Enqueue from your own code:

```python
from jobqueue.tasks import enqueue_email
enqueue_email("a@b.com", "Order #42 confirmed", "Thanks!", job_id="order-42-confirmation")
```

Inspect / replay dead letters:

```bash
redis-cli LRANGE jobqueue:dead 0 -1
python -c "from jobqueue import dead_letter; from jobqueue.redis_client import get_redis; print(dead_letter.requeue_all(get_redis()))"
```

## What the tests prove

|              Test |                                                              Assertion |
|-------------------|------------------------------------------------------------------------|
| `test_500_jobs_…` | 500 jobs submitted → **none lost, none duplicated**; **rate limit never exceeded** (audited with Redis-side timestamps); intentional failures retried the exact expected number of times |
| `test_permanent_failures_…` | Exhausted / permanent failures are dead-lettered with their payload; replay works |
| `test_redelivered_message_…` | A re-delivered message is not sent twice |
| `test_limiter_outage_…` | Limiter down → **fails closed** (nothing sent), recovers afterwards |
| `test_atomic_…` / `test_window_slides_…` | Lua limiter is atomic under 64 concurrent threads; window truly slides |
| `test_sigkill_mid_run_…` | `kill -9` a real worker mid-send → restart → **all jobs delivered** |

Integration tests scale the limit to **100 per 2 s** (instead of 200 per 60 s) so they finish quickly;
the code path is identical.

## Layout

```
jobqueue/
  config.py        settings (env-overridable: JOBQUEUE_RATE_LIMIT, JOBQUEUE_REDIS_URL, …)
  celery_app.py    Celery config: acks_late, reject_on_worker_lost, visibility timeout, …
  rate_limiter.py  hand-written Redis sliding-window limiter (Lua, atomic) — no 3rd-party limiter
  tasks.py         send_email task: gate → send → retry/backoff → dead-letter; enqueue_email()
  providers.py     provider interface + Redis-backed mock (plug the real one in here)
  dead_letter.py   dead-letter list: push / list / requeue_all
tests/             pytest suite (real Redis, real Celery worker)
scripts/burst.py   flash-sale simulation
```

## Configuration

All via environment variables prefixed `JOBQUEUE_` (see `jobqueue/config.py`), e.g.
`JOBQUEUE_RATE_LIMIT=200`, `JOBQUEUE_RATE_WINDOW_S=60`, `JOBQUEUE_MAX_ATTEMPTS=6`,
`JOBQUEUE_VISIBILITY_TIMEOUT_S=300`. The mock provider reads `MOCK_PROVIDER_DELAY_S` and
`MOCK_PROVIDER_FAIL_RATE`.
