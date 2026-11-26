# Architecture

User-facing docs (Spanish) with more detail: `docs/guide.md` (flows, REST table, security),
`docs/portals.md` (portal DOM and flow map).

## Flow

1. **Upload** (`POST /api/uploads`) → `services.RendicionService.ingest` stores the file
   (`storage.py`: sha256 content addressing, dedupe, magic-byte sniffing, 25 MB cap, path
   sanitising) and runs recognition.
2. **Recognition** (`recognition/`): `text.py` extracts embedded PDF text (pypdf) or OCRs it
   with the `tesseract` binary; `rules.py` classifies doc type and extracts date, amount,
   RUTs, folio, glosa with regexes; `llm.py` is the optional Anthropic classifier.
   `factory.py` picks `auto` (LLM if an API key exists, falling back to rules on error),
   `rules` or `llm`.
3. **Grouping** (`grouping.py`): union-find over documents linked by same folio,
   provider + date, folder and orden↔boleta proximity; each group becomes a rendición in
   `borrador` with a confidence and reason.
4. The user reviews/edits in the UI and marks it `lista`.
5. **Pipeline** (`pipeline.py`, `Pipeline.run_once`): submits `lista` rendiciones to
   Colmena, polls, downloads and attaches the comprobante de bonificación on approval,
   submits to Chubb, polls, fetches Chubb's liquidación, and runs the bank checks. Which
   accounts run and in what order comes from `workflow.py` (`data/workflow.json`).
   `record_submission` / `record_result` are the same transitions used by the REST
   report-back endpoints.

## State machine

`domain.py`: `Status` and `TRANSITIONS`, enforced by `check_transition`.

```
borrador → lista → colmena_enviada → colmena_aprobada → chubb_enviada → chubb_aprobada
                                   ↘ colmena_rechazada (→ borrador)   ↘ chubb_rechazada (→ colmena_aprobada)
any → error → (previous state | borrador)
```

Only `borrador` and `lista` are editable (`EDITABLE_STATUSES`). Every change is an `events`
row in SQLite.

`workflow.py` layers a per-account view on top: stages of `AccountSpec`s (Colmena, Chubb,
optional `abono_colmena` / `abono_chubb` bank nodes) with `requires`/`provides`, so an order
the engine cannot run (Chubb before Colmena) is rejected before saving. Per-account progress
is a `NodeState` (pendiente, por enviar, en revisión, aprobada, rechazada, error, no
continúa).

## Modules (`src/rendiciones/`)

| Path | Role |
| --- | --- |
| `api.py` | FastAPI app: static UI, REST, Host allow-list, CSRF header, CSP |
| `cli.py` | `serve`, `run-pipeline`, `login`, `accounts`, `bank-check`, `clear-marker`, `status`, `list` |
| `config.py` | `Settings.from_env` (`RENDICIONES_DATA_DIR`, `RENDICIONES_PORT`), private `data/` setup |
| `services.py` | Facade used by API and CLI: ingest, group, edit, move, delete, status |
| `db.py` | `Repository` over SQLite: `rendiciones`, `documents`, `events`, `submission_markers`, `bank_payments` |
| `runner.py` | UI-started pipeline passes in a background thread; picks portal and bank clients |
| `locking.py` | `flock` on `data/automation.lock` so only one process drives the browser |
| `accounts.py`, `credentials.py` | Cuentas tab: Keychain credentials, login history, login jobs |
| `rut.py` | RUT parsing, check digit, formatting |
| `automation/base.py` | `PortalClient`, `ColmenaClient`, `StatusResult`, `SubmissionResult` protocols |
| `automation/loader.py` | Resolves `--clients` / `RENDICIONES_PORTAL_FACTORY`; aliases `fake`, `playwright` |
| `automation/fake.py` | `FakeColmenaClient`, `FakeChubbClient`: in-memory, approve everything, ids like `COLMENA-00001` |
| `automation/browser/` | Playwright clients: `colmena.py`, `chubb.py`; `selectors.py` (all URLs and selectors), `mappings.py` (pure prestación tables and parsers), `session.py` (persistent profile), `files.py` (size limits, recompression), `login.py`, `autologin.py` |
| `bank/` | `bancochile.py` (read-only Playwright client), `cartola.py` (payload parsing), `matching.py` (glosa + exact-sum matching), `expected.py` (expected transfer per insurer), `settings.py` (`data/bank.json`), `fake.py` (`FakeBankClient`, `PayingFakeBank` for demos) |
| `static/` | UI: `index.html`, `styles.css`, ES modules without a build step in `js/` (`core.js`, `feedback.js`, `flow.js`, `flujo.js`, `pipeline.js`, `cuentas.js`, `rendiciones/*.js`) |

When a portal changes its markup, edits should land in `automation/browser/selectors.py`
and, for vocabulary, `mappings.py`.

## Local state

- `data/` (`0700`, files `0600`): `rendiciones.sqlite`, `files/`, `tmp/`, `screenshots/`,
  `workflow.json`, `bank.json`, `accounts-state.json`, `automation.lock`.
- `browser-profile/` (`0700`): persistent Playwright profile with the portal sessions.
- macOS Keychain, service `rendiciones-salud`: `portal:colmena`, `portal:chubb`,
  `portal:bancochile`, `anthropic`.
