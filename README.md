# Rendiciones de salud

App local para rendir reembolsos de salud: envía cada rendición a la Isapre (Colmena) y,
una vez aprobada, a Chubb con el comprobante de bonificación. Corre solo en `127.0.0.1`.

## Requisitos

- macOS, [uv](https://docs.astral.sh/uv/) y Python 3.12
- Tesseract para OCR: `brew install tesseract tesseract-lang`

## Aviso

Este proyecto no está afiliado a Isapre Colmena, Chubb ni Banco de Chile, ni cuenta con su
respaldo. Automatiza sus sitios con tu propia sesión y tus credenciales, lo que puede ir
contra sus términos de uso: úsalo bajo tu propio riesgo.

- Se entrega sin garantía de ningún tipo (ver [LICENSE](LICENSE)).
- Tú eres responsable de revisar lo que envía. Prueba primero con `make dry-run` o
  **Probar sin enviar**.
- Los portales cambian y la automatización puede dejar de funcionar en cualquier momento.
- Si hay una API key de Anthropic, el clasificador (`auto` o `llm`) envía tus documentos
  médicos a Anthropic.

## Inicio rápido

```bash
make install   # dependencias + Chromium de Playwright
make serve     # http://127.0.0.1:8765
```

Sube documentos, revisa cómo se agruparon y marca las rendiciones como **lista**. Guarda
las cuentas de los portales en la pestaña **Cuentas**.

## Comandos

| Comando | Qué hace |
| --- | --- |
| `make serve` | Levanta la interfaz |
| `make demo` | Interfaz con portales falsos (no envía nada) |
| `make dry-run` | Llena los formularios de los portales sin enviar |
| `make run` | Envía las rendiciones listas y consulta estados |
| `make monitor` | Repite `run` cada hora |
| `make login PORTAL=chubb` | Inicia sesión en un portal (`colmena`, `chubb`) |
| `make accounts ARGS="set colmena"` | Guarda la cuenta de un portal en el llavero (sin `ARGS`, las lista) |
| `make status` · `make list` | Resumen y listado de rendiciones |
| `make bank-check` | Lista abonos de aseguradoras en la cartola del Banco de Chile |
| `make privacy` | Revisa que no haya datos personales en los archivos versionados |
| `make check` | Lint, tipos, datos personales y tests |

`make help` muestra todos. Pasa flags extra con `ARGS`, por ejemplo
`make run ARGS="--headed"`. El CLI completo está en `uv run rendiciones --help`.

> [!WARNING]
> `make run` envía de verdad. Prueba primero con `make dry-run`.

## Configuración

Opcional, por variables de entorno (ver [`.env.example`](.env.example)):

| Variable | Por defecto | |
| --- | --- | --- |
| `RENDICIONES_DATA_DIR` | `data/` del proyecto | Documentos y base SQLite |
| `RENDICIONES_PORT` | `8765` | Puerto de la interfaz |
| `RENDICIONES_CLASSIFIER` | `auto` | `auto`, `rules` o `llm` (Claude, si hay API key; envía los documentos a Anthropic) |

Las contraseñas de los portales y la API key de Anthropic se guardan en el llavero de macOS,
nunca en disco. Cómo guardar la API key: [Clasificador con IA](docs/guide.md#clasificador-con-ia-opcional).

## Documentación

- [Guía de uso](docs/guide.md): envío a portales, verificación de abonos, clasificador IA,
  estados, API REST, arquitectura y seguridad
- [Mapeo de portales](docs/portals.md)

## Licencia

[PolyForm Noncommercial 1.0.0](LICENSE): puedes usarlo, modificarlo y compartirlo, pero no con fines comerciales.
