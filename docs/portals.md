# Health reimbursement portals: automation map

Mapped read-only on 2026-11-05 in the user's Chrome (logged-in sessions). Nothing was submitted, uploaded, or ticked.
Personal data is replaced with placeholders (`<RUT>`, `<FOLIO>`, `<AMOUNT>`, `<DATE>`).

---

## 1. Colmena (Isapre)

- **Framework:** Angular 6.1 (`app-root[ng-version="6.1.0"]`), PrimeNG widgets (`p-dropdown`, `p-checkbox`, `p-radioButton`, `p-calendar`, `p-growl`), hash routing (`/afiliados/#/...`). No iframes or shadow DOM.
- **Buttons are not `<button>` elements.** Use `app-boton-colmena[label="Continuar"] .wrapper`, `app-boton-colmena[label="Cancelar"] .wrapper` and similar. Neither role=button nor `getByRole('button')` matches them. Use `getByText('Continuar', {exact:true})` or the attribute selector.
- **Auth:** the app keeps its own session token in the browser. The client never reads it in Python: its few direct requests (status JSON, comprobante PDF) run inside the page, so they reuse the app's own session.
- **Session:** the session was still active after the earlier run. Deep links work on a full page load.

### 1.1 Solicitar: `#/reembolso/solicitar` (component `app-solicitar-reembolso-terminos`)

**Screen 0: bank account confirmation.** Shows the registered bank account (bank, account type, masked number) plus a "Modificar datos" link (`div.edit-datos`). Click `app-boton-colmena[label="Continuar"]` to advance. This step does not submit anything.

**Screen 1: the form.** One page, two numbered steps (`li.paso`).

1. **Tipo de prestación:** `p-dropdown[formcontrolname="tipoPrestacion"]`. Open it with `p-dropdown[formcontrolname=tipoPrestacion] .ui-dropdown`, then click `.ui-dropdown-item` by its exact text.
   - **Sub-types:** a cascading `p-dropdown[formcontrolname="subType"]` appears inside `li[formarrayname="SubTiposSelect"]`. It can be 2 levels deep (for example Exámenes > Procedimiento > Colonoscopía). Each level loads from the server after the previous pick, so wait for the next dropdown or for the document blocks. A leaf shows the document blocks, each one obligatorio, opcional or hidden (no solicitado) depending on the prestación (see the table below).
   - **Beneficiary:** there is **no beneficiary selector**. Colmena infers the beneficiary from the documents.
2. **Documentos:** `.box-adjunto` blocks, each holding one `input.file[type=file][name="doc1"]` (the name is the same on all three, so select by position or by the label `label.input-label`). Upload button: `.box-adjuntar` ("Agregar"). Delete: `.instacia-editar .box-eliminar`. No `accept` attribute is set. The UI says Jpg, Pdf or Png. The page enforces a **5 MB total** limit and a 10-byte minimum. Blocks in order:
   - [0] `boleta`: "Boleta o factura" (or "Boleta, factura o comprobante de pago" for Extranjero). Single file.
   - [1] `ordenPago`: "Detalle de Prestación". Has `multiple`. Tooltip: a Transbank voucher must include the itemised charge or the request is rejected.
   - [2] `ordenMedica`: "Orden Médica". Hidden when the document is NO SOLICITADO.
   - **On file select the page may upload the file right away** to Colmena's document-validation service. This creates server-side state before the final submit, so dry-run never touches a file input. Validation errors appear as toasts in `p-growl`.
   - **The validation upload does not always happen.** On load the page asks the server which processing flow to use, and in one of them the files go only with Continuar. Some prestaciones (GES, Extranjero, TEA, most lab/procedure/Otras Atenciones leaves) always skip it. The flow seems to change between page loads (inferred from the 2026-11-25 runs, not observed directly), so the client waits for the validation only if its upload actually starts.
3. **T&C:** `.box-terminos-condiciones p-checkbox[binary=true]` (inner `input[type=checkbox]`). The "términos y condiciones" link downloads the T&C document.
4. **Continuar** (`app-boton-colmena[label="Continuar"]`, theme `boton-continuar`) creates the request on the server with its files. Then:
   - If the user has **no excedentes**, it **finalises immediately**. **In that case Continuar is the effective submit.**
   - Otherwise it shows the **excedentes step** (`div.box-uso-excedentes`, heading "Solicitar Reembolso excedentes"): "Tienes $<AMOUNT> de excedentes. ¿Quieres utilizar la devolución de excedentes como una bonificación adicional?". Radios: `p-radioButton[formcontrolname="excedentesDecision"][value="S"]` (Sí) or `[value="N"]` (No). Then `app-boton-colmena[label="Continuar"]` finalises the request.
