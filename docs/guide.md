# Guía de uso

Detalle de cada parte de la app. Para empezar, ver el [README](../README.md).

## Requisitos

- uv instalado en `~/.local/bin/uv` (la plantilla de launchd en `deploy/` usa esa ruta).
- Tesseract usa español si está instalado el idioma `spa`; si no, inglés.
- Los documentos quedan en `data/`, fuera de git, con permisos `0700` (carpeta) y `0600`
  (archivos).

## Interfaz

En la interfaz puedes elegir archivos, elegir una carpeta completa o arrastrar archivos y
carpetas (se recorren subcarpetas). Se aceptan PDF, JPG, PNG, HEIC y WEBP de hasta 25 MB;
el tipo se valida por contenido (magic bytes), no por extensión. Cada archivo muestra su
resultado (tipo detectado, duplicado o error). Luego los documentos se agrupan en
rendiciones; en cada tarjeta puedes mover documentos a otra rendición o a una nueva, editar
tipo de prestación, fecha y monto, eliminar, y marcarla como **lista para enviar**. Cada
tarjeta muestra el avance por cuenta: un punto por cuenta (Colmena, Chubb) con su color y
estado (pendiente, por enviar, en revisión, aprobada, rechazada, error, no continúa).

En la pestaña **Flujo** (`http://127.0.0.1:8765/#flujo`) se edita a qué cuentas se envía
cada rendición y en qué orden: las cuentas se arrastran entre etapas (las de una misma etapa
van en paralelo) o fuera del flujo. Se guarda en `data/workflow.json` y lo usan las pasadas
desde la app y `run-pipeline`. Un orden que el motor no puede ejecutar no se guarda (por
ejemplo, Chubb antes que Colmena, porque necesita el comprobante de la Isapre). Las
solicitudes ya enviadas a una cuenta que sacas del flujo se siguen consultando.

Variables opcionales (ver `.env.example`): `RENDICIONES_DATA_DIR`, `RENDICIONES_PORT`,
`RENDICIONES_CLASSIFIER` (`auto` | `rules` | `llm`), `RENDICIONES_PORTAL_FACTORY`.

## Envío automático a Colmena y Chubb (navegador)

Los clientes reales usan Playwright con un perfil persistente en `browser-profile/` (fuera
de git, permisos `0700`). Usa Google Chrome si está instalado; si no, el Chromium de
Playwright (`uv run playwright install chromium`).

**1. Cuentas e inicio de sesión.** En la interfaz, pestaña **Cuentas**
(`http://127.0.0.1:8765/#cuentas`), guarda el usuario (RUT o correo) y la contraseña de
cada portal y presiona **Iniciar sesión ahora**: se abre el portal en una ventana visible,
se llena la cuenta guardada y tú completas lo que falte (por ejemplo, un código de
verificación). Sin cuenta guardada, la misma ventana sirve para iniciar sesión a mano.

- Las contraseñas se guardan **solo en el llavero de macOS (Keychain)**, cifradas, bajo el
  servicio `rendiciones-salud` (un ítem `portal:colmena` y otro `portal:chubb`). No quedan
  en la base, en `data/`, en logs ni en git, y la interfaz nunca las vuelve a mostrar (solo
  los últimos dígitos del usuario). Se pueden ver o borrar en *Acceso a Llaveros*.
- Cuando la sesión expira durante el envío automático, el navegador vuelve a iniciar
  sesión con la cuenta guardada. Lo intenta **una sola vez**: si el portal rechaza la clave
  o pide un paso adicional, el inicio automático queda en pausa (para no bloquear la cuenta)
  hasta que guardes la clave de nuevo o inicies sesión desde Cuentas.
- Los campos se buscan de forma genérica (`automation/browser/autologin.py`). En Colmena el
  inicio automático parte desde su página de login (`#/login`), salvo que la pestaña aún
  pueda tener sesión (abrir esa página la cierra), y lee su respuesta:
  "Usuario o clave inválido." pausa por clave rechazada; una clave caducada o cualquier otro
  mensaje pausa y muestra el texto de Colmena en Cuentas. Cada intento fallido deja una
  captura en `data/screenshots/login-<portal>-<fecha>.png`.

