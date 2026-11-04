# Full-Stack Developer Assessment — Biz-Tech Analytics

**Live Demo:** [Biz-Tech Assessment](https://biz-tech-assessment.onrender.com/)


|Section|                                            What |                        Where |                   Run |
|-------|-------------------------------------------------|------------------------------|-----------------------|
|     1 | Diagnose a broken system (N+1 query regression) | this folder (Django project) | see "Section 1" below |
|     2 | Rate-limited async job queue (Celery + Redis)   | [`section2_job_queue/`](section2_job_queue/README.md) | `cd section2_job_queue` and follow its README |
|     3 |                     Written architecture review |   [`ANSWERS.md`](ANSWERS.md) |             read only |

Written answers for all sections: **[ANSWERS.md](ANSWERS.md)**. Section 2 architecture decisions: **[DESIGN.md](DESIGN.md)**.

### Live Demo (Render)

- [Live Application](https://biz-tech-assessment.onrender.com/)
- [Silk Profiler Dashboard](https://biz-tech-assessment.onrender.com/silk/)
- [Original N+1 API](https://biz-tech-assessment.onrender.com/api/orders/summary/)
- [Optimized API](https://biz-tech-assessment.onrender.com/api/orders/summary-fixed/)
- [Django Admin](https://biz-tech-assessment.onrender.com/admin/)

The two code sections are independent projects with their own `requirements.txt`; use a separate virtualenv for each.

---

# Section 1: Diagnose a Broken System (N+1 query regression)

A Django API endpoint `/api/orders/summary/` got very slow for users with 200+ orders.
This repo reproduces the problem, fixes it, and proves it with django-silk.

Written answers (investigation log, root cause, why the fix works): see **ANSWERS.md**

## Run it (about 2 minutes)

Tested with Python 3.12, Django 6.1, DRF 3.18, django-silk 5.6 (should work on Django 4.2+)

```bash
python -m venv venv
venv\Scripts\activate          
pip install -r requirements.txt

python manage.py migrate          # creates the SQLite db
python manage.py seed_orders      # heavy_user = 250 orders, light_user = 20 orders
python manage.py measure_queries  # prints BEFORE / AFTER numbers recorded by django-silk
python manage.py test             # 5 tests, all should pass
```

Optional: see the same data in the silk web UI.

```bash
python manage.py runserver
# open http://localhost:8000/silk/  (requests table shows SQL query count + time)
```

## What is where

|                    File |                 Purpose |
|-------------------------|-------------------------|
| `orders/models.py`      | `Order` and `OrderItem` |
| `orders/serializers.py` | `OrderSummarySerializer` = the regression, `OrderSummaryFastSerializer` = the fix |
| `orders/views.py`       | `/summary/` (broken) and `/summary-fixed/` (fixed) side by side |
| `orders/management/commands/seed_orders.py` | demo data |
| `orders/management/commands/measure_queries.py` | before/after numbers from django-silk |
| `orders/tests.py`       | proves N+1 exists, fix is constant-query, output is identical |
| `evidence/before_after_silk.txt` | saved output of `measure_queries` |

## Result

|       user |        endpoint |orders| SQL queries | time (ms, local SQLite) |
|------------|-----------------|------|-------------|-------------------------|
| heavy_user | before (broken) | 250  |          753|                  2078   |
| heavy_user | after (fixed)   | 250  |            3|                     54  |
| light_user | before (broken) | 20   |          63 |                     207 |
| light_user | after (fixed)   | 20   |           3 |                      31 |

(Times are local SQLite, so they are small. See ANSWERS.md for why production was 30s+.)

**Live Deployment:** Hosted on Render using Django, Gunicorn, WhiteNoise and django-silk.

No `.env` or secrets are committed. `SECRET_KEY` comes from `DJANGO_SECRET_KEY` (a dev-only default is used if unset).


