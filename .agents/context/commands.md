# Commands

Requirements: macOS, `uv`, Python 3.12, Tesseract (`brew install tesseract tesseract-lang`).
`make help` lists every target; extra flags go in `ARGS` (`make run ARGS="--headed"`). Full
CLI: `uv run rendiciones --help`.

## Development

| Command | What it does |
| --- | --- |
| `make install` | `uv sync` + Playwright Chromium |
| `make check` | `lint`, `typecheck`, `test` (and the `privacy` guard; see `rules/security.md`) |
| `make test` | `uv run pytest` (`ARGS="-m browser"` for the fixture-portal browser tests) |
| `make lint` / `make format` | `ruff check` + `ruff format --check` / auto-fix |
| `make typecheck` | `uv run mypy src scripts` (strict) |

## Running the app

| Command | Touches real portals? |
| --- | --- |
| `make serve` | UI on 127.0.0.1:8765, real `data/`; its **Enviar** button submits for real |
| `make demo` | `serve --clients fake`: fake portals and bank, but still default `data/` and port |
| `make dry-run` | Fills real portal forms, stops before any upload or irreversible click |
| `make run` | **Submits real claims** and polls statuses |
| `make monitor` | `make run` every hour |
| `make login PORTAL=chubb` | Headed login window (`colmena`, `chubb`) |
| `make accounts` | List / `ARGS="set colmena"` / `ARGS="delete chubb"` Keychain accounts |
| `make status` · `make list` | Counts per status / listing (`ARGS="--status lista"`) |
| `make bank-check` | Lists insurer transfers in the real cartola (read-only, no DB writes) |

`uv run rendiciones run-pipeline --clients fake` runs a demo pass from the console.
`uv run rendiciones clear-marker ID colmena|chubb` clears a pre-submit marker after a manual
check of the portal.

## Demo server for screenshots

Never use 8765 or `data/` for demos (see `rules/security.md`):

```bash
RENDICIONES_DATA_DIR=<scratch dir> RENDICIONES_PORT=<other port> \
  uv run rendiciones serve --clients fake --classifier rules
```

Seed it with PDFs from `render_pdf()` in `tests/conftest.py` applied to
`tests/fixtures/texts/*.txt`.

## Environment

See `.env.example`: `RENDICIONES_DATA_DIR`, `RENDICIONES_PORT`, `RENDICIONES_CLASSIFIER`
(`auto` | `rules` | `llm`), `RENDICIONES_PORTAL_FACTORY` (`module:function` returning
`(colmena, chubb)`), `RENDICIONES_BROWSER_PROFILE`, `RENDICIONES_BROWSER_CHANNEL`.
`deploy/cl.rendiciones-salud.monitor.plist` is an uninstalled launchd template for the
hourly monitor.