Alternativa por consola:

```bash
uv run rendiciones accounts                # ver qué cuentas están guardadas
uv run rendiciones accounts set colmena    # pide usuario y clave (la clave no se muestra)
uv run rendiciones accounts delete chubb
uv run rendiciones login colmena           # ventana visible; --manual para no llenar la cuenta
```

**2. Probar sin enviar (`--dry-run`).** Llena los formularios (cuenta bancaria, tipo y
subtipo de prestación, beneficiario, producto, fecha…) y se detiene antes de cualquier carga
o clic irreversible; en Colmena ni siquiera adjunta, porque el portal sube cada archivo
apenas se elige. No cambia ningún estado; deja un evento `dry_run` en el historial.

```bash
uv run rendiciones run-pipeline --clients playwright --dry-run --headed
```

**3. Enviar de verdad.** Sin `--dry-run` se envía: `lista` → Colmena → (aprobada) descarga
el comprobante → Chubb → consulta resultados. `--headed` muestra el navegador.

```bash
uv run rendiciones run-pipeline --clients playwright [--headed]
```

**Desde la app.** En la pestaña Rendiciones, el panel **Envío a Colmena y Chubb** hace lo
mismo que `run-pipeline` sin abrir una terminal: «Probar sin enviar» equivale a `--dry-run`, y
«Enviar y consultar estados» envía de verdad (pide confirmación). La pasada corre en segundo
plano con el navegador oculto, y al terminar muestra qué rendiciones avanzaron. Usa los
portales reales; `serve --clients fake` sirve para probar la interfaz, y
`uv run rendiciones run-pipeline --clients fake` corre una pasada de demo por consola.

El servidor, `run-pipeline` y los inicios de sesión comparten un candado
(`data/automation.lock`), así que nunca usan el navegador del proyecto al mismo tiempo: si uno
lo tiene, los otros esperan o avisan quién lo está usando. Con `--loop`, una pasada que
encuentra el navegador ocupado se omite y se reintenta en el siguiente intervalo.

**4. Monitor.** `--loop --interval 3600` repite cada hora. También hay una plantilla de
launchd (no instalada) en `deploy/cl.rendiciones-salud.monitor.plist` que corre una pasada
por hora; las instrucciones están dentro del archivo.

```bash
uv run rendiciones run-pipeline --clients playwright --loop --interval 3600
```

Qué hace cada portal:

- **Colmena:** confirma la cuenta bancaria, elige tipo/subtipo según la tabla de
  `automation/browser/mappings.py`, adjunta boleta / detalle / orden médica en su bloque,
  espera la validación de cada archivo, acepta términos y presiona Continuar. Si aparece el
  paso de excedentes **siempre elige "Sí" (usar excedentes como bonificación adicional)**.
  Límite 5 MB en total (las fotos se recomprimen; un PDF demasiado grande falla antes de
  subir nada). Guarda el N° de solicitud y una captura en `data/screenshots/` (`0600`).
  Estados desde el JSON de "últimos reembolsos": Ingresada / En revisión / Por corregir →
  pendiente, Aprobada → aprobada (con monto), Rechazada → rechazada (con motivo).
- **Chubb:** entra desde el inicio (los enlaces directos no sirven), elige beneficiario por
  el RUT del paciente de los documentos, producto (Salud o Dental), prestación, fecha de
  atención (máximo 60 días), "reembolsado previamente: Sí", adjunta documentos + comprobante
  (10 MB por archivo) y confirma. Guarda el N° de denuncio y una captura. Estados desde
  "Historial de Reembolsos": Pagado → aprobada, Rechazada → rechazada, En Proceso → pendiente.

Seguridad ante dobles envíos:

- Antes de enviar a Colmena se revisan tus últimas solicitudes: si ya hay una con el mismo
  monto posterior a la fecha de atención (y no asociada a otra rendición), no se envía y la
  rendición queda en `error` con el N° encontrado. Si de verdad es otra prestación:
  `run-pipeline --clients playwright --allow-duplicate ID`.
- Justo antes del clic final se guarda un marcador en la base. Si el proceso se corta, la
  siguiente pasada no reenvía: busca la solicitud en el portal y la asocia, o deja la
  rendición en `error` pidiendo revisar. Tras revisar el portal:
  `uv run rendiciones clear-marker ID colmena` (o `chubb`) y vuelve la rendición a `lista`.

Si la sesión expiró y no se pudo volver a iniciar con la cuenta guardada, la rendición en
curso pasa a `error` con el mensaje "inicia sesión desde Cuentas o con
`rendiciones login colmena`" (o `chubb`) y ese portal se omite en el resto de la pasada.
Después de iniciar sesión, devuelve la rendición a su estado anterior desde la interfaz o con
`POST /api/rendiciones/{id}/status`.

## Verificación de abonos en el Banco de Chile (opcional)

Confirma en la cartola de tu cuenta corriente (CLP) que el reembolso aprobado por cada
aseguradora llegó de verdad. Es opcional: se activa agregando en **Flujo** los nodos
**Abono Colmena** y/o **Abono Chubb**. Cada uno necesita la aprobación de su aseguradora en
una etapa anterior; por ejemplo:

```
Etapa 1: Colmena   →   Etapa 2: Chubb + Abono Colmena   →   Etapa 3: Abono Chubb
```

- **Cuenta.** En **Cuentas**, guarda el RUT y la clave del Banco de Chile (van al llavero
  como `portal:bancochile`) y usa **Probar conexión**. O por consola:
  `uv run rendiciones accounts set bancochile`.
- **Qué se busca.** En la misma tarjeta se configura la glosa de cada aseguradora (por
  defecto Colmena `Pago:proveedores 0762966190` y Chubb `Pago:proveedores 0774088202`) y
  los días de espera tras la aprobación (30 por defecto). Se guarda en `data/bank.json`.
- **Cómo se calza.** Solo cuentan los abonos cuya glosa contiene la de esa aseguradora y
  que llegaron después del envío. Un abono confirma la combinación más pequeña de
  rendiciones aprobadas pendientes cuyos montos suman exactamente su monto (una
  aseguradora puede pagar varias juntas). Un abono ya usado no vuelve a usarse.
- **Estados del nodo.** Pendiente (la aseguradora aún no aprueba) · Esperando abono ·
  Abonado (con el ID Transacción del banco) · No llegó (pasaron los días de espera) · Sin
  abono (rechazada o monto 0). Un abono que llega tarde igual pasa a Abonado.
- **Lectura.** Solo lectura: se inicia sesión (o se reutiliza la sesión guardada en
  `browser-profile/bancochile-session.json`, `0600`), se abre Movimientos, se elige la
  cuenta CLP y se lee la cartola que carga el propio portal. La cartola se lee una
  vez por pasada y solo si hay algún abono pendiente. Si el banco rechaza la clave, el
  inicio automático queda en pausa hasta que la guardes de nuevo. Un error del banco no
  cambia el estado de las rendiciones: aparece como aviso en el panel de envío.
- **Revisar a mano:** `uv run rendiciones bank-check [--days 60] [--headed]` lista los
  abonos de aseguradoras de la cartola sin tocar la base.

El cliente es una versión reducida del scraper de Banco de Chile de el-chanchito
(`apps/scrapers/scrapers/backends/banchile_web.py` y `banchile_movements.py`): login,
cartola CLP y detalle de la glosa, sin tarjetas, USD ni saldos.

## Clasificador con IA (opcional)

