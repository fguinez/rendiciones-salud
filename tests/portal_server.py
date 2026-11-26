"""Local HTTP server for the fake portal pages in ``tests/fixtures/portals/``.

It serves the Colmena and Chubb look-alike SPAs plus the few JSON/multipart endpoints
their scripts call, and records every POST so tests can assert what was "sent".
Only binds 127.0.0.1; nothing here talks to a real portal.
"""

from __future__ import annotations

import base64
import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PORTALS_DIR = Path(__file__).parent / "fixtures" / "portals"
TOKEN = "tok-fixture-123"
COLMENA_API = "/services/afiliados/private/reembolso-web/"
COLMENA_REPORT = "/services/afiliados/private/report"
CHUBB_LIQUIDATION = "/api/charles-taylor/liquidation-document/"
REPORT_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
)


@dataclass
class PortalState:
    """Mutable behaviour of the fake portals, set per test."""

    today: str = "2026-11-05"
    colmena_expired: bool = False
    # Expired, the app stays on a dead page instead of redirecting to its login route.
    colmena_stale_when_expired: bool = False
    # Any other answer Colmena may give to a login (the wrong password has its own text).
    colmena_login_message: str | None = None
    colmena_password_expired: bool = False
    # The session dies right after the page loads (the API then answers 401).
    colmena_expires_after_load: bool = False
    excedentes: int = 0
    robot_error: bool = False
    # False mimics Colmena's "flujo_colmena": selecting a file posts nothing to the robot.
    robot: bool = True
    request_number: str = "987654"
    colmena_requests: list[dict[str, Any]] = field(default_factory=list)
    chubb_expired: bool = False
    chubb_sso_pending: bool = False
    denuncio: str = "4455667788"
    history: list[dict[str, str]] = field(default_factory=list)
    username: str = "11.111.111-1"
    password: str = "clave-de-prueba"
    logins: list[str] = field(default_factory=list)
    posts: list[tuple[str, bytes]] = field(default_factory=list)

    def posted(self, part: str) -> list[bytes]:
        """Bodies of POSTs whose path contains ``part``."""
        return [body for path, body in self.posts if part in path]


def _rut_clean(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit() or ch in "kK")


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the default stderr logging."""

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _file(self, relative: str) -> None:
        self._send(200, (PORTALS_DIR / relative).read_bytes(), "text/html; charset=utf-8")

    def _state_js(self, which: str) -> None:
        state = self.server.state
        if which == "colmena":
            data = {
                "expired": state.colmena_expired,
                "staleWhenExpired": state.colmena_stale_when_expired,
                "excedentes": state.excedentes,
                "robotError": state.robot_error,
                "robot": state.robot,
                "requestNumber": state.request_number,
                "token": TOKEN,
            }
            if state.colmena_expires_after_load:
                state.colmena_expires_after_load = False
                state.colmena_expired = True
        else:
            data = {
                "expired": state.chubb_expired,
                "today": state.today,
                "denuncio": state.denuncio,
                "history": state.history,
            }
        body = f"window.FIXTURE = {json.dumps(data)};".encode()
        self._send(200, body, "application/javascript")

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        state = self.server.state
        if path == "/afiliados/state.js":
            self._state_js("colmena")
        elif path in ("/afiliados/", "/afiliados/index.html"):
            self._file("colmena/index.html")
        elif path.startswith(COLMENA_API + "consulta/ultimas/"):
            if state.colmena_expired or self.headers.get("X-Auth-Token") != TOKEN:
                self._json({"code": 401, "message": "unauthorized"}, 401)
                return
            folio = path.rsplit("/", 1)[1]
            items = state.colmena_requests
            if folio != "0":
                items = [i for i in items if str(i.get("solIdn")) == folio]
            self._json({"code": 0, "message": "OK", "response": items})
        elif path.startswith(CHUBB_LIQUIDATION):
            denuncio = path[len(CHUBB_LIQUIDATION) :].split("/", 1)[0]
            pdf = REPORT_PDF + denuncio.encode()
            self._json({"response": {"archivoBase64": base64.b64encode(pdf).decode()}})
        elif path == "/chubb/state.js":
            self._state_js("chubb")
        elif path.startswith("/chubb/private/") and state.chubb_sso_pending:
            state.chubb_sso_pending = False
            self._file("chubb/sso.html")
        elif path.startswith("/chubb/private/"):
            self._file("chubb/public.html" if state.chubb_expired else "chubb/index.html")
        elif path == "/chubb/b2c":
            self._file("chubb/b2c.html")
        elif path in ("/chubb/", "/chubb/index.html"):
            self._file("chubb/public.html")
        else:
            self._send(404, b"not found", "text/plain")

    def _login(self, path: str, body: bytes) -> None:
        state = self.server.state
        data = json.loads(body or b"{}")
        portal = "colmena" if path == "/login" else "chubb"
        state.logins.append(portal)
        valid = (
            _rut_clean(str(data.get("user"))) == _rut_clean(state.username)
            and data.get("password") == state.password
        )
        if portal == "chubb":
            if not valid:
                self._json({"error": "bad credentials"}, 401)
                return
            state.chubb_expired = False
            self._json({"ok": True})
        elif state.colmena_login_message is not None:
            self._json({"codigoRetorno": 9, "glosaRetorno": state.colmena_login_message})
        elif not valid:
            self._json({"codigoRetorno": 1, "glosaRetorno": "Usuario o clave inválido."})
        elif state.colmena_password_expired:
            self._json({"codigoRetorno": 2, "glosaRetorno": "Tu contraseña ha caducado."})
        else:
            state.colmena_expired = False
            self._json({"codigoRetorno": 0})

    def _report(self, body: bytes) -> None:
        """Comprobante PDF for an approved request; an empty body for anything else."""
        state = self.server.state
        if state.colmena_expired or self.headers.get("X-Auth-Token") != TOKEN:
            self._json({"code": 401, "message": "unauthorized"}, 401)
            return
        folio = str(json.loads(body or b"{}").get("parametros", {}).get("folioSolicitud"))
        approved = any(
            str(i.get("solIdn")) == folio and i.get("estado") == 6 for i in state.colmena_requests
        )
        self._send(200, REPORT_PDF if approved else b"", "application/pdf")

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        state = self.server.state
        state.posts.append((path, body))
        if path in ("/login", "/chubb/b2c"):
            self._login(path, body)
        elif path.endswith("/validacion-documento"):
            self._json({"code": 1 if state.robot_error else 0, "glosaRetorno": "ok"})
        elif path == COLMENA_API + "solicitud":
            self._json({"code": 0, "response": {"id": int(state.request_number)}})
        elif path.startswith(COLMENA_API + "finaliza-solicitud"):
            self._json({"code": 0})
        elif path == COLMENA_REPORT:
            self._report(body)
        elif path == "/api/refund":
            self._json({"data": "ciphertext"})
        else:
            self._send(404, b"not found", "text/plain")


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, state: PortalState) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.state = state


class PortalServer:
    """Context manager running the fake portals on a random local port."""

    def __init__(self) -> None:
        self.state = PortalState()
        self._server = _Server(self.state)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def origin(self) -> str:
        """``http://127.0.0.1:<port>``."""
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    def __enter__(self) -> PortalServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
