# Section 2 — Design: Rate-Limited Async Job Queue

Code: [`section2_job_queue/`](section2_job_queue/) · Written SIGKILL answer: [`ANSWERS.md`](ANSWERS.md) (Section 2)

## 1. The problem in numbers

|                                    Fact |                                 Consequence |
|-----------------------------------------|---------------------------------------------|
| Provider allows **200 emails / minute** | Hard ceiling. Exceeding it risks throttling or suspension of the *whole* account (OTPs included). |
| Flash sale: **2,000 requests in < 10 s** | Arrival rate is ~1,200× the send rate. We cannot send them inline; we must **buffer**. |
| Drain time = 2,000 / 200 | **~10 minutes** to empty the queue. ~1,800 jobs wait. |
| Worker may crash mid-run | A job must survive a `kill -9` (see `ANSWERS.md`). |

So the system has four jobs: **accept fast, release slowly, retry failures, never lose anything.**

```
 web request ──enqueue_email()──▶  Redis list "emails"  (the buffer; survives restarts)
                                         │
                              Celery worker(s), N slots each
                                         │
                       ┌─────────────────▼──────────────────┐
                       │ 1. already sent? (jobqueue:sent:ID)│── yes ─▶ no-op (dedupe)
                       │ 2. limiter.acquire()  [Lua, atomic]│── denied ▶ re-schedule (ETA), NOT a failure
                       │ 3. provider.send(idempotency=ID)   │
                       └───────┬───────────────────┬────────┘
                         success                 failure
                  SET sent marker        transient: retry, exp. backoff + jitter
                                         permanent / attempts exhausted: ▶ DEAD-LETTER list
```

## 2. Architecture choice: Celery + Redis vs Django Q vs custom

| | **Celery + Redis** (chosen) | Django Q (Q2) | Custom (Redis list + worker loop) |
|-|-----------------------------|---------------|-----------------------------------|
| Crash safety | `acks_late`, `reject_on_worker_lost`, visibility timeout: mature and well documented | Supports ORM/Redis brokers; ack semantics less configurable and less battle-tested | Must build reliable-queue pattern (`BRPOPLPUSH`/processing list, reaper, heartbeats) myself — easy to get subtly wrong |
| Retries / backoff / ETA | Built in (`retry(countdown=…)`) | Basic retry count, no per-attempt backoff curve | Build it (delayed-job sorted set + poller) |
| Horizontal scale | Add workers; prefork/threads/gevent pools | Add clusters; fewer concurrency models | Whatever I write |
| Operational tooling | Flower, `celery inspect`, wide ecosystem & knowledge | Django-admin views (nice), small ecosystem | None |
| Coupling to Django | None (and the platform may not be Django-only) | Requires Django | None |
| Cost | Heavier dependency; Celery's Redis broker has the visibility-timeout quirks (below) | Lightest to set up for a Django monolith | Highest build + maintenance cost; the assignment's risk is in *reliability*, exactly where hand-rolled code is weakest |

**Decision: Celery + Redis.** The hard requirements are reliability primitives (ack-after-run, redelivery, delayed retry). Celery gives those battle-tested; Django Q would be a fine choice for a small Django-only app with low stakes but I have less confidence in its crash semantics under a burst; a custom queue would spend the whole time budget re-implementing Celery and still be less trustworthy. I **did** write the rate limiter myself, because that is the part Celery does not give correctly: Celery's built-in `rate_limit="200/m"` is **per worker process, in memory** — with 4 workers you would send 800/min. A global limit needs shared state, hence Redis.

Costs I accept: Celery's Redis transport has visibility-timeout behavior that needs care (§5), and Redis is a single point of failure for both queue and limiter (§4.3).

## 3. Rate limiter: sliding-window log (`section2_job_queue/jobqueue/rate_limiter.py`)

### 3.1 Why this approach (Option B) over A and C

|   | Token bucket (A) | **Sliding-window log (B, chosen)** | Fixed window (C) |
|---|------------------|------------------------------------|------------------|
| Guarantee | Average rate + burst up to bucket size | **At most 200 in *any* 60 s window** | 200 per calendar minute |
| Boundary problem | Allows `capacity` burst by design | None | **Up to 2× (400) across a minute boundary** (200 at 12:00:59 + 200 at 12:01:00) |
| Memory / op cost | O(1) | O(limit) = ≤200 members; trivial | O(1) |
| Accurate "retry in N s" hint | Computable | **Exact** (oldest entry + window − now) | Time to next boundary |

The provider's limit is a hard external constraint, so the *strongest guarantee* matters more than O(1) memory. Fixed window can send 2× the limit in a short span, which is exactly the kind of overshoot the limit exists to prevent. A token bucket can be configured safely (capacity 200, refill 200/60 s) but it deliberately permits bursts, and it needs fractional-token math to be exact. The sliding window log is the only option whose guarantee is literally the sentence in the requirements. At 200 entries the "O(n) memory" cost is irrelevant.

