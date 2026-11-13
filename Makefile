.PHONY: install test cov lint check serve demo examples clean worker

install:
	pip install -e ".[dev]"

test:
	pytest

cov:
	pytest --cov=decision_platform --cov-report=term-missing

lint:
	ruff check src tests scripts
	ruff format --check src tests scripts
	mypy src

check: lint test

serve:
	dp serve --port 8000

worker:
	dp worker --interval 10

demo:
	./scripts/demo.sh

examples:
	python scripts/generate_examples.py

clean:
	rm -rf data .pytest_cache .coverage build *.egg-info src/*.egg-info
	find . -name __pycache__ -type d -exec rm -rf {} +
