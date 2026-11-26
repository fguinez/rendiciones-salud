"""Command line entry point: ``rendiciones serve | run-pipeline | login | status | list``."""

from __future__ import annotations

import argparse
import json
import sys
import time

from rendiciones.config import DEFAULT_HOST, Settings, configure_logging
from rendiciones.domain import Status
from rendiciones.recognition import build_classifier


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the web app on 127.0.0.1."""
    import uvicorn

    from rendiciones.api import create_app
    from rendiciones.runner import bank_factory, clients_factory

    settings = Settings.from_env()
    port = args.port or settings.port
    app = create_app(
        settings,
        build_classifier(args.classifier),
        portal_clients=clients_factory(args.clients),
        bank=bank_factory(args.clients, settings),
    )
    print(f"Abriendo http://{DEFAULT_HOST}:{port}")
    uvicorn.run(app, host=DEFAULT_HOST, port=port, log_level="info", access_log=False)
    return 0


def _close_clients(*clients: object) -> None:
    for client in clients:
        close = getattr(client, "close", None)
        if callable(close):
            close()


def cmd_run_pipeline(args: argparse.Namespace) -> int:
    """Run the saved workflow (Colmena → Chubb by default) once (or in a loop)."""
    from rendiciones.automation.loader import (
        FAKE_SPEC,
        PLAYWRIGHT_SPEC,
        PortalConfigError,
        load_clients,
        resolve_spec,
    )
    from rendiciones.locking import AutomationBusyError, AutomationLock
    from rendiciones.pipeline import Pipeline
    from rendiciones.services import RendicionService
    from rendiciones.workflow import WorkflowStore

    spec = resolve_spec(args.clients)
    options: dict[str, object] = {}
    if spec == PLAYWRIGHT_SPEC:
        options = {
            "dry_run": args.dry_run,
            "headed": args.headed,
            "allow_duplicates": frozenset(args.allow_duplicate or ()),
        }
    try:
        colmena, chubb = load_clients(spec, **options)
    except PortalConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if spec == FAKE_SPEC:
        print("ADVERTENCIA: clientes falsos; los estados no reflejan los portales reales.")
    if args.dry_run:
        print("DRY-RUN: se llenan los formularios sin adjuntar ni enviar; no cambia ningún estado.")
    settings = Settings.from_env()
    service = RendicionService(settings, build_classifier("rules"))
    workflow = WorkflowStore.for_data_dir(settings.data_dir).read()
    print("Flujo: " + " → ".join(" + ".join(stage) for stage in workflow.stages))
    from rendiciones.bank.settings import BankSettingsStore
    from rendiciones.runner import bank_factory

    bank = None
    if any(key.startswith("abono_") for key in workflow.accounts):
        make_bank = bank_factory(args.clients, settings)
        bank = make_bank() if make_bank else None
    pipeline = Pipeline(
        service,
        colmena,
        chubb,
        dry_run=args.dry_run,
        workflow=workflow,
        bank=bank,
        bank_settings=BankSettingsStore.for_data_dir(settings.data_dir).read(),
    )
    lock = AutomationLock.for_data_dir(settings.data_dir)
    try:
        while True:
            try:
                with lock.hold("run-pipeline"):
                    try:
                        report = pipeline.run_once()
                    finally:
                        _close_clients(colmena, chubb)
            except AutomationBusyError as exc:
                print(f"Pasada omitida: {exc}", file=sys.stderr, flush=True)
                if not args.loop:
                    return 1
                time.sleep(args.interval)
                continue
            print(json.dumps(report.as_dict()), flush=True)
            for problem in report.bank_errors:
                print(f"Verificación de abonos: {problem}", file=sys.stderr)
            for portal in report.sessions_expired:
                print(
                    f"Sesión de {portal} expirada: inicia sesión desde Cuentas en la app "
                    f"o con `uv run rendiciones login {portal}`",
                    file=sys.stderr,
                )
            if not args.loop:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 130


def cmd_bank_check(args: argparse.Namespace) -> int:
    """Read the Banco de Chile cartola (read-only) and list the insurers' abonos."""
    from datetime import date, timedelta

    from rendiciones.bank import BankError
    from rendiciones.bank.bancochile import bank_client
    from rendiciones.bank.matching import glosa_matches
    from rendiciones.bank.settings import BankSettingsStore
    from rendiciones.locking import AutomationBusyError, AutomationLock

    settings = Settings.from_env()
    bank_settings = BankSettingsStore.for_data_dir(settings.data_dir).read()
    since = date.today() - timedelta(days=args.days)
    lock = AutomationLock.for_data_dir(settings.data_dir)
    try:
        with lock.hold("rendiciones bank-check"):
            abonos = bank_client(settings, headless=not args.headed).credits(
                since, lambda d: any(glosa_matches(d, v) for v in bank_settings.glosas.values())
            )
    except (AutomationBusyError, BankError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for movement in sorted(abonos, key=lambda m: m.occurred_on):
        insurer = next(
            (k for k, v in bank_settings.glosas.items() if glosa_matches(movement.description, v)),
            "?",
        )
        amount = f"{movement.amount:,}".replace(",", ".")
        print(
            f"{movement.occurred_on.isoformat()}  {insurer:8}  {amount:>10}  "
            f"{movement.operation_id or '-':>14}  {movement.description}"
        )
    print(f"{len(abonos)} abono(s) de aseguradoras desde {since.isoformat()}")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    """Open a headed browser on the portal and wait for the login (autofilled if saved)."""
    from rendiciones.automation.browser import BrowserOptions, BrowserSession, profile_dir
    from rendiciones.automation.browser.login import wait_for_login
    from rendiciones.credentials import CredentialStoreError, KeychainStore
    from rendiciones.locking import AutomationBusyError, AutomationLock

    try:
        credential = None if args.manual else KeychainStore().get(args.portal)
    except CredentialStoreError as exc:
        print(f"{exc}; sigue a mano.", file=sys.stderr)
        credential = None
    hint = (
        "Se llenará con la cuenta guardada; completa lo que falte."
        if credential
        else "Inicia sesión a mano."
    )
    print(f"Se abrirá {args.portal} en el navegador del proyecto. {hint}")
    lock = AutomationLock.for_data_dir(Settings.from_env().data_dir)
    try:
        with (
            lock.hold(f"rendiciones login {args.portal}"),
            BrowserSession(BrowserOptions(profile_dir=profile_dir(), headless=False)) as session,
        ):
            ok = wait_for_login(session, args.portal, timeout_s=args.timeout, credential=credential)
    except AutomationBusyError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if ok:
        print(f"Sesión de {args.portal} detectada y guardada en el perfil del navegador.")
        return 0
    print(f"No se detectó una sesión iniciada en {args.portal}.", file=sys.stderr)
    return 1


def cmd_accounts(args: argparse.Namespace) -> int:
    """List, save or delete portal credentials in the keychain."""
    import getpass

    from rendiciones.accounts import STATE_FILE, AccountService, LoginLog, browser_login_runner
    from rendiciones.credentials import CredentialStoreError, KeychainStore

    settings = Settings.from_env()
    accounts = AccountService(
        KeychainStore(), LoginLog(settings.data_dir / STATE_FILE), browser_login_runner
    )
    if args.action != "list" and not args.portal:
        print("Indica el portal: colmena o chubb", file=sys.stderr)
        return 2
    try:
        if args.action == "set":
            username = input(f"Usuario de {args.portal}: ")
            password = getpass.getpass(f"Contraseña de {args.portal} (no se muestra): ")
            accounts.save(args.portal, username, password)
            print(f"Cuenta de {args.portal} guardada en el llavero.")
        elif args.action == "delete":
            deleted = accounts.delete(args.portal)
            print("Cuenta borrada." if deleted else "No había cuenta guardada.")
        else:
            for account in accounts.list():
                user = account["username_hint"] or "sin configurar"
                print(f"{account['portal']:<8} {user}")
    except (CredentialStoreError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def cmd_clear_marker(args: argparse.Namespace) -> int:
    """Remove the idempotency marker left by an interrupted submission."""
    from rendiciones.services import RendicionService

    service = RendicionService(Settings.from_env(), build_classifier("rules"))
    removed = service.repo.clear_marker(args.id, args.portal)
    if removed:
        service.repo.add_event(args.id, f"{args.portal}_marcador_borrado", {"via": "cli"})
    print("Marcador borrado." if removed else "No había marcador para esa rendición.")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    """Print the number of rendiciones per status."""
    from rendiciones.services import RendicionService

    service = RendicionService(Settings.from_env(), build_classifier("rules"))
    counts = service.repo.count_by_status()
    for status in Status:
        print(f"{status.value:<18} {counts.get(status.value, 0)}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """Print rendiciones as a table."""
    from rendiciones.services import RendicionService

    service = RendicionService(Settings.from_env(), build_classifier("rules"))
    status = Status(args.status) if args.status else None
    print(f"{'ID':>4}  {'ESTADO':<17} {'FECHA':<10} {'MONTO':>10}  {'TIPO':<22} DOCS")
    for r in service.repo.list_rendiciones(status):
        fecha = r.fecha_atencion.isoformat() if r.fecha_atencion else "-"
        monto = f"{r.monto:,}".replace(",", ".") if r.monto is not None else "-"
        tipo = r.tipo_prestacion.value if r.tipo_prestacion else "-"
        docs = len(r.documents)
        print(f"{r.id:>4}  {r.status.value:<17} {fecha:<10} {monto:>10}  {tipo:<22} {docs}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """CLI argument parser."""
    parser = argparse.ArgumentParser(prog="rendiciones", description="Rendiciones de salud")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Levantar la interfaz web (solo 127.0.0.1)")
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--classifier", choices=["auto", "rules", "llm"], default=None)
    serve.add_argument(
        "--clients",
        default=None,
        help="Clientes para los envíos desde la app: 'playwright' (por defecto), 'fake' o "
        "modulo:funcion",
    )
    serve.set_defaults(func=cmd_serve)

    run = sub.add_parser("run-pipeline", help="Enviar a Colmena/Chubb y consultar estados")
    run.add_argument(
        "--clients",
        default=None,
        help="'playwright' (portales reales), 'fake' (demo) o modulo:funcion",
    )
    run.add_argument("--loop", action="store_true", help="Repetir indefinidamente")
    run.add_argument("--interval", type=int, default=600, help="Segundos entre pasadas")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Llenar formularios sin adjuntar ni enviar, sin cambiar estados",
    )
    run.add_argument("--headed", action="store_true", help="Mostrar el navegador")
    run.add_argument(
        "--allow-duplicate",
        type=int,
        action="append",
        metavar="ID",
        help="Enviar a Colmena aunque exista una solicitud con el mismo monto (repetible)",
    )
    run.set_defaults(func=cmd_run_pipeline)

    login = sub.add_parser("login", help="Iniciar sesión a mano en un portal (navegador visible)")
    login.add_argument("portal", choices=["colmena", "chubb"])
    login.add_argument("--timeout", type=int, default=900, help="Segundos máximos de espera")
    login.add_argument("--manual", action="store_true", help="No usar la cuenta guardada")
    login.set_defaults(func=cmd_login)

    accounts = sub.add_parser("accounts", help="Cuentas de los portales (llavero de macOS)")
    accounts.add_argument("action", choices=["list", "set", "delete"], nargs="?", default="list")
    accounts.add_argument("portal", choices=["colmena", "chubb", "bancochile"], nargs="?")
    accounts.set_defaults(func=cmd_accounts)

    bank = sub.add_parser(
        "bank-check", help="Leer la cartola del Banco de Chile y listar abonos de aseguradoras"
    )
    bank.add_argument("--days", type=int, default=60, help="Días hacia atrás")
    bank.add_argument("--headed", action="store_true", help="Mostrar el navegador")
    bank.set_defaults(func=cmd_bank_check)

    clear = sub.add_parser(
        "clear-marker", help="Borrar el marcador de un envío interrumpido (tras revisar el portal)"
    )
    clear.add_argument("id", type=int)
    clear.add_argument("portal", choices=["colmena", "chubb"])
    clear.set_defaults(func=cmd_clear_marker)

    status = sub.add_parser("status", help="Resumen por estado")
    status.set_defaults(func=cmd_status)

    lst = sub.add_parser("list", help="Listar rendiciones")
    lst.add_argument("--status", choices=[s.value for s in Status], default=None)
    lst.set_defaults(func=cmd_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    configure_logging()
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
