.PHONY: install migrate serve worker dev test lint backup stats

install:
	python3.12 -m venv .venv
	./.venv/bin/pip install -r requirements.txt

migrate:
	./.venv/bin/alembic upgrade head

serve:
	./.venv/bin/python -m app.cli serve --reload

worker:
	./.venv/bin/python -m app.worker

dev:
	./start.sh

test:
	./.venv/bin/pytest -q

lint:
	./.venv/bin/ruff check .

backup:
	./.venv/bin/python -m app.cli backup

stats:
	./.venv/bin/python -m app.cli stats
