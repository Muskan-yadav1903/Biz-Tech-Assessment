# ANSWERS.md

## Section 1: Diagnose a Broken System

### My interpretation of the scenario
The task says "no code change was made to that view". I read this as: the view file is untouched, but the
deployment changed something else on the request path. In my reproduction, the deployment added two fields
(`item_count`, `total`) to the **serializer** that the unchanged view uses. The view's git history looks clean,
so nobody suspected it. I built this scenario myself (as the task asks), and the numbers below come from that reproduction.

### 1. Incident investigation log

**Step 1: Look at the shape of the failure before touching any code.**
Only users with more than 200 orders time out; everyone else is fine. That means the cost grows with the number of
rows per user. A crashed dependency or bad config would hurt every user equally, so I stopped looking at infrastructure and started looking for something that runs *once per order*.

**Step 2: Check what the deployment really changed, not only the view.**
"The view did not change" only proves one file is unchanged. A DRF request goes through
view -> queryset -> serializer -> renderer, plus middleware and settings. So I diffed the whole release: serializers, models, signals, middleware, settings and migrations. The diff showed two new `SerializerMethodField`s in `OrderSummarySerializer` and a new `user.username` field. That is the first concrete suspect.

**Step 3: Decide between "one slow query" and "many fast queries" using the database.**
These two look the same to a user (timeout) but have different causes. I checked query logging / the slow query log:
- one query taking seconds -> missing index or bad plan (`EXPLAIN ANALYZE` would show a Seq Scan)
- hundreds of near-identical tiny queries -> N+1

**Step 4: Reproduce locally with a profiler.**
I seeded a user with 250 orders and called the endpoint with django-silk on. Silk recorded **753 SQL queries for one request**. The queries were the same three statements repeated 250 times (user lookup, `COUNT(*)` on items, `SELECT` of items).
That confirmed many-fast-queries, not one slow query.

**Step 5: Rule out the other candidates (so the diagnosis is not a guess).**
- *Missing index:* `Order.user` is a `ForeignKey`, and Django creates an index on every ForeignKey automatically. The release had no migration touching indexes. The queries were also fast individually.
- *Cache invalidation:* no cache is used on this path, so nothing to invalidate.
- *Serializer CPU overhead:* the time was spent waiting on SQL (753 round trips), not in Python.
- *Connection settings:* a new DB connection per query would show up as a connection log, not repeated identical SELECTs.

### 2. Root cause category and justification
**N+1 query** (caused by lazy evaluation in the serializer)

**Justification**: the number of queries is `3N + 3` (753 for 250 orders, 63 for 20 orders). It scales linearly with the number of orders, which matches "only users with 200+ orders". Each query alone is fast; the total is slow.
On my local SQLite there is no network, so 753 queries take ~0.6 s. On a real server the DB is across the network; at roughly 40 ms per round trip, 753 queries is about 30 seconds (753 x 0.04 s = 30.1 s). That is an estimate, not a production measurement, but it matches the reported 30 s+ timeouts.

### 3. The code that demonstrates the problem
`orders/serializers.py`, class `OrderSummarySerializer`, used by `OrderSummaryView` (`/api/orders/summary/`):
- `source="user.username"` -> lazy load of the ForeignKey, 1 query per order
- `order.items.count()` -> a new `SELECT COUNT(*)` per order
- `order.items.all()` -> a new `SELECT ... FROM orders_orderitem WHERE order_id = X` per order

The view itself is just `Order.objects.filter(user=request.user)`. That queryset is lazy and returns only orders; every related lookup happens later, inside the serializer loop, once per row.

### 4. The fix and why it works at the DB / ORM level
`OrderSummaryFixedView` (`/api/orders/summary-fixed/`):

```python
Order.objects.filter(user=request.user)
    .select_related("user")
    .annotate(
        item_count=Count("items"),
        total=Coalesce(Sum(F("items__price") * F("items__quantity")), Value(Decimal("0.00"))),
    )
```
and `OrderSummaryFastSerializer`, which only reads plain attributes.

- **`select_related("user")`** makes Django add `INNER JOIN auth_user` to the main query and fill `order.user` from the same row.
  Reading `order.user.username` later does not touch the DB (the FK object is already cached on the instance).
- **`annotate(Count, Sum)`** makes Django add a `LEFT OUTER JOIN orders_orderitem` with `GROUP BY orders_order.id ...`. The database
  computes the count and sum while scanning the joined rows, so Python no longer loads every `OrderItem` just to add numbers.
  `F("items__price") * F("items__quantity")` is evaluated per item row inside SQL, then summed.
- **`Coalesce(..., 0.00)`** handles an order with no items: `SUM` over no rows is `NULL` in SQL, and `COALESCE` turns it into `0.00`
  (covered by a test).