5. **Confirmation:** `div.detalleUsoExcedentes` shows "¡Hemos recibido tu solicitud!" and "Nro de Solicitud <FOLIO>".

**Prestación tree** (leaf ids; docs as boleta/ordenPago/ordenMedica; O=obligatorio, P=opcional, –=no solicitado):

| Tipo (id) | Sub-tipo (id) | boleta | detalle (ordenPago) | orden médica |
|---|---|---|---|---|
| Consultas (médicas, psicológicas, psiquiátricas) (3448) | (leaf) | O | P | – |
| Exámenes de laboratorio, Imágenes y Procedimientos (3449) | Procedimiento (3452) > Otro procedimiento 3457 / Colonoscopía 3458 / Endoscopía 3459 | O | O | O |
| | Laboratorio (3453) > COVID19 3469 | O | O | P |
| | Laboratorio > Otros Exámenes 3470 | O | O | O |
| | Imágenes 3454 | O | O | O |
| Otras Atenciones (3450) | Lentes Ópticos 3480 | O | O | O |
| | Kinesiología, fonoaudiología o Nutricionista (3481) > Kinesiología 3488 / Fonoaudiología 3489 | O | O | O |
| | … > Nutricionista 3490 | O | P | P |
| | Terapia Ocupacional 3482; Prótesis/Órtesis o dispositivos Médicos 3483 | O | O | O |
| | Atención de Urgencia 3484 | O | O | – |
| | Atención de Matronería 3536 | O | P | (n/a) |
| GES (3451)* | Consulta GES 3508 | O | P | – |
| | Medicamentos GES 3509 / Procedimientos GES 3510 | O | P | O |
| Atención Médica Ambulatoria en el Extranjero (3537) | (leaf) | O | P | P |
| TEA (10100) | Psiquiatria 10101 / Psicologia 10102 | O | P | P |
| | Fonoaudiologia 10103 / Kinesiologia 10104 / Terapia Ocupacional 10105 | O | P | O |

\*GES is in Colmena's prestación catalogue but may not be offered in the UI dropdown. Without it, the UI shows 5 options: Consultas, Exámenes…, Otras Atenciones, Atención Médica Ambulatoria en el Extranjero, TEA.

The page notes that resolution takes up to 5 business days (20 for procedures, urgencias, extranjero and GES) and payment up to 2 business days after approval. Hospital bills must be filed at a branch.

### 1.2 Consultar: `#/reembolso/consultar` (component `app-consultar-solicitudes-listado`)

- **Search by folio:** `input[formcontrolname="folioSolicitud"]` (digits, max length 9), then the "Buscar" button next to it.
- **Search by date:** two `p-calendar[dateformat="dd/mm/yy"]` inputs (Desde, Hasta; readonly text inputs, so pick from the calendar), then `app-boton-colmena[label="Buscar"]`.
- **Default list** ("Tus Últimos Reembolsos"): all recent requests, which can be dozens on an active account.
- **Statuses:** the list is backed by plain JSON, so **the client reads that JSON from inside the page** instead of scraping the cards (URL in `selectors.py`, parsing in `mappings.py`).
- **Card UI:** one `app-card-reembolso` per request. Shows "Solicitud <FOLIO>", "Fecha solicitud", a status badge `app-mark[theme=...]`, then:
  - Aprobada: Banco, Cuenta bancaria, Monto bonificado, Fecha de pago.
  - Rechazada: an "Observación" text.
- **Status values:** observed "Aprobada" (`app-mark[theme=aceptada]`) and "Rechazada" (`theme=rechazada`). The app also has "Ingresada", "En revisión" and "Por corregir". With "Por corregir", the user re-uploads documents from the portal.
- **Comprobante de bonificación:** shown on approved cards only, as `app-card-reembolso app-boton-colmena[label="Comprobante reembolso"]` (theme `boton-descargar-reembolso`). The button asks Colmena's report service for a PDF. **The client makes that same request from inside the page** instead of clicking, so it reuses the app's session. The "Ya descargó los documentos…" throttle is a client-side flag on the button, not a server limit. The submitted documents come from `app-boton-colmena[label="Descargar documentos"]`.

