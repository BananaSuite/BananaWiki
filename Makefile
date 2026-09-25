PYTHON ?= .venv/bin/python

.PHONY: help setup dev start test hosting-dev hosting-start hosting-test source build-easy-deployment

help:
	@echo "setup                 Create a virtual environment and install dependencies"
	@echo "dev / start           Run a single wiki locally in development / Gunicorn"
	@echo "hosting-dev / hosting-start   Run the hosting portal"
	@echo "test / hosting-test   Run tests"
	@echo "source                Archive the committed source"
	@echo "Managed server: sudo ./banana install --mode wiki (or hosting)"
	@echo "Maintenance: sudo bananawiki update / backup / restore / uninstall"
	@echo "Automatic updates remain off until explicitly enabled"
	@echo "build-easy-deployment Build the portable launcher with PyInstaller"

setup:
	python3 -m venv .venv
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r requirements.txt

dev:
	./dev.sh

start:
	./start.sh

test:
	$(PYTHON) -m pytest tests -q

hosting-dev:
	./hosting/dev.sh

hosting-start:
	./hosting/start.sh

hosting-test:
	$(PYTHON) -m pytest tests/test_hosting*.py tests/test_container_runtime.py -q

source:
	mkdir -p dist
	git archive --format=tar.gz --prefix=BananaWiki/ --output=dist/BananaWiki-source.tar.gz HEAD

build-easy-deployment:
	$(PYTHON) scripts/build_easy_deployment.py
