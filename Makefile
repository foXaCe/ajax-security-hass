# Mirrors the jobs in .github/workflows/ci.yml. No shared source with CI —
# if ci.yml changes, update these targets to match.

.PHONY: lint typecheck test coverage check deploy-dev

lint:
	ruff check .
	ruff format --check .
	bandit -r custom_components/ajax/ -c pyproject.toml
	codespell custom_components/ajax/

typecheck:
	mypy custom_components/ajax/

test:
	python -m pytest tests/ -q

coverage:
	python -m pytest tests/ --cov=custom_components/ajax --cov-report=term-missing

check: lint typecheck test

deploy-dev:
	sudo rm -rf /home/stephane/homeassistant/config/custom_components/ajax
	sudo cp -r custom_components/ajax /home/stephane/homeassistant/config/custom_components/
	sudo chown -R stephane:stephane /home/stephane/homeassistant/config/custom_components/ajax
	docker restart homeassistant
