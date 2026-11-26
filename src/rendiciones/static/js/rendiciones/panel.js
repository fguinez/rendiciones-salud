// Side panel for one rendición: blockers, status actions, fields, documents and history.

import {
  $, api, docLabel, el, ENUMS, fmtDate, fmtDateTime, fmtMoney, fmtRut, icon, keepFocus, LABELS, pct, plural,
  statusLabel, tipoLabel,
} from "../core.js";
import { renderFlow, eventExtra } from "../flow.js";
import { busy, openMenu, toast } from "../feedback.js";
import {
  deleteDocument, deleteRendiciones, failed, moveDocs, patchDocument, patchRendicion, recompute, setStatus,
} from "./actions.js";
import { endDocDrag, startDocDrag } from "./dnd.js";
import { statusBadge } from "./list.js";
import { blockers, isEditable, LOW_DOC, LOW_GROUPING, missingFields, setQuery, state, subscribe } from "./store.js";

const panel = { shownId: null, expanded: new Set(), drafts: new Map(), errors: new Map() };
const NARROW = window.matchMedia("(max-width: 899px)");

export function closePanel() {
  if (state.query.r != null) setQuery({ r: null });
}

function hide() {
  const drawer = $("#panel");
  if (drawer.hidden) return;
  const id = panel.shownId;
  drawer.hidden = true;
  drawer.replaceChildren();
  panel.shownId = null;
  document.body.classList.remove("panel-open");
  setInert(false);
  const row = document.querySelector(`[data-focus-key="row-${id}"]`);
  if (row && (drawer.contains(document.activeElement) || document.activeElement === document.body)) row.focus();
}

function setInert(on) {
  for (const sel of [".topbar", "#view-rendiciones"]) $(sel).inert = on;
}

const valueOf = (v) => (v == null ? "" : String(v));

/** Bind a form to a draft so typing survives data refreshes. Returns a dirty() fn. */
function bindDraft(form, key, original, onDirty) {
  const draft = panel.drafts.get(key);
  if (draft) for (const [name, v] of Object.entries(draft)) if (form.elements[name]) form.elements[name].value = v;
  const dirty = () => panel.drafts.has(key);
  form.addEventListener("input", (e) => {
    const name = e.target.name;
    if (!(name in original)) return;
    const d = { ...(panel.drafts.get(key) || {}) };
    if (e.target.value === original[name]) delete d[name]; else d[name] = e.target.value;
    if (Object.keys(d).length) panel.drafts.set(key, d); else panel.drafts.delete(key);
    onDirty(dirty());
  });
  onDirty(dirty());
  return dirty;
}

function discard(key) {
  panel.drafts.delete(key);
  panel.errors.delete(key);
  render();
}

function field(label, input, { tag, error, hint } = {}) {
  const id = input.id || `f-${Math.random().toString(36).slice(2, 9)}`;
  input.id = id;
  if (error) { input.setAttribute("aria-invalid", "true"); input.setAttribute("aria-describedby", `${id}-err`); }
  return el("div", { class: `field${error ? " invalid" : ""}` },
    el("label", { for: id }, el("span", {}, label), tag || null),
    input,
    error ? el("span", { class: "field-error", id: `${id}-err` }, error) : null,
    hint ? el("span", { class: "field-hint" }, hint) : null);
}

function select(name, values, labels, value, empty) {
  return el("select", { name, value: valueOf(value) },
    empty != null ? el("option", { value: "" }, empty) : null,
    values.map((v) => el("option", { value: v }, labels[v] || v)));
}

function actionButton(label, iconName, run, { variant = "secondary", disabledReason = null } = {}) {
  const btn = el("button", {
    class: `button ${variant}`, type: "button", "aria-disabled": disabledReason ? "true" : null,
    "data-focus-key": `pa-${label}`,
  }, iconName ? icon(iconName) : null, label);
  btn.addEventListener("click", () => {
    if (disabledReason) { toast(disabledReason, "info"); return; }
    busy(btn, run);
  });
  return btn;
}