Mechanics: sorted set, member = unique id, score = admission time (ms). Per attempt: `ZREMRANGEBYSCORE` (drop expired) → `ZCARD` → `ZADD` if below the limit, else compute wait from `ZRANGE 0 0 WITHSCORES`.

### 3.2 Atomicity

Count-then-insert from the client is a race: 50 workers can all see `199` and all insert. The four Redis commands therefore run inside **one Lua script** (`EVALSHA`). Redis executes scripts single-threaded and atomically, so no command from another client can interleave. (`MULTI/EXEC` alone is not enough: the decision depends on the `ZCARD` *result*, and a transaction cannot branch on an intermediate result.)

* **Clock:** the script calls Redis `TIME`, so all workers share one clock; skew between worker machines cannot break the guarantee.
* **Verified by:** `test_atomic_under_heavy_concurrency` (64 threads × 20 calls, limit 50 → exactly 50 admitted) and the 500-job test, which audits admission timestamps recorded *inside* the script.

### 3.3 What happens when Redis fails — fail **closed**

If the limiter cannot reach Redis (timeout, `READONLY` during failover, OOM…), `acquire()` **raises** and the task **does not send**; it re-schedules itself after `limiter_error_retry_s` (+ jitter) *without* consuming a failure attempt.

Why closed rather than open: failing open during a Redis incident means every worker sends at full speed, and a flash-sale backlog of 1,800 emails would blast past 200/min and risk suspension of the provider account, which also carries OTPs and alerts. The cost of failing closed is *delay*, which is recoverable; the cost of failing open is an account suspension, which is not. Redis socket timeouts are 2 s so a hung Redis fails quickly instead of freezing worker slots. Verified by `test_limiter_outage_fails_closed_then_recovers` and `test_fails_closed_when_redis_is_unreachable`.

Honest limit: Redis is also the broker. In a *total* Redis outage nothing is consumed at all, and a task that fails to publish its own retry relies on the visibility-timeout recovery (§5). Production mitigation: Redis with AOF + replica/Sentinel.

### 3.4 Details that matter

* **A denied job is not a failed job.** It is re-scheduled with `countdown = retry_after + jitter` and its failure-attempt counter is untouched, so a long wait in line can never push a job into the dead-letter list.
* **Jitter** (0–`rate_jitter_s`) spreads the ~1,800 waiting jobs so they do not wake in the same millisecond.
* **A failed provider attempt still consumes a slot.** Conservative on purpose: a request that timed out may still have been counted by the provider.
* **No `time.sleep()`** anywhere: waiting is done by the broker's ETA scheduling, so a worker slot is never parked.

## 4. Reliability design

### 4.1 Retries and dead letter

* `TransientEmailError` / any other exception → `attempt += 1`, delay = exponential backoff (`5 s, 10 s, 20 s, 40 s, 80 s`, capped at 120 s) with *equal jitter* (half fixed, half random) so a provider outage is not followed by a synchronized retry stampede.
* After `max_attempts` (6) **or** on `PermanentEmailError` (e.g. invalid address — retrying cannot help) → pushed, with payload, error and attempt count, to the Redis list `jobqueue:dead`. `dead_letter.requeue_all()` replays them with a fresh budget once the cause is fixed. A job leaves the list only after it has been re-published.
* Dead letters live in Redis (durable, inspectable with `redis-cli`), not in logs.

### 4.2 Delivery guarantee: at-least-once + dedupe ≈ exactly-once

Not losing jobs means accepting redelivery, so duplicates are possible *by design*. Two layers neutralize them:

1. `jobqueue:sent:<job_id>` marker (7-day TTL), checked **before** consuming a rate-limit slot. Covers: crash after the send but before the ack.
2. `idempotency_key = job_id` passed to the provider. Covers the one remaining window: crash *between* the provider accepting the email and our marker being written. (Needs a provider that honors idempotency keys; SES/SendGrid-style APIs offer one. If the chosen provider has none, a rare duplicate email is the price of never losing an OTP, and I think that's the right trade for transactional mail.)

Known gap: two workers processing the *same* job id simultaneously could both pass step 1. This needs a redelivery while the original is still running (visibility timeout shorter than a task run); I keep timeout ≫ task time limit rather than adding a distributed lock whose own crash-recovery would be a new failure mode.

### 4.3 Celery settings and why (`section2_job_queue/jobqueue/celery_app.py`)

|               Setting |                                                                     Why |
|-----------------------|-------------------------------------------------------------------------|
| `task_acks_late=True` | Ack after the task finishes → a crashed worker's message is redelivered |
| `task_reject_on_worker_lost=True` | Pool child killed (OOM) → requeue immediately |
| `task_acks_on_failure_or_timeout=False` | An *unhandled* error or hard time-limit kill must not silently ack (= lose) the job; it stays un-acked and is redelivered |
| `worker_prefetch_multiplier=1` | A worker reserves only what it can run; less hoarding, less exposure to a crash |
| `visibility_timeout=300` + `unacked_restore_throttle=1` | See §5 |
| soft 30 s / hard 45 s time limit | A hung provider call becomes a normal retry instead of an eternal slot leak |
| `task_ignore_result=True` | Fire-and-forget; no result backend load |

## 5. The visibility-timeout trade-off (the subtle part)

Redis has no native "un-acked message" concept; kombu emulates it: a delivered message sits in an `unacked` hash and is **restored to the queue if it is still there after `visibility_timeout`**. This single knob pulls two ways:

* **Too short** → tasks that are *legitimately held* (my ETA retries wait up to `rate_window + jitter` = 65 s, backoff up to 120 s) get redelivered while still waiting → duplicate attempts.
* **Too long** → after a `kill -9` of a whole worker, its in-flight jobs sit invisible for that long before another worker picks them up.

I resolve it by **bounding every hold** (waits ≤ 65 s, backoff ≤ 120 s, task ≤ 45 s) and setting `visibility_timeout = 300 s`; `Settings.warnings()` logs if the numbers are inconsistent. Expected worst-case recovery after a crash ≈ 300 s + one 10 s sweep tick.

**Gotcha I measured rather than assumed:** kombu only sweeps for expired un-acked messages every `unacked_restore_interval × unacked_restore_throttle` — by default 10 s × 10 = **~100 s**. With `visibility_timeout=5` in the crash test, recovery took **102 s**; setting `unacked_restore_throttle=1` brought it to **12 s**.

## 6. Capacity and behaviour under the flash sale

* Publishing 2,000 jobs is a few hundred ms (the web request only does an `LPUSH`); the test asserts the burst enqueues in < 10 s.
* ~200 jobs/min are sent; ~1,800 wait. Each waiting job re-polls the limiter about once a minute → on the order of 9,000 cheap Lua calls over the 10-minute drain (≈15/s). Negligible for Redis.
* Worker concurrency does **not** change throughput (the limiter is the bottleneck); it only needs to be high enough to send 200/min with the provider's latency (e.g. 4 slots × 1 s latency ≈ 240/min).
* Admission order is **not FIFO** among waiting jobs (whoever wakes first after a slot frees wins).

## 7. Known limitations / what I would do next

1. **Priority lanes.** An OTP created during the sale waits behind ~1,800 order emails (up to ~9 minutes) — unacceptable for a login code. Fix: separate queues (`otp`, `transactional`, `bulk`) and reserve part of the 200/min budget for the OTP lane (e.g. a second limiter key or a quota split). I did not build this because the assignment specifies a single limit, but it is the first thing I would raise with the team.
2. **Reservation-style limiter.** Replacing "retry and hope" with a script that hands each job a concrete future slot would give FIFO fairness and remove the ~9,000 denied polls, at the cost of more complex state.
3. **Poison messages that crash the worker** (segfault/OOM on every attempt) would be redelivered every visibility timeout forever. Track a delivery counter and dead-letter after N.
4. **Durable broker:** run Redis with `appendonly yes` + replica/Sentinel (the compose file enables AOF); otherwise a Redis restart can lose queued jobs.
5. **Enqueue atomicity:** when called from a DB transaction, enqueue in `transaction.on_commit` (or use an outbox table) so a rolled-back order never emails the customer.
6. Metrics/alerts: queue depth, oldest-job age, dead-letter count, limiter denial rate.

## 8. How it is tested (`section2_job_queue/tests/`)

|              Test |                                                    What it proves |
|-------------------|-------------------------------------------------------------------|
| `test_500_jobs_…` | 500 jobs through a real worker + real Redis: **none lost, none duplicated**, **rate limit never exceeded** (audited with Redis-side timestamps; every provider call passed the limiter), intentional failures retried the exact expected number of times |
| `test_permanent_failures_…` | Exhausted and permanent failures land in the dead-letter list with payload, healthy jobs unaffected, replay works |
| `test_redelivered_message_…` | A duplicate delivery is a no-op |
| `test_limiter_outage_…` | Limiter failure → fail closed, nothing sent, then recovers |
| `test_atomic_…`, `test_window_slides_…` | Lua atomicity; true sliding (not fixed) window |
| `test_sigkill_mid_run_…` | `kill -9` a real worker mid-send, restart, **all jobs delivered** |

The integration tests run with 100 emails / 2 s instead of 200 / 60 s so the same code paths run in seconds.