- Result: **one query for all orders** instead of `3N + 1`. Total per request is 3 queries (session, auth user, orders), and it does not grow with the number of orders (test `test_fixed_endpoint_uses_constant_queries` checks 250 orders and 20 orders give the same count).
- `coerce_to_string=False` on the serializer's `total` field keeps the JSON a number, like the old endpoint. My first version returned `"84.00"` (a string) and the "identical output" test caught it, which would have broken the mobile app.

Alternative I considered: `prefetch_related("items")`. It also fixes N+1 (2 queries: orders, then items with `WHERE order_id IN (...)`), but it still loads every item into Python memory just to count and sum them. For a summary endpoint, `annotate` is cheaper.
`prefetch_related` is better when the response needs the item rows themselves.

### 5. Profiler evidence (django-silk)
Output of `python manage.py measure_queries` (read from django-silk's own `Request` records, also visible at `/silk/`):

|       user |        endpoint |orders| SQL queries | time (ms, local SQLite) |
|------------|-----------------|------|-------------|-------------------------|
| heavy_user | before (broken) | 250  |          753|                  2078   |
| heavy_user | after (fixed)   | 250  |            3|                     54  |
| light_user | before (broken) | 20   |          63 |                     207 |
| light_user | after (fixed)   | 20   |           3 |                      31 |

Saved copy: `evidence/before_after_silk.txt`

### 6. What this fix does not cover (trade-offs)
- The endpoint is still **not paginated**. A user with 50,000 orders would still get a huge response. Pagination is the next step.
- `annotate` with GROUP BY over a big `orderitem` table needs the index on `OrderItem.order_id`. Django creates it automatically
  because it is a ForeignKey, but it is worth checking with `EXPLAIN` on real data.
- To stop this coming back, `test_fixed_endpoint_uses_constant_queries` fails if anyone adds a per-row query to this endpoint again.
  Running silk (or debug-toolbar) on staging would also have shown the 700+ queries before release.




## Section 2: Design a Rate-Limited Async Job Queue

Code, tests and run instructions: `section2_job_queue/` (see its README). Architecture decisions and trade-offs
(Celery + Redis vs Django Q vs custom, token bucket vs sliding window vs fixed window, fail-open vs fail-closed):
see **DESIGN.md** in the repo root.

### Written question: what happens to in-flight tasks if the Celery worker process is SIGKILLed?

> *Question as I read it from the assessment: "What happens to in-flight tasks if the Celery worker process is killed with SIGKILL, and how do you make sure none are lost?"*

** answer:** with Celery's *defaults* the in-flight tasks are **lost**. With the configuration below they are **redelivered to another worker** after at most `visibility_timeout` (+ one 10 s sweep tick), so recovery is *delayed*, not instant, and a dedupe marker makes the redelivery safe.

#### 1. Why the default loses jobs

By default Celery acknowledges (`ACK`) a message **when the worker receives it, before the task runs** (`acks_early`). Once acked, Redis forgets the message. If the process is then `kill -9`'d, nothing remembers the job. SIGKILL cannot be caught, so there is no shutdown hook, no `finally`, no chance to clean up.

#### 2. Configuration that prevents the loss

| Setting (`section2_job_queue/jobqueue/celery_app.py`) |                            Effect on a SIGKILL |
|-------------------------------------------------------|------------------------------------------------|
|    **`task_acks_late = True`**  | The message is acked only **after** the task finishes. A killed worker never acks, so the message stays in Redis' `unacked` set and is eligible for redelivery. |
| **`task_reject_on_worker_lost = True`** | Covers the other crash shape: only a *pool child* is killed (typically the Linux OOM-killer) while the main process survives. The main process detects `WorkerLostError` and **requeues immediately**. Without it, an `acks_late` task whose child died is dropped. |
| **`broker_transport_options.visibility_timeout = 300`** | Covers the **whole-process** SIGKILL (or a dead machine). Nobody is left to reject the message, so the Redis transport puts any message that has been un-acked for longer than this back on the queue, and another worker consumes it. |
| **`unacked_restore_throttle = 1`** | kombu only checks for expired un-acked messages every `interval x throttle` (default 10 s x 10 = about **100 s**). I measured 102 s of extra recovery delay in the crash test with the default; with throttle 1 it was 12 s. |
| **`task_acks_on_failure_or_timeout = False`** | A task killed by the *hard time limit* or that raised an unhandled exception is **not** acked, so it is redelivered rather than silently dropped. (All expected failures are handled in code by retry / dead-letter.) |
| **`worker_prefetch_multiplier = 1`** | A worker reserves only as many messages as it has free slots, so a crash strands fewer jobs and other workers are not starved by a hoarding one. |

(Messages that were *prefetched but not yet started* are also un-acked, so they follow exactly the same recovery path.)

#### 3. The trade-off to be aware of: visibility timeout

`visibility_timeout` is a dial between two failure modes:

* **Too short:** my rate-limit retries are held by the worker with an ETA (up to about 65 s) and backoff retries (up to 120 s). If the timeout were shorter than that, those *healthy, waiting* tasks would be redelivered and attempted twice.
* **Too long:** after a SIGKILL the lost jobs wait that long before anyone retries them.

So every hold is bounded (at most 65 s rate-limit wait, 120 s backoff, 45 s task time limit) and the timeout is set above them at 300 s. `Settings.warnings()` logs a warning if the values become inconsistent. **Worst-case recovery after a crash is about 300 s + 10 s.** This is the price of using Redis as a broker; RabbitMQ would redeliver immediately on connection loss.

#### 4. Consequence: at-least-once, so duplicates must be handled

A job killed *after* the provider accepted the email but *before* the ack will run again. Two guards:

1. A `jobqueue:sent:<job_id>` marker is checked before sending, so a repeat delivery is a no-op.
2. `job_id` is passed to the provider as an **idempotency key** to cover the tiny window between "provider accepted" and "marker written".

For OTP and order mail I deliberately choose *at-least-once* over *at-most-once*: a rare duplicate is far better than a lost login code.

#### 5. What this does **not** protect against

If **Redis itself** loses data (restart without persistence), queued and un-acked jobs go with it. Run Redis with AOF (`appendonly yes`, enabled in `section2_job_queue/docker-compose.yml`) and a replica/Sentinel in production.

#### 6. Proof, not just theory

`section2_job_queue/tests/test_sigkill_recovery.py` starts a real `celery worker` subprocess with 12 queued jobs (each send takes 1.5 s), waits until sends are in flight, **`kill -9`s the entire process group** (main process + pool children), starts a second worker, and asserts that **all 12 jobs are delivered**, including the ones that were mid-flight at the moment of the kill (observed: 4 in flight, all recovered; about 12 s with a 5 s visibility timeout).




## Section 3: Written Architecture Review

## Question A — Django Admin Performance

If a Django admin page becomes slow with 500,000+ records, I would investigate the following three areas:

**1. Expensive database count queries**

Django Admin uses pagination, which requires counting records. On a table with millions of rows, counting can become expensive, especially when filters are applied.

I would first check the queries generated by the admin page. I would set `show_full_result_count = False` in `ModelAdmin` to avoid the additional full-table count. I would also keep `list_per_page` at a reasonable value, such as 50. If exact counts are not essential, I would consider a custom paginator that uses an estimated count.

**2. N+1 queries in list_display**

If the admin list displays related objects, Django may execute separate queries for each record. For example, accessing `order.customer.name` for every order can create unnecessary database queries.

I would use `list_select_related = ('customer',)` in `ModelAdmin` to fetch related ForeignKey objects using SQL JOINs. For ManyToMany relationships, I would use `prefetch_related()` through `get_queryset()`.

**3. Expensive search, filters and ordering**

Searching or sorting on frequently used fields without suitable indexes can make database queries slow. I would inspect the SQL queries using `EXPLAIN` and identify expensive filters and sorting operations.

I would add appropriate indexes using `db_index=True` or `Meta.indexes`. For text searches using `icontains` on PostgreSQL, I would consider a trigram index instead of relying on a normal B-tree index.

Overall, I would use Django Silk and database query analysis to identify the actual bottleneck before making changes.

## Question B — Pagination Trade-offs

Offset-based and cursor-based pagination solve the same problem but behave differently when an API handles thousands of records.

**1. Offset-based pagination**

Django REST Framework provides `LimitOffsetPagination` and `PageNumberPagination` for this approach.

It is easy to implement and allows users to jump directly to a particular page. However, at large offsets, the database may need to scan and skip many rows before returning the requested records. This can become slow as the dataset grows.

Another problem is data mutation. If new records are inserted or existing records are deleted between requests, users may see duplicate or missing records while navigating pages.

**2. Cursor-based pagination**

DRF provides `CursorPagination`, which uses a cursor to identify the position of the next result instead of skipping a large number of rows.

It performs better for large datasets because the database can continue from the previous position using an indexed ordering field. I would use a stable ordering such as `created_at` with `id` as a tie-breaker and create a matching database index.

Cursor pagination is especially useful for mobile applications with infinite scrolling because users can continuously load more records without requesting large offsets.

However, it does not support jumping directly to an arbitrary page, which makes it less suitable for interfaces that require page numbers.

**My choice:** For a mobile app returning 10,000 records, I would choose cursor-based pagination because it provides better performance at scale and handles new records more consistently during scrolling. For admin dashboards or reports where users need to jump between pages, I would prefer offset-based pagination.