function head(r) {
  return el("header", { class: "drawer-head" },
    el("div", { class: "drawer-title" },
      el("p", { class: "eyebrow" }, `Rendición #${r.id}`),
      el("h2", { id: "panel-title", tabindex: "-1" }, tipoLabel(r.tipo_prestacion) || "Sin tipo de prestación"),
      el("div", { class: "drawer-meta" }, statusBadge(r.status),
        el("span", {}, fmtMoney(r.monto)), el("span", { class: "sep", "aria-hidden": "true" }, "·"),
        el("span", {}, fmtDate(r.fecha_atencion)))),
    el("button", { class: "button ghost icon-only", type: "button", "aria-label": "Cerrar panel", title: "Cerrar (Esc)", onclick: closePanel }, icon("x")));
}

function blockersBox(r) {
  const list = blockers(r);
  if (!list.length) {
    if (r.status === "borrador") return el("div", { class: "callout ok" }, icon("check"), el("span", {}, "Tiene todo lo necesario. Márcala como lista para que salga en la próxima pasada."));
    return null;
  }
  return el("div", { class: "callout warn", role: "group", "aria-label": "Qué falta" },
    el("p", { class: "callout-title" }, r.status === "borrador" ? "Antes de enviarla" : "Requiere atención"),
    el("ul", {}, list.map((b) => el("li", { class: b.level }, icon("alert"), b.text))));
}

function statusActions(r) {
  const out = [];
  const missing = missingFields(r);
  if (r.status === "borrador") {
    out.push(actionButton("Marcar lista para enviar", "check", () => setStatus([r.id], "lista"),
      { variant: "primary", disabledReason: missing.length ? `Falta ${missing.join(", ")}` : null }));
  } else if (r.status === "lista") {
    out.push(actionButton("Volver a borrador", "pencil", () => setStatus([r.id], "borrador")));
  } else if (r.status === "error") {
    if (r.status_before_error) {
      out.push(actionButton("Reintentar", "refresh", () => setStatus([r.id], r.status_before_error,
        { message: `#${r.id} se reintentará en la próxima pasada` }), { variant: "primary" }));
    }
    out.push(actionButton("Volver a borrador", "pencil", () => setStatus([r.id], "borrador")));
  } else if (r.status === "colmena_rechazada") {
    out.push(actionButton("Volver a borrador para corregir", "pencil", () => setStatus([r.id], "borrador"), { variant: "primary" }));
  }
  if (!out.length) return null;
  const note = r.status === "borrador" && missing.length ? `Falta ${missing.join(", ")}.`
    : r.status === "lista" ? "Sale en la próxima pasada de envío." : null;
  return el("div", { class: "drawer-actions" }, out, note ? el("span", { class: "muted small-print" }, note) : null);
}

function progressSection(r) {
  if (!(r.progress || []).length) return null;
  const strip = el("div", { class: "flow", role: "list", "aria-label": "Avance por cuenta" });
  renderFlow(strip, r.progress);
  return el("section", { class: "drawer-section" }, el("h3", {}, "Avance"), strip,
    r.error_message && r.status !== "error" ? el("p", { class: "error-msg" }, r.error_message) : null);
}

