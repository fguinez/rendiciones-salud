// Shared helpers: DOM building, icons, API calls, formatting and labels.

export const CSRF = { "X-Rendiciones-Client": "1" };

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  let value;
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (k === "value" && (tag === "input" || tag === "select" || tag === "textarea")) value = v ?? "";
    else if (k === "checked" || k === "disabled" || k === "open") node[k] = Boolean(v);
    else if (v === true) node.setAttribute(k, "");
    else if (v !== undefined && v !== null && v !== false) node.setAttribute(k, v);
  }
  append(node, children);
  if (value !== undefined) node.value = value; // after children, so <select> finds its option
  return node;
}

function append(node, children) {
  for (const c of children) {
    if (c == null || c === false) continue;
    if (Array.isArray(c)) append(node, c);
    else node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

const SVG = "http://www.w3.org/2000/svg";
const ICONS = {
  check: [["path", { d: "M3.5 8.5l3 3 6-7" }]],
  x: [["path", { d: "M4 4l8 8M12 4l-8 8" }]],
  alert: [["path", { d: "M8 2.2l6.3 11.3H1.7z" }], ["path", { d: "M8 6.5v3.2M8 11.7v.1" }]],
  clock: [["circle", { cx: 8, cy: 8, r: 6 }], ["path", { d: "M8 4.8V8l2.2 1.4" }]],
  pencil: [["path", { d: "M10.6 2.6l2.8 2.8-7.9 7.9H2.7v-2.8z" }]],
  send: [["path", { d: "M2 7.6L14 2.4 9.6 14l-2-4.6z" }]],
  circle: [["circle", { cx: 8, cy: 8, r: 5.5 }]],
  file: [["path", { d: "M4 1.8h5l3 3v9.4H4z" }], ["path", { d: "M9 1.8v3h3" }]],
  upload: [["path", { d: "M8 10.5V2.5M4.6 5.8L8 2.5l3.4 3.3M2.5 10.5v3h11v-3" }]],
  folder: [["path", { d: "M1.8 3.5h4.4l1.5 1.6h6.5v8.4H1.8z" }]],
  chevronRight: [["path", { d: "M6 3.5L10.5 8 6 12.5" }]],
  chevronDown: [["path", { d: "M3.5 6L8 10.5 12.5 6" }]],
  more: [["circle", { cx: 3.5, cy: 8, r: 0.6 }], ["circle", { cx: 8, cy: 8, r: 0.6 }], ["circle", { cx: 12.5, cy: 8, r: 0.6 }]],
  grip: [["path", { d: "M6 3.5v.1M10 3.5v.1M6 8v.1M10 8v.1M6 12.5v.1M10 12.5v.1" }]],
  search: [["circle", { cx: 7, cy: 7, r: 4.5 }], ["path", { d: "M10.4 10.4L14 14" }]],
  filter: [["path", { d: "M2 3.5h12M4.5 8h7M7 12.5h2" }]],
  refresh: [["path", { d: "M13.5 8A5.5 5.5 0 1 1 12 4.2" }], ["path", { d: "M13.2 1.8v3h-3" }]],
  external: [["path", { d: "M9.5 2.5h4v4M13.5 2.5L7.5 8.5M11.5 9.5v4h-9v-9h4" }]],
  trash: [["path", { d: "M2.5 4.5h11M6.2 4.5V2.6h3.6v1.9M4 4.5l.7 9h6.6l.7-9" }]],
  merge: [["path", { d: "M3 2.5v2.5c0 2 1.5 3 3.5 3H13M10.5 5.5L13 8l-2.5 2.5M3 13.5V10" }]],
  lock: [["rect", { x: 3, y: 7, width: 10, height: 7, rx: 1.5 }], ["path", { d: "M5.5 7V5a2.5 2.5 0 0 1 5 0v2" }]],
  plus: [["path", { d: "M8 3v10M3 8h10" }]],
  play: [["path", { d: "M5 3.2v9.6L12.5 8z" }]],
  move: [["path", { d: "M2.5 8h11M10.5 5l3 3-3 3" }]],
  arrowRight: [["path", { d: "M3 8h9.5M9 4.5L12.5 8 9 11.5" }]],
  undo: [["path", { d: "M5.5 3.5l-3 3 3 3" }], ["path", { d: "M2.5 6.5h7a4 4 0 0 1 0 8h-2" }]],
};

export function icon(name, cls = "") {
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("class", `icon ${cls}`.trim());
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  for (const [tag, attrs] of ICONS[name] || ICONS.circle) {
    const part = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs)) part.setAttribute(k, v);
    svg.append(part);
  }
  return svg;
}

export function errorText(detail) {
  if (Array.isArray(detail)) return detail.map((d) => (typeof d === "string" ? d : d.msg || JSON.stringify(d))).join(" · ");
  return typeof detail === "string" ? detail : JSON.stringify(detail);
}

export async function api(method, url, body) {
  const opts = { method, headers: { ...CSRF } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(url, opts);
  } catch {
    const err = new Error("No hay conexión con la app. Revisa que siga abierta.");
    err.status = 0;
    throw err;
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const fallback = res.status === 404 ? "No encontrado" : res.statusText || `Error ${res.status}`;
    const err = new Error(data.detail ? errorText(data.detail) : fallback);
    err.detail = data.detail;
    err.status = res.status;
    throw err;
  }
  return data;
}

