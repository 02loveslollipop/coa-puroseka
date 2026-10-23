.PHONY: prepare enrich test build status
prepare:
	.venv/bin/python scripts/prepare.py
enrich:
	.venv/bin/python scripts/enrich_once.py
test:
	.venv/bin/python -m pytest -q
build:
	npm --prefix web run build
status:
	.venv/bin/python scripts/aws.py status
