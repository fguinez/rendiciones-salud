// Rendiciones state: API data, the URL-backed query and the rules derived from them.

import { api, fmtDate, LABELS, statusLabel, tipoLabel } from "../core.js";

export const EDITABLE = new Set(["borrador", "lista"]);
// Grouping scores: 0.95 folio, 0.85 RUT+fecha, 0.75 carpeta, 0.7 orden↔boleta same day, 0.6 otherwise.
export const LOW_GROUPING = 0.7;
export const LOW_DOC = 0.7;

export const state = {
  items: [],
  byId: new Map(),
  docOwner: new Map(), // document id -> rendición id
  loaded: false,
  loadError: null,
  query: emptyQuery(),
  selected: new Set(),
};

const listeners = new Set();
export const subscribe = (fn) => { listeners.add(fn); return () => listeners.delete(fn); };
export function notify() { for (const fn of listeners) fn(); }

function setItems(items) {
  state.items = items;
  state.byId = new Map(items.map((r) => [r.id, r]));
  state.docOwner = new Map(items.flatMap((r) => r.documents.map((d) => [d.id, r.id])));
  for (const id of [...state.selected]) if (!state.byId.has(id)) state.selected.delete(id);
}

let inflight = null;
let trailing = null;

function load() {
  inflight = (async () => {
    try {
      setItems(await api("GET", "/api/rendiciones"));
      state.loaded = true;
      state.loadError = null;
    } catch (err) {
      state.loadError = err.message;
    } finally {
      inflight = null;
    }
    notify();
  })();
  return inflight;
}

/** Re-fetch every rendición and re-render; the fetch always starts after this call. */
export function refresh() {
  if (trailing) return trailing;
  if (!inflight) return load();
  const next = () => { trailing = null; return load(); };
  trailing = inflight.then(next, next);
  return trailing;
}

/** Re-fetch one rendición (cheaper after editing a document). */
export async function refreshOne(id) {
  try {
    const fresh = await api("GET", `/api/rendiciones/${id}`);
    setItems(state.items.map((r) => (r.id === id ? fresh : r)));
  } catch (err) {
    if (err.status !== 404) throw err;
    setItems(state.items.filter((r) => r.id !== id));
  }
  notify();
}

export const docById = (docId) => {
  const r = state.byId.get(state.docOwner.get(docId));
  return r ? r.documents.find((d) => d.id === docId) : null;
};

export const supportDocs = (r) => r.documents.filter((d) => d.role === "soporte");
export const isEditable = (r) => EDITABLE.has(r.status);

export function missingFields(r) {
  return [
    !r.tipo_prestacion && "tipo de prestación",
    !r.fecha_atencion && "fecha",
    r.monto == null && "monto",
  ].filter(Boolean);
}

/** Why a rendición cannot move forward, most important first. */
export function blockers(r) {
  const out = [];
  if (r.status === "error") out.push({ level: "bad", text: r.error_message ? `Error: ${r.error_message}` : "Falló el último intento" });
  if (r.status === "colmena_rechazada") out.push({ level: "bad", text: "Colmena la rechazó: corrígela y vuelve a enviarla" });
  if (r.status === "chubb_rechazada") out.push({ level: "bad", text: "Chubb la rechazó" });
  if (r.status === "borrador") {
    for (const f of missingFields(r)) out.push({ level: "warn", text: `Falta ${f}` });
    if (r.grouping_confidence < LOW_GROUPING) {
      out.push({ level: "warn", text: `Confianza baja de agrupación (${Math.round(r.grouping_confidence * 100)}%): ${r.grouping_reason}` });
    }
  }
  if (isEditable(r)) {
    const doubtful = supportDocs(r).filter((d) => (d.confidence ?? 1) < LOW_DOC && !(d.edited_fields || []).length);
    if (doubtful.length) {
      out.push({ level: "warn", text: doubtful.length === 1 ? "1 documento leído con dudas" : `${doubtful.length} documentos leídos con dudas` });
    }
  }
  return out;
}

const SENT = new Set(["colmena_enviada", "chubb_enviada"]);
const ACTIVE_NODE = new Set(["pendiente", "por_enviar", "en_revision"]);

function finished(r) {
  if (r.status === "chubb_aprobada") return true;
  if (r.status !== "colmena_aprobada") return false;
  return !(r.progress || []).some((n) => n.in_workflow && ACTIVE_NODE.has(n.state));
}

export const VIEWS = [
  { key: "atencion", label: "Requiere atención", short: "Atención", test: (r) => blockers(r).length > 0 && r.status !== "lista" },
  { key: "borradores", label: "Borradores", short: "Borradores", test: (r) => r.status === "borrador" },
  { key: "listas", label: "Listas para enviar", short: "Listas", test: (r) => r.status === "lista" },
  { key: "curso", label: "En curso", short: "En curso", test: (r) => SENT.has(r.status) || (r.status.endsWith("_aprobada") && !finished(r)) },
  { key: "terminadas", label: "Terminadas", short: "Terminadas", test: finished },
  { key: "todas", label: "Todas", short: "Todas", test: () => true },
];
export const viewByKey = (key) => VIEWS.find((v) => v.key === key);

/** View shown when the URL does not name one. */
export function defaultView() {
  return state.items.some(VIEWS[0].test) ? "atencion" : "todas";
}
export const currentView = () => state.query.view || defaultView();

