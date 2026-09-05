.PHONY: install run bench all json list api gen clean

VENV := .venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip

install:                       ## create venv + install deps
	python3 -m venv $(VENV)
	$(PIP) install -q --upgrade pip
	$(PIP) install -q -r requirements.txt

run:                           ## all scenarios, 1M synthetic rows
	$(PY) engine.py

all: run

bench:                         ## stress: 5M rows, one scenario
	$(PY) engine.py --rows 5000000 --scenario urban_multi_capture

json:                          ## machine-readable output
	$(PY) engine.py --scenario urban_multi_capture --json

list:                          ## list scenarios
	$(PY) engine.py --list-scenarios

gen:                           ## write a synthetic parquet to feed --data / API
	$(PY) engine.py --rows 1000000 --gen candidates.parquet

api:                           ## serve REST API on :8000
	$(VENV)/bin/uvicorn api:app --host 0.0.0.0 --port 8000

clean:
	rm -rf $(VENV) __pycache__ candidates.parquet