function fieldsSection(r) {
  const editable = isEditable(r);
  const section = el("section", { class: "drawer-section", "aria-labelledby": "fields-title" }, el("h3", { id: "fields-title" }, "Datos de la rendición"));
  if (!editable) {
    section.append(el("dl", { class: "facts" },
      el("dt", {}, "Tipo de prestación"), el("dd", {}, tipoLabel(r.tipo_prestacion) || "—"),
      el("dt", {}, "Fecha de atención"), el("dd", {}, fmtDate(r.fecha_atencion)),
      el("dt", {}, "Monto"), el("dd", {}, fmtMoney(r.monto))));
    return section;
  }
  if (r.fields_locked) {
    const btn = el("button", { class: "link-button", type: "button", "data-focus-key": "recompute" }, "Recalcular desde documentos");
    btn.addEventListener("click", () => busy(btn, () => recompute(r.id)));
    section.append(el("div", { class: "callout info compact" }, icon("lock"),
      el("span", {}, "Valores fijados a mano · ", btn)));
  }
  const key = `r:${r.id}`;
  const errors = panel.errors.get(key) || {};
  const original = { tipo_prestacion: valueOf(r.tipo_prestacion), fecha_atencion: valueOf(r.fecha_atencion), monto: valueOf(r.monto) };
  const save = el("button", { class: "button small", type: "submit", "data-focus-key": "r-save" }, "Guardar");
  const cancel = el("button", { class: "button ghost small", type: "button", onclick: () => discard(key) }, "Descartar");
  const bar = el("div", { class: "form-bar" }, el("span", { class: "muted small-print" }, "Al guardar, estos valores quedan fijados y ya no se recalculan."), cancel, save);
  const form = el("form", { class: "form-grid", novalidate: true },
    field("Tipo de prestación", select("tipo_prestacion", ENUMS.tipo, LABELS.tipo, r.tipo_prestacion, "—"), { error: errors.tipo_prestacion }),
    field("Fecha de atención", el("input", { name: "fecha_atencion", type: "date", value: original.fecha_atencion }), { error: errors.fecha_atencion }),
    field("Monto ($)", el("input", { name: "monto", type: "number", min: "0", step: "1", inputmode: "numeric", value: original.monto }), { error: errors.monto }),
    bar);
  for (const n of ["tipo_prestacion", "fecha_atencion", "monto"]) form.elements[n].dataset.focusKey = `r-${n}`;
  bindDraft(form, key, original, (dirty) => { bar.hidden = !dirty; });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const body = {};
    const errs = {};
    for (const name of Object.keys(original)) {
      const v = form.elements[name].value;
      if (v === original[name]) continue;
      if (v === "") errs[name] = "No puede quedar vacío";
      else body[name] = name === "monto" ? Number(v) : v;
    }
    if (Object.keys(errs).length) { panel.errors.set(key, errs); render(); return; }
    busy(save, async () => {
      try {
        panel.drafts.delete(key);
        panel.errors.delete(key);
        await patchRendicion(r.id, body);
      } catch (err) {
        panel.drafts.set(key, Object.fromEntries(Object.keys(body).map((k) => [k, form.elements[k].value])));
        failed(err);
        render();
      }
    });
  });
  section.append(form, el("p", { class: "muted small-print" },
    r.grouping_confidence < LOW_GROUPING ? [icon("alert", "warn"), " "] : null,
    `Agrupación: ${r.grouping_reason} · confianza ${pct(r.grouping_confidence)}`));
  return section;
}

const DOC_FIELDS = [
  ["doc_type", "Tipo de documento"], ["fecha", "Fecha"], ["monto", "Monto ($)"], ["provider_name", "Prestador"],
  ["provider_rut", "RUT del prestador"], ["folio", "Folio / N°"], ["patient_rut", "RUT del paciente"],
];

const RUT_FIELDS = new Set(["provider_rut", "patient_rut"]);

function fieldTag(d, name) {
  if ((d.edited_fields || []).includes(name)) return el("span", { class: "tag edited" }, "editado");
  if (d[name] != null && d[name] !== "") return el("span", { class: "tag" }, "detectado");
  return el("span", { class: "tag empty" }, "sin dato");
}

function preview(d) {
  const url = `/api/documents/${d.id}/file`;
  const open = el("a", { class: "button ghost small", href: url, target: "_blank", rel: "noopener" }, icon("external"), "Abrir");
  let body;
  if (/^image\/(png|jpe?g|webp|gif)$/.test(d.mime)) {
    body = el("img", { src: url, alt: `Vista previa de ${d.original_name}`, loading: "lazy" });
  } else if (d.mime === "application/pdf") {
    body = el("iframe", { src: `${url}#toolbar=0&navpanes=0&view=FitH`, title: `Vista previa de ${d.original_name}`, loading: "lazy" });
  } else {
    body = el("div", { class: "preview-empty" }, icon("file"), el("span", {}, "Vista previa no disponible aquí"),
      el("a", { href: url, target: "_blank", rel: "noopener" }, "Abrir el documento en otra pestaña"));
  }
  return el("div", { class: "doc-preview" }, body, el("div", { class: "preview-bar" }, open));
}

