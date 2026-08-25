# agentbox — see README.md for what the targets do.

.DEFAULT_GOAL := help

.PHONY: help
help:
	@echo "make install    install the host dependencies and agentbox from this checkout"
	@echo "make host-deps  install docker, node, openssh and sysbox only"
	@echo "make uninstall  remove the installed agentbox"
	@echo "make test       run every test (unit + end-to-end)"
	@echo "make test-unit  run the unit tests only"
	@echo "make test-e2e   run the end-to-end suite against containerised local LLMs"

.PHONY: install
install: host-deps
	-pipx uninstall agentboxer
	pipx install .

.PHONY: host-deps
host-deps:
	bash scripts/install-host-deps.sh

.PHONY: uninstall
uninstall:
	pipx uninstall agentboxer

.PHONY: test
test: test-unit test-e2e

.PHONY: test-e2e
test-e2e:
	bash tests/e2e/run.sh

.PHONY: test-unit
test-unit:
	python -m unittest discover -s tests