---

## 2. Chubb: Seguro Complementario de Salud (colectivos-empresas.cl.chubb.com)

- **Framework:** Angular 20.3 (Ivy), Angular Material MDC (`mat-vertical-stepper`, `mat-select`, `mat-radio-group`, `mat-datepicker`, `mat-paginator`). No iframes or shadow DOM.
- **Auth:** Azure AD B2C single sign-on (`auth.chubb.com`). The session state lives in **sessionStorage (per tab)**.
  - A new tab or a hard reload of a `/private/*` deep link first shows "No tiene permiso para acceder al portal de asegurado". It then re-auths silently via SSO (when the B2C cookie is valid) and lands on `/private/home`, **not on the deep link**.
  - Navigate inside the SPA instead: on `/private/home`, click the "Solicitar un reembolso" tile, or click the "Historial de Reembolsos" button.
  - Once, clicking a header nav link sent an expired session to the public home `/` and required a fresh login.
  - For Playwright, reuse a persistent context and drive navigation via in-app clicks.
- **API responses are encrypted** and decrypted in the browser. **Scrape the DOM** rather than calling the API directly.

### 2.1 Solicitar: `/private/refund` (component `app-refund`)

A linear `mat-vertical-stepper` with 3 steps: 1 Beneficiario, 2 Datos de reembolsos, 3 Información de pago.

1. **Beneficiario:** `mat-radio-group[formcontrolname="personalIdBeneficiary"][aria-label="Seleccionar beneficiario"]` with one `app-user-select .card` per insured person (name, Titular/Beneficiario tag, masked RUT). The radio `value` is the beneficiary's RUT. Clicking a card reveals step 2.
2. **Datos de reembolsos:**
   - `mat-select[formcontrolname="Product"]` (label "Selecciona producto a reembolsar"). Options depend on the policies the user holds (one product per policy, e.g. a health or a dental product).
   - `mat-select[formcontrolname="Benefit"]` (label "Prestación a reembolsar"):
     - Health product: Consulta médica y exámenes, Medicamentos, Lentes y Marcos, Prótesis y ortesis, Cirugías y hospitalizaciones, Accidentes, Urgencias.
     - Dental product: Reembolso de gastos dentales.
   - Pick an option with `page.locator('mat-select[formcontrolname=Benefit]').click()` and then `getByRole('option', {name})`.
   - **Fecha de la atención:** `input[formcontrolname="FechaAtencion"]` (matDatepicker; `min` = today − 60 days, `max` = today). The calendar toggle is `button[aria-label="Open calendar"]`. **Typed input is parsed as MM/DD/YYYY** (the native DateAdapter): "10/15/2026" is accepted and displayed as 15/10/2026, while "15/10/2026" is invalid. An ISO string shifts by −1 day (UTC). Type `MM/DD/YYYY` or use the calendar.
   - **"¿Reembolsaste previamente en tu Isapre/Fonasa?":** `mat-radio-group[formcontrolname="reembolsadoPreviamente"]` with `input[value="si"]` or `input[value="no"]`. This choice changes which document is mandatory.
   - **Amount fields: none.** The form never asks for amounts; Chubb takes them from the documents.
   - **Documents:** `app-document-section .doc-grid .doc-card`. Each card has a title, an Obligatorio/Opcional tag, `button.doc-card__attach` ("Adjuntar documentos"), a help button `button.doc-card__btn[aria-label="Ayuda"]`, and a hidden `input.hidden[type=file][multiple][accept="image/png, image/jpeg, image/gif, image/bmp, application/pdf"]`. Call `setInputFiles` on the `input[type=file]` inside the card whose title matches. The page enforces **10 MB per file** (10,485,760 bytes). Files stay in the browser until the final submit.
3. **Información de pago:** if the user has a registered bank account, the step shows it. Otherwise it has `Bank`, `AccountType` and `AccountNumber` controls. This step unlocks only after the mandatory docs are attached, so it was not opened.
4. **Submit:** `button.send-request` ("Enviar solicitud", disabled until valid) opens a **confirmation dialog**. On confirm the page sends the whole request, all files included, in a single upload. The page generates the claim number (denuncio) itself, so the client reads it from that upload (path in `selectors.py`) and falls back to the confirmation text.
   - There is **no T&C checkbox** on Chubb.