function docForm(r, d) {
  const key = `d:${d.id}`;
  const editable = isEditable(r) && d.role === "soporte";
  const errors = panel.errors.get(key) || {};
  if (!editable) {
    return el("dl", { class: "facts" }, DOC_FIELDS.flatMap(([name, label]) => [
      el("dt", {}, label), el("dd", {}, name === "doc_type" ? docLabel(d.doc_type) : name === "monto" ? fmtMoney(d.monto)
        : name === "fecha" ? fmtDate(d.fecha) : RUT_FIELDS.has(name) ? fmtRut(d[name]) || "—" : d[name] || "—")]));
  }
  const original = Object.fromEntries(DOC_FIELDS.map(([name]) => [name, valueOf(RUT_FIELDS.has(name) ? fmtRut(d[name]) : d[name])]));
  const input = (name) => {
    if (name === "doc_type") return select(name, ENUMS.doc, LABELS.doc, d.doc_type);
    if (name === "fecha") return el("input", { name, type: "date", value: original[name] });
    if (name === "monto") return el("input", { name, type: "text", inputmode: "numeric", autocomplete: "off", value: original[name] });
    return el("input", { name, type: "text", value: original[name], autocomplete: "off", spellcheck: "false",
      placeholder: RUT_FIELDS.has(name) ? "12.345.678-9" : null });
  };
  const save = el("button", { class: "button small", type: "submit" }, "Guardar cambios");
  const bar = el("div", { class: "form-bar" }, errors._ ? el("span", { class: "field-error" }, errors._) : null,
    el("button", { class: "button ghost small", type: "button", onclick: () => discard(key) }, "Descartar"), save);
  const form = el("form", { class: "form-grid", novalidate: true },
    DOC_FIELDS.map(([name, label]) => {
      const node = input(name);
      node.dataset.focusKey = `d${d.id}-${name}`;
      return field(label, node, { tag: fieldTag(d, name), error: errors[name] });
    }), bar);
  bindDraft(form, key, original, (dirty) => { bar.hidden = !dirty && !errors._; });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const monto = form.elements.monto.value.trim();
    if (monto !== "" && !/^\d+$/.test(monto)) {
      panel.errors.set(key, { monto: "Ingresa solo números, sin puntos ni signo $" });
      render();
      return;
    }
    const body = {};
    for (const [name] of DOC_FIELDS) {
      const v = form.elements[name].value.trim();
      if (v === original[name]) continue;
      body[name] = v === "" ? null : name === "monto" ? Number(v) : v;
    }
    if (!Object.keys(body).length) { discard(key); return; }
    busy(save, async () => {
      const draft = panel.drafts.get(key);
      try {
        panel.drafts.delete(key);
        panel.errors.delete(key);
        await patchDocument(d, body);
      } catch (err) {
        if (draft) panel.drafts.set(key, draft);
        panel.errors.set(key, fieldErrors(err));
        render();
      }
    });
  });
  return form;
}

/** Map a 422 body to {field: message}; anything else goes to "_". */
function fieldErrors(err) {
  const out = {};
  if (Array.isArray(err.detail)) {
    for (const d of err.detail) {
      const name = Array.isArray(d.loc) ? d.loc[d.loc.length - 1] : null;
      const msg = String(d.msg || "Valor inválido").replace(/^Value error,\s*/i, "");
      if (name && DOC_FIELDS.some(([n]) => n === name)) out[name] = msg;
      else out._ = msg;
    }
  } else {
    out._ = err.message;
  }
  return out;
}

function moveTargets(r, d) {
  const others = state.items.filter((o) => o.id !== r.id && isEditable(o)).sort((a, b) => b.id - a.id);
  return [
    { label: "Nueva rendición", icon: "plus", run: () => moveDocs([d.id], null) },
    ...(others.length ? [{ heading: "Rendiciones editables" }] : []),
    ...others.slice(0, 12).map((o) => ({
      label: `#${o.id} · ${tipoLabel(o.tipo_prestacion) || "Sin tipo"}`, hint: fmtDate(o.fecha_atencion),
      run: () => moveDocs([d.id], o.id),
    })),
  ];
}

