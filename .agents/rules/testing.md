# Testing

- pytest under `tests/`, one `test_<module>.py` per area. Run with `make test`; `make check`
  must be green before committing.
- Every test function has a one-line docstring saying the behaviour it checks.
- Arrange / Act / Assert, separated by blank lines (no `# Arrange` comments).
- Shared fixtures live in `tests/conftest.py` (`settings` on a `tmp_path` data dir,
  `service`, `client` with the CSRF header, `pdf_of`, `ingest`, `lista_rendicion`).
- Document inputs are text fixtures in `tests/fixtures/texts/`, rendered to PDF on the fly
  with `render_pdf()`. Add new document types there, with invented data only
  (`rules/security.md` lists the placeholders).
- Mock every external call: use the fake portal and bank clients
  (`src/rendiciones/automation/fake.py`, `src/rendiciones/bank/fake.py`), `mocker` /
  `monkeypatch` for the Keychain and the Anthropic API. No test may reach the network.
- Browser tests carry the `browser` marker (`pytestmark` in `tests/test_portal_browser.py`)
  and run Playwright against the look-alike portals in `tests/fixtures/portals/`, served by
  `tests/portal_server.py` on 127.0.0.1; any other host is blocked.