**Required documents** (* = Obligatorio, ? = Opcional) for "No" and "Sí" (reembolsado previamente):

| Prestación | No | Sí |
|---|---|---|
| Consulta médica y exámenes | *Boleta o factura, ?Bono o boleta Isapre/Fonasa, ?Derivación del médico, ?Otros | *Bono o Bonificación Isapre/Fonasa, ?Boleta o factura, ?Derivación del médico, ?Otros |
| Medicamentos | *Boleta Farmacia, *Receta Médica, ?Certificado/Declaración Médica, ?Otros | same |
| Lentes y Marcos | *Orden Médica, *Detalle de cristales o marcos, *Boleta o factura, ?Bono Isapre/Fonasa, ?Otros | *Bono Isapre/Fonasa, *Orden Médica, *Detalle de cristales o marcos, ?Boleta, ?Otros |
| Prótesis y ortesis | *Orden Médica, *Boleta o factura, ?Bono, ?Otros | *Bono, *Orden Médica, ?Boleta, ?Otros |
| Cirugías y hospitalizaciones | *Protocolo Operatorio, *Epicrisis, *Boleta o factura, *Prefacturas valorizadas, ?Bono, ?Resultados de exámenes, ?Otros | same, but Bono is * |
| Accidentes | *Boleta o factura, *Hoja de atención de urgencias, *Declaración Accidente, *Detalle atención valorizado, ?Bono, ?Otros | *Bono, *Hoja de urgencias, *Declaración Accidente, *Detalle valorizado, ?Boleta, ?Otros |
| Urgencias | *Boleta o factura, *Hoja de atención de urgencias, *Declaración Accidente, ?Bono, ?Otros | *Bono, *Hoja de urgencias, *Declaración Accidente, ?Boleta, ?Otros |
| Dental: Reembolso de gastos dentales | *Múltiples documentos | same |

The site notes that a status is visible 48 business hours after submission.

### 2.2 Historial: `/private/refund-history` (component `app-refund-history`)

- **Filters:** `mat-select[formcontrolname="beneficiary"]`, `[formcontrolname="state"]` (Todos, Aceptada, Pendiente, Rechazada, Pagado, En liquidación), `[formcontrolname="month"]`, `[formcontrolname="year"]`. Filtering is client-side. Clear with the "Limpiar filtros" button.
- **Table:** `table.table`, columns: Nombre beneficiario | Fecha (DD-MM-YYYY) | Nombre o ID de reembolso | Tipo de reembolso (e.g. "Salud") | Monto | Estado | Reembolso.
  - Paginated with `mat-paginator` (default 5 per page). Increase the page size or click next.
  - A second "Denuncios" section follows below the table.
- **Estado labels:** the filter offers Aceptada, Pendiente, Rechazada, Pagado and En liquidación, but the table shows only three labels (state → label):
  - Rechazada → "Rechazada"
  - En liquidación → "En Proceso"
  - Aceptada or Pagado → "Pagado"
  - Pendiente or En Proceso → "En Proceso"
- **Liquidación (settlement) PDF:** the last column has `button[mattooltip^="Click para descargar informe de liquidac"]`. It is hidden for Pendiente, En Proceso and En liquidación. Clicking it fetches the PDF and saves it as `liquidación-<nro_siniestro_cliente>.pdf`; the client clicks it and takes the PDF from the download or from the page's own response.

---

## Blockers / notes

- **CAPTCHA and 2FA:** none seen on either site while logged in. Login itself was not exercised: Chubb uses B2C (may have MFA), and Colmena's login was not touched.
- **Colmena: no draft or preview step.**
  - Selecting a file may already upload it for validation.
  - With the T&C box ticked, the first "Continuar" can finalise the request outright when the user has no excedentes.
  - Automation must treat the T&C box plus Continuar as the commit point.
- **Chubb session fragility:** sessionStorage is per tab, deep links redirect to home, and the API is encrypted. Use a persistent browser profile and in-app navigation.
- **Colmena buttons** are custom `app-boton-colmena` divs, not real buttons.