function docItem(r, d) {
  const open = panel.expanded.has(d.id);
  const movable = isEditable(r) && d.role === "soporte";
  const edited = (d.edited_fields || []).length > 0;
  const doubtful = (d.confidence ?? 1) < LOW_DOC && !edited && d.role === "soporte";
  const bodyId = `doc-body-${d.id}`;
  const meta = [docLabel(d.doc_type), d.fecha ? fmtDate(d.fecha) : null, d.monto != null ? fmtMoney(d.monto) : null, d.provider_name].filter(Boolean).join(" · ");
  const toggle = el("button", {
    class: "doc-toggle", type: "button", "aria-expanded": String(open), "aria-controls": bodyId, "data-focus-key": `doc-${d.id}`,
    onclick: () => { if (open) panel.expanded.delete(d.id); else panel.expanded.add(d.id); render(); },
  }, icon(open ? "chevronDown" : "chevronRight", "chev"),
  el("span", { class: "doc-text" },
    el("span", { class: "doc-name" }, d.original_name),
    el("span", { class: "doc-meta" }, meta)),
  LABELS.role[d.role] ? el("span", { class: "tag" }, LABELS.role[d.role]) : null,
  doubtful ? el("span", { class: "tag warn" }, icon("alert"), `Lectura dudosa · ${pct(d.confidence)}`) : null,
  edited ? el("span", { class: "tag edited" }, "editado") : null,
  panel.drafts.has(`d:${d.id}`) ? el("span", { class: "tag draft" }, "sin guardar") : null);
  const tools = el("div", { class: "doc-tools" },
    movable ? el("button", {
      class: "button ghost small icon-only", type: "button", "aria-label": `Mover ${d.original_name} a…`, title: "Mover a…",
      "data-focus-key": `move-${d.id}`, onclick: (e) => openMenu(e.currentTarget, moveTargets(r, d), { label: "Mover a" }),
    }, icon("move")) : null,
    el("button", {
      class: "button ghost small icon-only", type: "button", "aria-label": `Más acciones para ${d.original_name}`,
      "data-focus-key": `more-${d.id}`,
      onclick: (e) => openMenu(e.currentTarget, [
        { label: "Abrir en otra pestaña", icon: "external", run: () => window.open(`/api/documents/${d.id}/file`, "_blank", "noopener") },
        { label: "Eliminar documento", icon: "trash", danger: true, disabled: !movable, reason: "Solo en borradores o listas",
          run: () => deleteDocument(d) },
      ], { label: "Acciones del documento" }),
    }, icon("more")));
  const li = el("li", { class: `doc-item${open ? " open" : ""}${doubtful ? " doubtful" : ""}`, draggable: movable ? "true" : null },
    el("div", { class: "doc-row" }, movable ? el("span", { class: "grip", "aria-hidden": "true", title: "Arrastra a otra rendición de la lista" }, icon("grip")) : null, toggle, tools),
    open ? el("div", { class: "doc-body", id: bodyId }, preview(d), docForm(r, d)) : null);
  if (movable) {
    li.addEventListener("dragstart", (e) => {
      if (e.target !== li) return;
      startDocDrag(e, [d.id], d.original_name);
      li.classList.add("dragged");
    });
    li.addEventListener("dragend", () => { li.classList.remove("dragged"); endDocDrag(); });
  }
  return li;
}

function docsSection(r) {
  const support = r.documents.filter((d) => d.role === "soporte");
  const receipts = r.documents.filter((d) => d.role !== "soporte");
  return el("section", { class: "drawer-section", "aria-labelledby": "docs-title" },
    el("div", { class: "section-head" },
      el("h3", { id: "docs-title" }, `Documentos (${support.length})`),
      isEditable(r) && support.length ? el("span", { class: "muted small-print hide-narrow" }, "Arrastra uno a otra fila para moverlo") : null),
    support.length ? el("ul", { class: "doc-list" }, support.map((d) => docItem(r, d)))
      : el("p", { class: "muted" }, "Esta rendición no tiene documentos de respaldo."),
    receipts.length ? [el("h4", { class: "subhead" }, "Comprobantes de los portales"), el("ul", { class: "doc-list" }, receipts.map((d) => docItem(r, d)))] : null);
}

const FIELD_NAMES = Object.fromEntries(DOC_FIELDS.map(([k, v]) => [k, v.replace(" ($)", "").toLowerCase()]));

