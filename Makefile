
.PHONY: help setup sync-inter sync-inter-all init-astr build-woff2 converter builder test proof clean update-project-template update

help:
	@echo "###"
	@echo "# Build targets for Astr"
	@echo "###"
	@echo
	@echo "  make setup:  Installs the font build dependencies"
	@echo "  make init-astr:  Converts Astr.glyphspackage into the complete Astr source"
	@echo "    Optional (decimals accepted): INTER_SCALE=100% ASTR_BASELINE=11.856 ASTR_MASTER_WEIGHTS=262.5,365.8,425.2,470.9,516.3,601.5 ASTR_EXPORT_WEIGHTS=262.5,300,400,500,601.5"
	@echo "  make sync-inter:  Fetches current Inter and updates changed merged data"
	@echo "  make sync-inter-all:  Fetches current Inter and reapplies every Inter glyph"
	@echo "  make build:  Builds the fonts and places them in the fonts/ directory"
	@echo "    Optional: BUILD_JOBS=1 lowers peak memory; default is 2"
	@echo "  make build-woff2:  Compresses existing variable and static TTFs to WOFF2"
	@echo "  make test:   Tests the fonts with fontbakery"
	@echo "  make proof:  Creates HTML proof documents in the proof/ directory"
	@echo

build: build.stamp

INTER_REPOSITORY_URL ?= https://github.com/rsms/inter.git
GLYPHS_SOURCE ?= sources/Astr.glyphspackage
INTER_SCALE ?= 100%
ASTR_BASELINE ?= 0
ASTR_MASTER_WEIGHTS ?= 225,325,400,425,475,550
ASTR_EXPORT_WEIGHTS ?= 225,300,400,500,550
BUILD_JOBS ?= 2

sync-inter: venv
	. venv/bin/activate; python sources/sync_inter.py --source "$(GLYPHS_SOURCE)" --repository "$(INTER_REPOSITORY_URL)"

sync-inter-all: venv
	. venv/bin/activate; python sources/sync_inter.py --source "$(GLYPHS_SOURCE)" --repository "$(INTER_REPOSITORY_URL)" --force

init-astr: venv
	. venv/bin/activate; python sources/sync_inter.py --source "$(GLYPHS_SOURCE)" --repository "$(INTER_REPOSITORY_URL)" --initialize --force --scale "$(INTER_SCALE)" --astr-baseline "$(ASTR_BASELINE)" --master-weights "$(ASTR_MASTER_WEIGHTS)" --export-weights "$(ASTR_EXPORT_WEIGHTS)"

build-woff2: venv
	. venv/bin/activate; python sources/build_woff2.py

setup: venv

venv: venv/touchfile

venv-test: venv-test/touchfile

converter: venv
	find sources/masters -mindepth 1 -maxdepth 1 ! -name .DS_Store -exec rm -rf {} +
	. venv/bin/activate; glyphs2ufo --generate-GDEF "$(GLYPHS_SOURCE)" -m sources/masters
	. venv/bin/activate; python sources/prepare_build_sources.py

builder: venv
	find fonts -mindepth 1 -maxdepth 1 ! -name .DS_Store -exec rm -rf {} +
	. venv/bin/activate; gftools builder --no-ninja sources/config_variable.yaml
	. venv/bin/activate; ninja -C sources -f build.ninja -j "$(BUILD_JOBS)"
	. venv/bin/activate; gftools builder --no-ninja sources/config_static.yaml
	. venv/bin/activate; python sources/use_prebuilt_static_instances.py sources/build.ninja
	. venv/bin/activate; ninja -C sources -f build.ninja -j "$(BUILD_JOBS)"
	. venv/bin/activate; python sources/post.py
build.stamp: venv converter builder

venv/touchfile: requirements.txt
	test -d venv || python3 -m venv venv
	. venv/bin/activate; pip install -Ur requirements.txt
	touch venv/touchfile

venv-test/touchfile: requirements-test.txt
	test -d venv-test || python3 -m venv venv-test
	. venv-test/bin/activate; pip install -Ur requirements-test.txt
	touch venv-test/touchfile

test: venv-test build.stamp
	TOCHECK=$$(find fonts/variable -type f 2>/dev/null); if [ -z "$$TOCHECK" ]; then TOCHECK=$$(find fonts/ttf -type f 2>/dev/null); fi ; . venv-test/bin/activate; mkdir -p out/ out/fontbakery; fontbakery check-googlefonts -l WARN --full-lists --succinct --badges out/badges --html out/fontbakery/fontbakery-report.html --ghmarkdown out/fontbakery/fontbakery-report.md $$TOCHECK  || echo '::warning file=sources/config.yaml,title=Fontbakery failures::The fontbakery QA check reported errors in your font. Please check the generated report.'

proof: venv build.stamp
	TOCHECK=$$(find fonts/variable -type f 2>/dev/null); if [ -z "$$TOCHECK" ]; then TOCHECK=$$(find fonts/ttf -type f 2>/dev/null); fi ; . venv/bin/activate; mkdir -p out/ out/proof; diffenator2 proof $$TOCHECK -o out/proof

clean:
	rm -rf venv
	find . -name "*.pyc" -delete

update-project-template:
	npx update-template https://github.com/googlefonts/googlefonts-project-template/

update: venv venv-test
	venv/bin/pip install --upgrade pip-tools
	# See https://pip-tools.readthedocs.io/en/latest/#a-note-on-resolvers for
	# the `--resolver` flag below.
	venv/bin/pip-compile --upgrade --verbose --resolver=backtracking requirements.in
	venv/bin/pip-sync requirements.txt

	venv-test/bin/pip install --upgrade pip-tools
	venv-test/bin/pip-compile --upgrade --verbose --resolver=backtracking requirements-test.in
	venv-test/bin/pip-sync requirements-test.txt

	git commit -m "Update requirements" requirements.txt requirements-test.txt
	git push
