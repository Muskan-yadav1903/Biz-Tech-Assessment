PY := .venv/bin/python

.PHONY: install redis test worker burst clean

install:
	python3 -m venv .venv
	$(PY) -m pip install -q -r requirements.txt

redis:
	docker compose up -d --wait redis

test: install redis
	$(PY) -m pytest

worker:
	$(PY) -m celery -A jobqueue.celery_app:app worker -Q emails -c 8 -l INFO

# Simulate the flash sale: 2,000 jobs at once (takes ~10 min to drain at 200/min).
burst:
	$(PY) -m scripts.burst --n 2000

clean:
	docker compose down -v
	rm -rf .venv .pytest_cache