function eventText(ev) {
  const change = ev.to_status ? ` → ${statusLabel(ev.to_status)}` : "";
  let extra = eventExtra(ev);
  const ids = ev.detail.document_ids || (ev.detail.document_id != null ? [ev.detail.document_id] : null);
  if (ev.kind === "documento_editado" && ev.detail.fields) extra = `: ${ev.detail.fields.map((f) => FIELD_NAMES[f] || f).join(", ")}`;
  else if (ids && ids.length > 1 && ev.kind !== "documento_editado") extra += ` (${plural(ids.length, "documento", "documentos")})`;
  return `${LABELS.event[ev.kind] || ev.kind}${extra}${change}`;
}

function historySection(r) {
  const details = el("details", { class: "history drawer-section" }, el("summary", {}, "Historial"), el("ol"));
  details.addEventListener("toggle", async () => {
    if (!details.open) return;
    const ol = details.querySelector("ol");
    ol.replaceChildren(el("li", { class: "muted" }, "Cargando…"));
    try {
      const events = await api("GET", `/api/rendiciones/${r.id}/events`);
      ol.replaceChildren(...events.map((ev) => el("li", {}, el("time", { datetime: ev.at }, fmtDateTime(ev.at)), " ", eventText(ev))));
    } catch (err) {
      ol.replaceChildren(el("li", { class: "error-msg" }, "No se pudo cargar: " + err.message));
    }
  });
  return details;
}

function dangerZone(r) {
  if (!isEditable(r)) return null;
  return el("div", { class: "drawer-foot" },
    actionButton("Eliminar rendición", "trash", async () => { if (await deleteRendiciones([r])) closePanel(); }, { variant: "ghost danger-text" }));
}

function loadingBody() {
  return el("div", { class: "drawer-body" }, el("p", { class: "muted" }, "Cargando…"));
}

export function render() {
  const id = state.query.r;
  const drawer = $("#panel");
  if (id == null) { hide(); return; }
  const r = state.byId.get(id);
  if (!r && state.loaded) {
    toast(`La rendición #${id} ya no existe`, "info");
    hide();
    setQuery({ r: null });
    return;
  }
  const opening = drawer.hidden || panel.shownId !== id;
  const scroller = drawer.querySelector(".drawer-body");
  const scroll = !opening && scroller ? scroller.scrollTop : 0;
  const hadFocus = drawer.contains(document.activeElement);
  drawer.hidden = false;
  document.body.classList.add("panel-open");
  setInert(NARROW.matches);
  panel.shownId = id;
  keepFocus(() => {
    if (!r) { drawer.replaceChildren(loadingBody()); return; }
    drawer.replaceChildren(head(r), el("div", { class: "drawer-body" },
      blockersBox(r), statusActions(r), progressSection(r), fieldsSection(r), docsSection(r), historySection(r), dangerZone(r)));
  });
  const body = drawer.querySelector(".drawer-body");
  if (body) body.scrollTop = scroll;
  // The focused control may be gone (e.g. "Recalcular" after recomputing): stay in the panel.
  if (hadFocus && !drawer.contains(document.activeElement)) $("#panel-title")?.focus({ preventScroll: true });
  if (opening && r) {
    const row = document.querySelector(`[data-focus-key="row-${id}"]`);
    row?.scrollIntoView({ block: "nearest" });
    if (NARROW.matches || !drawer.contains(document.activeElement)) $("#panel-title")?.focus({ preventScroll: true });
  }
}

/** Mirror the sticky top bar's height in --topbar-h so a side-by-side drawer starts below it. */
function trackTopbar() {
  const bar = $(".topbar");
  const sync = () => {
    const sticky = getComputedStyle(bar).position === "sticky";
    document.documentElement.style.setProperty("--topbar-h", sticky ? `${bar.offsetHeight}px` : "0px");
  };
  new ResizeObserver(sync).observe(bar);
  window.addEventListener("resize", sync);
}

export function initPanel() {
  trackTopbar();
  subscribe(render);
  NARROW.addEventListener("change", () => { if (!$("#panel").hidden) setInert(NARROW.matches); });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || e.defaultPrevented || $("#panel").hidden) return;
    if (document.querySelector("dialog[open]")) return;
    e.preventDefault();
    closePanel();
  });
  // Unsaved edits survive re-renders, but warn before leaving the page with them.
  window.addEventListener("beforeunload", (e) => { if (panel.drafts.size) e.preventDefault(); });
}

export const hasDrafts = () => panel.drafts.size > 0;
