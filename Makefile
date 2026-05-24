.PHONY: install test demo serve clean docker docker-up docker-down lint

PYTHON ?= python3
VENV   := .venv
PIP    := $(VENV)/bin/pip
PYBIN  := $(VENV)/bin

help:
	@echo "Aegis Discovery — common tasks"
	@echo
	@echo "  make install      Create venv + install package + dev deps"
	@echo "  make test         Run the full test suite"
	@echo "  make demo         Reset DB, ingest samples, run pipeline, print dashboard"
	@echo "  make serve        Start the FastAPI server on http://127.0.0.1:8000"
	@echo "  make docker-up    Build and start the Docker container"
	@echo "  make docker-down  Stop the Docker container"
	@echo "  make clean        Remove venv, __pycache__, *.db, .pytest_cache"

install:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

test:
	$(PYBIN)/pytest -q

demo:
	rm -f aegis.db
	$(PYBIN)/aegis demo

serve:
	$(PYBIN)/aegis serve

docker-up:
	docker-compose up --build -d

docker-down:
	docker-compose down

clean:
	rm -rf $(VENV) .pytest_cache build dist *.egg-info aegis.db
	find . -type d -name __pycache__ -exec rm -rf {} +