export function emptyQuery() {
  return { view: null, tipo: [], desde: "", hasta: "", min: "", max: "", prestador: "", q: "", orden: "", ids: [], r: null };
}

export const SORTS = [
  { key: "fecha-desc", label: "Fecha (más reciente)" },
  { key: "fecha-asc", label: "Fecha (más antigua)" },
  { key: "monto-desc", label: "Monto (mayor)" },
  { key: "monto-asc", label: "Monto (menor)" },
  { key: "id-desc", label: "N° (más nuevo)" },
  { key: "id-asc", label: "N° (más antiguo)" },
  { key: "estado-asc", label: "Estado" },
];
export const DEFAULT_SORT = "fecha-desc";

export function providerOf(r) {
  const names = supportDocs(r).map((d) => d.provider_name).filter(Boolean);
  return names[0] || null;
}

export function providers() {
  const set = new Set(state.items.flatMap((r) => r.documents.map((d) => d.provider_name).filter(Boolean)));
  return [...set].sort((a, b) => a.localeCompare(b, "es"));
}

function searchText(r) {
  return [
    `#${r.id}`, String(r.id), tipoLabel(r.tipo_prestacion), statusLabel(r.status), r.monto, fmtDate(r.fecha_atencion),
    ...r.documents.flatMap((d) => [d.original_name, d.provider_name, d.folio, d.monto, LABELS.doc[d.doc_type]]),
    ...(r.progress || []).map((n) => n.request_id),
  ].filter((v) => v != null).join(" ").toLowerCase();
}

/** Every filter except the quick view. */
export function matchesFacets(r, q = state.query) {
  if (q.ids.length && !q.ids.includes(r.id)) return false;
  if (q.tipo.length && !q.tipo.includes(r.tipo_prestacion || "")) return false;
  if (q.desde && (!r.fecha_atencion || r.fecha_atencion < q.desde)) return false;
  if (q.hasta && (!r.fecha_atencion || r.fecha_atencion > q.hasta)) return false;
  if (q.min !== "" && (r.monto == null || r.monto < Number(q.min))) return false;
  if (q.max !== "" && (r.monto == null || r.monto > Number(q.max))) return false;
  if (q.prestador && !r.documents.some((d) => d.provider_name === q.prestador)) return false;
  if (q.q && !searchText(r).includes(q.q.toLowerCase())) return false;
  return true;
}

const STATUS_ORDER = ["error", "colmena_rechazada", "chubb_rechazada", "borrador", "lista", "colmena_enviada",
  "colmena_aprobada", "chubb_enviada", "chubb_aprobada"];

export function sortItems(items, orden = state.query.orden || DEFAULT_SORT) {
  const [key, dir] = orden.split("-");
  const sign = dir === "asc" ? 1 : -1;
  const value = {
    fecha: (r) => r.fecha_atencion || "",
    monto: (r) => r.monto ?? -1,
    id: (r) => r.id,
    estado: (r) => STATUS_ORDER.indexOf(r.status),
  }[key] || ((r) => r.id);
  return [...items].sort((a, b) => {
    const va = value(a);
    const vb = value(b);
    if (va < vb) return -sign;
    if (va > vb) return sign;
    return b.id - a.id;
  });
}

export function visibleItems() {
  const view = viewByKey(currentView()) || VIEWS.at(-1);
  return sortItems(state.items.filter((r) => matchesFacets(r) && view.test(r)));
}

export const hasFacets = (q = state.query) =>
  Boolean(q.ids.length || q.tipo.length || q.desde || q.hasta || q.min !== "" || q.max !== "" || q.prestador || q.q);

const LIST_KEYS = ["tipo", "ids"];

export function parseHash(hash = location.hash) {
  const raw = hash.replace(/^#/, "");
  const [name, search = ""] = raw.split("?");
  return { name, params: new URLSearchParams(search) };
}

export function queryFromParams(params) {
  const q = emptyQuery();
  for (const key of Object.keys(q)) {
    const v = params.get(key);
    if (v == null || v === "") continue;
    if (key === "tipo") q.tipo = v.split(",").filter(Boolean);
    else if (key === "ids") q.ids = v.split(",").map(Number).filter(Number.isInteger);
    else if (key === "r") q.r = Number(v) || null;
    else q[key] = v;
  }
  return q;
}

function hashFromQuery(q) {
  const params = new URLSearchParams();
  for (const [key, v] of Object.entries(q)) {
    if (LIST_KEYS.includes(key)) { if (v.length) params.set(key, v.join(",")); }
    else if (v != null && v !== "") params.set(key, String(v));
  }
  const s = params.toString().replaceAll("%2C", ",");
  return "#rendiciones" + (s ? "?" + s : "");
}

/** Update the query, mirror it in the URL (``push`` adds a history entry) and re-render. */
export function setQuery(patch, { push = false } = {}) {
  state.query = { ...state.query, ...patch };
  const hash = hashFromQuery(state.query);
  if (hash !== location.hash) history[push ? "pushState" : "replaceState"](null, "", hash);
  sessionQuery = hash;
  notify();
}

/** Last rendiciones URL of this session, to come back to it from other tabs. */
export let sessionQuery = "";

/** Read the query from the URL (on load, back/forward). Returns true if it changed. */
export function syncFromUrl() {
  const next = queryFromParams(parseHash().params);
  const changed = JSON.stringify(next) !== JSON.stringify(state.query);
  state.query = next;
  sessionQuery = location.hash;
  return changed;
}