export const fmtMoney = (n) => (n == null ? "—" : "$ " + Number(n).toLocaleString("es-CL"));
/** Canonical ``12345678-9`` RUT shown with thousands dots (``12.345.678-9``). */
export const fmtRut = (rut) => (rut ? rut.replace(/^(\d+)-/, (_, body) => `${Number(body).toLocaleString("es-CL")}-`) : rut);
export const fmtDate = (d) => (d ? d.slice(0, 10).split("-").reverse().join("/") : "—");
export const fmtDateTime = (iso) => (iso ? new Date(iso).toLocaleString("es-CL") : "—");
export const fmtShortDate = (iso) => (iso ? new Date(iso).toLocaleDateString("es-CL") : "");
export const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
export const pct = (x) => `${Math.round((x || 0) * 100)}%`;

export function debounce(fn, ms) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

export const LABELS = {
  status: {
    borrador: "Borrador", lista: "Lista para enviar", colmena_enviada: "Enviada a Colmena",
    colmena_aprobada: "Aprobada por Colmena", colmena_rechazada: "Rechazada por Colmena",
    chubb_enviada: "Enviada a Chubb", chubb_aprobada: "Aprobada por Chubb",
    chubb_rechazada: "Rechazada por Chubb", error: "Error",
  },
  doc: {
    boleta_electronica: "Boleta electrónica", boleta_honorarios: "Boleta de honorarios",
    factura: "Factura", orden_medica: "Orden médica", receta: "Receta",
    programa_medico: "Programa médico / presupuesto", informe_examen: "Informe / resultado",
    comprobante_bonificacion: "Comprobante bonificación", documento_chubb: "Documento Chubb",
    otro: "Otro",
  },
  tipo: {
    consulta_medica: "Consulta médica", examenes_laboratorio: "Exámenes de laboratorio",
    imagenologia: "Imagenología", procedimiento: "Procedimiento", medicamentos: "Medicamentos",
    dental: "Dental", otros_profesionales: "Kinesiología / psicología / otros",
    lentes: "Lentes", hospitalizacion: "Hospitalización", otro: "Otro",
  },
  event: {
    creada: "Creada", editada: "Datos editados", estado: "Cambio de estado",
    documentos_agregados: "Documentos agregados", documento_movido: "Documento movido",
    documento_quitado: "Documento quitado", documento_editado: "Documento editado",
    documento_adjuntado: "Documento adjuntado", valores_desbloqueados: "Datos recalculados desde los documentos",
    comprobante_adjuntado: "Comprobante adjuntado", agrupacion_restaurada: "Agrupación restaurada",
    liquidacion_adjuntada: "Liquidación de Chubb adjuntada",
    colmena_enviada: "Enviada a Colmena", colmena_resultado: "Resultado de Colmena",
    chubb_enviada: "Enviada a Chubb", chubb_resultado: "Resultado de Chubb",
    dry_run: "Prueba sin enviar", error: "Error",
    abono_abonado: "Abono confirmado en el banco", abono_no_llego: "El abono no llegó a tiempo",
  },
  role: { bonificacion_colmena: "Comprobante Colmena", respuesta_chubb: "Liquidación Chubb" },
};

/** Ordered option values; refreshed from GET /api/meta when available. */
export const ENUMS = {
  status: Object.keys(LABELS.status),
  tipo: Object.keys(LABELS.tipo),
  doc: Object.keys(LABELS.doc),
};

const META_KEYS = { statuses: "status", tipos_prestacion: "tipo", doc_types: "doc" };

/** Merge enums (and labels, if the server sends them) from /api/meta. */
export async function loadMeta() {
  let meta;
  try {
    meta = await api("GET", "/api/meta");
  } catch {
    return; // keep the built-in labels
  }
  for (const [key, target] of Object.entries(META_KEYS)) {
    const values = meta[key];
    if (!Array.isArray(values) || !values.length) continue;
    ENUMS[target] = values.map((v) => (typeof v === "string" ? v : v.value));
    for (const v of values) if (typeof v === "object" && v.label) LABELS[target][v.value] = v.label;
  }
  for (const [key, target] of [["status_labels", "status"], ["tipo_labels", "tipo"], ["doc_labels", "doc"]]) {
    if (meta[key] && typeof meta[key] === "object") Object.assign(LABELS[target], meta[key]);
  }
}

export const statusLabel = (s) => LABELS.status[s] || s;
export const tipoLabel = (t) => (t ? LABELS.tipo[t] || t : null);
export const docLabel = (t) => LABELS.doc[t] || t || "Documento";

/** Saved workflow shared by the pipeline bar, the editor and the accounts view. */
export const shared = { workflow: { stages: [["colmena"], ["chubb"]], accounts: [] } };
export const accountLabel = (key) => (shared.workflow.accounts.find((a) => a.key === key) || {}).label || key;

/** Re-render, then refocus the element with the same ``data-focus-key`` if it was replaced. */
export function keepFocus(render) {
  const active = document.activeElement;
  const key = active && active.dataset ? active.dataset.focusKey : null;
  render();
  if (!key || (active.isConnected && document.activeElement === active)) return;
  const next = document.querySelector(`[data-focus-key="${CSS.escape(key)}"]`);
  if (next) next.focus({ preventScroll: true });
}
