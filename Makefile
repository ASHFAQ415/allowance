# Allowance v4.2.2 Makefile

.PHONY: test test-unit test-integration seed dev docker-build docker-up

test:
	python -m unittest discover -s backend/tests -t . -p "test_*.py" -v

test-unit:
	python -m unittest discover -s backend/tests/unit -t . -p "test_*.py" -v

test-integration:
	python -m unittest discover -s backend/tests/integration -t . -p "test_*.py" -v

seed:
	python scripts/seed_db.py

dev-backend:
	uvicorn backend.app.api:app --reload --port 8000 --workers 1

dev-frontend:
	cd frontend && npm run dev

docker-build:
	docker compose build

docker-up:
	docker compose up -d

