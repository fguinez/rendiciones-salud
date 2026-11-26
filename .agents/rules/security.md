# Security: personal data and real portals

## Personal data (this repo is public)

The app runs on the owner's real health claims, so everything it touches is personal. GitHub
keeps PR diffs, descriptions and comments forever, even after a force-push or a deleted
branch. An earlier audit found a PR description that named the owner's therapy and real
reimbursement amounts, and tests whose "synthetic" bank rows had been copied from a real
statement; the private repo had to be replaced by a fresh public one. Do not repeat that.

**NEVER** put the owner's real data in code, tests, fixtures, docs, commit messages, PR
titles, descriptions or comments, issues, screenshots or GIFs. That covers:

- RUTs; names of the owner or family; addresses, phones, e-mails.
- Bank account numbers, balances, transaction ids.
- Insurance policy / contract numbers, Colmena folios and N° de solicitud, Chubb request
  (denuncio) numbers.
- Real amounts and dates of claims.
- Health details: diagnoses, therapy or treatment types, prescriptions, exams.
- Names of real doctors, clinics, labs or other providers.
- Portal usernames, passwords, tokens, cookies, API keys.

**Invent test data; never derive it from a real document, statement, portal page or log.**
Reuse the existing placeholders:

- RUTs: `11.111.111-1` (patient), `22.333.444-K`, `76.123.456-0` and `77.654.321-7`
  (providers), `15.555.555-6` (doctor), `12.345.678-9` / `12.345.678-5` (format examples),
  `11.111.111-2` (bad check digit). Same values without dots are fine.
- Names marked DEMO / FICTICIA / EJEMPLO / PRUEBA: `LABORATORIO CLÍNICO DEMO SPA`,
  `CLÍNICA DEMO`, `CENTRO MÉDICO EJEMPLO`, `Camila Ficticia Pérez`, `Juan Ficticio`,
  `PACIENTE DE PRUEBA`, `Banco Ficticio`.
- Round amounts such as `28.000` and `40.000`; e-mails at `@example.com`.
- Fake portal ids like `COLMENA-00001` / `CHUBB-00001` (`src/rendiciones/automation/fake.py`).

See `tests/fixtures/texts/` and `tests/conftest.py` for the full set.

**Debugging against the real portals or the real bank:** keep findings out of the repo and
PRs. Describe behaviour ("Chubb rejects attention dates older than 60 days"), never values.
Never write "tested against the real account" next to a number, a date or an id. Map new
portal pages with placeholders (`<RUT>`, `<FOLIO>`, `<AMOUNT>`, `<DATE>`), as
`docs/portals.md` does.

**Screenshots and GIFs only from a fake demo server.** Port 8765 is the owner's real server
(real portals, real data), and `make demo` / `.claude/launch.json` still use the default
`data/` and port. Run a separate server on a scratch data dir:

```bash
RENDICIONES_DATA_DIR=<scratch dir> RENDICIONES_PORT=<other port> \
  uv run rendiciones serve --clients fake --classifier rules
```

Seed it by uploading PDFs built with `render_pdf()` (`tests/conftest.py`) from
`tests/fixtures/texts/`.

**Local data stays local.** `data/`, `.env`, `*.sqlite` and `browser-profile/` are in
`.gitignore` and must stay there. Portal and bank passwords and the Anthropic API key live in
the macOS Keychain (service `rendiciones-salud`; `src/rendiciones/credentials.py`,
`src/rendiciones/recognition/secrets.py`), never on disk, in logs or in the repo. Logs carry
ids and counts only, never names, RUTs, amounts or document content.

**CI guard.** `make privacy` (part of `make check`, script `scripts/check_personal_data.py`)
fails on RUT-shaped strings outside its allowlist, non-example e-mails and home-directory
paths. If it flags something, replace the value with a placeholder. Add to the allowlist only
for a genuine placeholder, never to silence a real value.

**Don't mention the owner's employer or work tooling** in the repo, commits or PRs.

## Real portals and the bank

- `make run`, `make monitor`, `rendiciones run-pipeline --clients playwright` and the UI's
  **Enviar y consultar estados** submit real claims. Use `make dry-run` / **Probar sin
  enviar**, or fake clients (`--clients fake`), unless the owner explicitly asks to submit.
- Tests never reach a real site: browser tests run against `tests/portal_server.py` on
  127.0.0.1 and block every other host. Mock every external call (portals, bank, Anthropic
  API, Keychain).
- The Banco de Chile integration (`src/rendiciones/bank/bancochile.py`) is read-only: log in,
  read the CLP cartola, nothing else. Keep it that way. **NEVER** automate anything that moves
  money (transfers, payments, payee or account changes) in any client.
- Don't weaken the duplicate-submission guards in `src/rendiciones/pipeline.py` (recent-request
  check, `submission_markers`) or the one-attempt auto-login in
  `src/rendiciones/automation/browser/autologin.py`, which avoids locking the account.
- Don't weaken the API's local-only protections in `src/rendiciones/api.py` (Host allow-list,
  `X-Rendiciones-Client` CSRF header, CSP) or the `0700`/`0600` modes set in
  `src/rendiciones/config.py`.