El modo por defecto es `auto`: si hay una API key de Anthropic disponible usa Claude
(`claude-sonnet-5-5`) y cae a las reglas ante cualquier error; si no hay key, usa solo el
clasificador por reglas, sin conexión. `rules` fuerza las reglas; `llm` se comporta como
`auto`, pero avisa en el log si no hay key.
La key se lee de `ANTHROPIC_API_KEY` o del Llavero de macOS; nunca se guarda en disco ni en
logs. Para guardarla en el Llavero:

```bash
security add-generic-password -s rendiciones-salud -a anthropic -w   # pide la key
security find-generic-password -s rendiciones-salud -a anthropic -w  # verificar
security delete-generic-password -s rendiciones-salud -a anthropic   # borrar
```

Ojo: con el clasificador IA los documentos se envían a la API de Anthropic.

## Estados

```
borrador → lista → colmena_enviada → colmena_aprobada → chubb_enviada → chubb_aprobada
                                  ↘ colmena_rechazada (→ borrador)    ↘ chubb_rechazada (→ colmena_aprobada)
cualquier estado → error → (estado anterior | borrador)
```

Cada cambio queda en el historial (`events`), junto con los N° de solicitud, resultados,
montos bonificados/reembolsados y el comprobante de bonificación adjunto.

## Automatización de portales

`rendiciones.automation.base` define los protocolos:

```python
class PortalClient(Protocol):
    name: str

    def submit(self, rendicion: RendicionView) -> SubmissionResult: ...
    def check_status(self, request_id: str) -> StatusResult: ...


class ColmenaClient(PortalClient, Protocol):
    def download_bonificacion(self, request_id: str, dest: Path) -> Path: ...
```

`run-pipeline` carga `RENDICIONES_PORTAL_FACTORY=rendiciones.<modulo>:<funcion>` (debe
devolver `(colmena, chubb)`) y hace: `lista` → enviar a Colmena → consultar → al aprobar,
descargar y adjuntar comprobante → enviar a Chubb → consultar. `FakePortalClient` /
`FakeColmenaClient` sirven para pruebas. Detalles de los portales: `docs/portals.md`.

Alternativamente, un agente externo que maneja el navegador puede usar la API REST
(las escrituras requieren el encabezado `X-Rendiciones-Client: 1`):

| Método | Ruta | Uso |
| --- | --- | --- |
| GET | `/api/rendiciones?status=lista` | rendiciones listas, con rutas absolutas de archivos |
| GET | `/api/rendiciones/{id}` · `/events` | detalle e historial |
| POST | `/api/rendiciones/{id}/submissions` | `{"portal": "colmena"\|"chubb", "request_id": "..."}` |
| POST | `/api/rendiciones/{id}/results` | `{"portal", "outcome": "approved"\|"rejected", "monto"}` |
| POST | `/api/rendiciones/{id}/documents` | multipart `file` + `role=bonificacion_colmena` |
| POST | `/api/rendiciones/{id}/status` | `{"status": "...", "error_message"}` (validado) |
| PATCH/DELETE | `/api/rendiciones/{id}` | editar / eliminar (solo borrador o lista) |
| POST | `/api/rendiciones/{id}/recompute` | descartar valores manuales y recalcular desde los documentos |
| PUT | `/api/rendiciones/{id}/grouping` | `{"confidence", "reason"}` (restaurar agrupación al deshacer) |
| POST | `/api/uploads` | multipart `files[]`, `paths[]`, `batch_id`, `group` |
| POST | `/api/batches/{batch_id}/group` | agrupar un lote subido con `group=false` |
| POST/DELETE | `/api/documents/{id}/move` · `/api/documents/{id}` | mover / eliminar documento |
| PATCH | `/api/documents/{id}` | corregir campos extraídos de un documento |
| POST | `/api/documents/move` | `{"document_ids": [...], "rendicion_id"}` mover varios (null = nueva) |
| GET | `/api/documents/{id}/file` | ver archivo |

Documentación OpenAPI en `http://127.0.0.1:8765/api/docs`.

## Arquitectura

