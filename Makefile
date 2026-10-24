.PHONY: install test lint demo clean

install:
	pip install -r requirements-dev.txt

test:
	pytest -q

lint:
	ruff check .

demo:
	python examples/make_sample_tree.py
	python retention_archiver.py sandbox/warehouse --report sandbox/plan.csv

clean:
	rm -rf sandbox .pytest_cache .ruff_cache __pycache__ tests/__pycache__
