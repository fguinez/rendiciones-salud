# Coding

- Python 3.12 only (`requires-python = ">=3.12,<3.13"`), managed with `uv`. Add dependencies
  with `uv add` (dev ones with `uv add --dev`); runtime deps stay in `pyproject.toml`.
- `mypy --strict` on `src/` must pass; fully type new code. Untyped third-party modules are
  listed in `[[tool.mypy.overrides]]` in `pyproject.toml`.
- `ruff check` and `ruff format` (line length 100, rule set in `pyproject.toml`) must pass.
- Language split: code, identifiers, comments, docstrings, logs and commit messages in
  English; user-facing text (UI in `src/rendiciones/static/`, CLI help, API error details,
  `README.md`, `docs/`) in neutral Chilean Spanish. Domain words stay in Spanish everywhere
  (`rendición`, `boleta`, `prestación`, `bonificación`, `abono`, `cartola`, `glosa`).
- Keep the layering: API and CLI go through `services.py` / `pipeline.py`; SQL lives only in
  `db.py`, parametrised; status changes go through `Repository` in `db.py`, which enforces
  `domain.check_transition`.
- Portal selectors and URLs belong in `automation/browser/selectors.py`; portal vocabulary
  and parsers in the pure, unit-tested `automation/browser/mappings.py`.
- New portal or bank clients implement the protocols in `automation/base.py` or
  `bank/__init__.py` and get a fake counterpart for tests and demos.
- Log ids and counts only (see `rules/security.md`).
- The UI is vanilla ES modules with no build step and no external dependencies; keep it so.
- Keep comments and docstrings short; don't restate the code.
