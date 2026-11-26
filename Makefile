UV      ?= uv
RUN     := $(UV) run rendiciones
PORTAL  := colmena
ARGS    :=

.DEFAULT_GOAL := help
.PHONY: help install serve demo dry-run run monitor login accounts status list bank-check \
	test lint format typecheck privacy check clean

help: ## Muestra esta ayuda
	@awk 'BEGIN {FS = ":.*## "} /^[a-z-]+:.*## / {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Instala dependencias y el Chromium de Playwright
	$(UV) sync
	$(UV) run playwright install chromium

serve: ## Levanta la interfaz (puerto RENDICIONES_PORT, 8765 por defecto)
	$(RUN) serve $(ARGS)

demo: ## Levanta la interfaz con portales falsos (no envía nada)
	$(RUN) serve --clients fake $(ARGS)

dry-run: ## Llena los formularios sin enviar (navegador visible)
	$(RUN) run-pipeline --clients playwright --dry-run --headed $(ARGS)

run: ## Envía las rendiciones listas a los portales y consulta estados
	$(RUN) run-pipeline --clients playwright $(ARGS)

monitor: ## Repite el envío y la consulta cada hora
	$(RUN) run-pipeline --clients playwright --loop --interval 3600 $(ARGS)

login: ## Inicia sesión en un portal: make login PORTAL=chubb
	$(RUN) login $(PORTAL) $(ARGS)

accounts: ## Muestra las cuentas guardadas en el llavero
	$(RUN) accounts $(ARGS)

status: ## Cuenta rendiciones por estado
	$(RUN) status

list: ## Lista rendiciones (ARGS="--status lista")
	$(RUN) list $(ARGS)

bank-check: ## Lista abonos de aseguradoras en la cartola del Banco de Chile
	$(RUN) bank-check $(ARGS)

test: ## Corre los tests
	$(UV) run pytest $(ARGS)

lint: ## Revisa lint y formato
	$(UV) run ruff check
	$(UV) run ruff format --check

format: ## Corrige lint y formatea
	$(UV) run ruff check --fix
	$(UV) run ruff format

typecheck: ## Revisa tipos con mypy
	$(UV) run mypy src scripts

privacy: ## Revisa que no haya datos personales
	$(UV) run --no-project python scripts/check_personal_data.py

check: lint typecheck privacy test ## Corre lint, tipos, privacidad y tests

clean: ## Borra cachés
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	find src tests -name __pycache__ -type d -prune -exec rm -rf {} +
