rendiciones-salud is a local, single-user macOS app that turns uploaded health documents
(boletas, órdenes médicas, recetas, comprobantes) into rendiciones: it recognises each
document, groups them, submits each rendición to Isapre Colmena and then to Chubb with
Colmena's comprobante de bonificación, polls both for the outcome, and optionally checks the
Banco de Chile cartola for the insurers' transfers. It is exposed as a FastAPI server on
127.0.0.1 (REST API plus a vanilla JS UI) and a `rendiciones` CLI; the portals are driven
with Playwright, documents and state live in a local SQLite + file store, and secrets live in
the macOS Keychain. **This repository is public: read `rules/security.md` before writing
anything that ends up in a commit, a PR, an issue or an image.**

# Context
- @context/architecture.md
- @context/commands.md

# Rules
- public repo, personal data and real portals: @rules/security.md
- @rules/coding.md
- @rules/testing.md
- @rules/commits.md
