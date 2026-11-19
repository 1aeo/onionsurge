PYTHON ?= python3
NODE ?= node
.PHONY: build build-live catalogue check previews serve

ifneq ($(filter catalogue,$(MAKECMDGOALS)),)
check: catalogue
endif

build:
	$(PYTHON) scripts/build.py

catalogue:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) scripts/build_catalogue.py

build-live:
	$(PYTHON) scripts/build.py --live --output dist-live

check: build
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) scripts/build_catalogue.py --check
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest discover -s tests
	$(NODE) --check site/app.js
	$(NODE) --check site/live.js
	$(NODE) tests/test_display.cjs
	$(NODE) --test tests/test_live.cjs
	$(PYTHON) scripts/package_previews.py

previews: build
	$(PYTHON) scripts/package_previews.py

serve: build
	$(PYTHON) -m http.server 8873 --bind 127.0.0.1 --directory dist