```
src/rendiciones/
  api.py           FastAPI: UI estática, REST, middleware de seguridad (Host, CSRF, CSP)
  cli.py           serve | run-pipeline | login | accounts | bank-check | clear-marker
                   | status | list
  config.py        configuración y creación segura de data/
  services.py      ingesta, agrupación, edición (fachada usada por API y CLI)
  pipeline.py      orquestación de envíos y consultas + record_submission/record_result
  workflow.py      a qué cuentas va cada rendición y en qué orden (data/workflow.json)
  runner.py        pasadas lanzadas desde la interfaz, en un hilo aparte
  locking.py       candado entre procesos para el navegador compartido
  accounts.py      cuentas en la interfaz: credenciales, historial y trabajos de login
  credentials.py   credenciales en el llavero de macOS
  domain.py        tipos de documento, prestaciones, estados y transiciones
  db.py            SQLite (consultas parametrizadas), historial de eventos
  storage.py       almacenamiento por sha256, dedupe, sniffing, saneo de rutas, 25 MB
  grouping.py      agrupación (mismo folio, prestador+fecha, carpeta, orden↔boleta)
  rut.py           validación de RUT
  recognition/     extracción de texto (pypdf, OCR tesseract), reglas, LLM opcional
  bank/            verificación de abonos: cartola.py (parseo de la cartola), matching.py
                   (calce glosa + suma de montos), expected.py (monto esperado),
                   settings.py, bancochile.py (Playwright), fake.py
  automation/      protocolos de portales, clientes falsos, cargador
    browser/       clientes Playwright: selectors.py (selectores y URLs), mappings.py
                   (tablas de prestaciones y parsers), colmena.py, chubb.py, common.py,
                   session.py (contexto persistente), files.py (conversión y límites),
                   login.py, autologin.py (llena el login con la cuenta guardada)
  static/          interfaz (HTML/JS/CSS sin dependencias, módulos ES sin build)
    js/            core.js (helpers, API), feedback.js (toasts, diálogos), flow.js,
                   pipeline.js, flujo.js, cuentas.js
      rendiciones/ store.js (estado y URL), filters.js, list.js, panel.js (panel lateral),
                   batch.js (revisión del lote), upload.js, dnd.js, actions.js, index.js
```

## Seguridad

- Solo `127.0.0.1`; se rechazan encabezados `Host` ajenos (DNS rebinding).
- Escrituras exigen `X-Rendiciones-Client: 1` y `Origin` local (CSRF).
- `data/` con permisos `0700`, archivos y base `0600`; nombres saneados; sin `..` ni rutas
  absolutas; límite de 25 MB; tipo por magic bytes; SQL parametrizado.
- Los logs solo contienen ids y conteos, nunca nombres, RUTs, montos ni contenido.
- Sin credenciales en el código ni en disco: la sesión vive en `browser-profile/` (`0700`);
  el token de Colmena se usa solo dentro de la página, nunca pasa a Python.

## Desarrollo

```bash
uv run pytest
uv run ruff check && uv run ruff format --check
uv run mypy src
```

Los tests usan solo datos sintéticos (`tests/fixtures/texts/`), convertidos a PDF al vuelo.
Los tests de navegador (`-m browser`) corren contra páginas locales que imitan los portales
(`tests/fixtures/portals/`, servidas en `127.0.0.1`); cualquier otro host se bloquea.

## Limitaciones conocidas

- La automatización de portales se construyó desde un mapeo de solo lectura
  (`docs/portals.md`); los pasos de envío nunca se probaron contra los sitios reales. Haz
  primero un `--dry-run --headed`.
- Colmena no tiene reembolso web para medicamentos (salvo GES), dental ni hospitalizaciones:
  esas rendiciones quedan en `error` con el motivo.
- El clasificador por reglas depende de la calidad del texto/OCR; revisa los datos antes de
  marcar una rendición como lista.
- La agrupación es heurística; la confianza y el motivo se muestran en cada tarjeta.
