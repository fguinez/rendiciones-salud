// Rendiciones list: rows, sortable header, totals, empty states and the bulk action bar.

import { $, el, fmtDate, fmtMoney, icon, keepFocus, plural, shared, statusLabel, tipoLabel } from "../core.js";
import { toast } from "../feedback.js";
import { deleteRendiciones, mergeInto, moveDocs, readiness, setStatus } from "./actions.js";
import { drag } from "./dnd.js";
import { clearAll, filterChips, renderActiveFilters, renderViewTabs, syncToolbar } from "./filters.js";
import {
  blockers, currentView, DEFAULT_SORT, hasFacets, isEditable, providerOf, refresh, setQuery, state, subscribe,
  supportDocs, viewByKey, visibleItems,
} from "./store.js";
import { chooseFiles } from "./upload.js";

const STATUS_ICON = {
  borrador: "pencil", lista: "check", colmena_enviada: "clock", chubb_enviada: "clock",
  colmena_aprobada: "check", chubb_aprobada: "check", colmena_rechazada: "x", chubb_rechazada: "x", error: "alert",
};

export function statusBadge(status) {
  return el("span", { class: `badge status-${status}` }, icon(STATUS_ICON[status] || "circle"), statusLabel(status));
}

const SORTABLE = { id: "desc", fecha: "desc", monto: "desc", estado: "asc" };

function sortButton(key, label) {
  const [cur, dir] = (state.query.orden || DEFAULT_SORT).split("-");
  const active = cur === key;
  return el("button", {
    class: `sort${active ? " active" : ""}`, type: "button", "data-focus-key": `sort-${key}`,
    "aria-label": `Ordenar por ${label.toLowerCase()}${active ? (dir === "asc" ? ", ascendente" : ", descendente") : ""}`,
    onclick: () => {
      const nextDir = active ? (dir === "asc" ? "desc" : "asc") : SORTABLE[key];
      const orden = `${key}-${nextDir}`;
      setQuery({ orden: orden === DEFAULT_SORT ? "" : orden });
    },
  }, label, active ? el("span", { class: "sort-dir", "aria-hidden": "true" }, dir === "asc" ? "↑" : "↓") : null);
}

function header(shown) {
  const all = shown.length > 0 && shown.every((r) => state.selected.has(r.id));
  const some = shown.some((r) => state.selected.has(r.id));
  const check = el("input", {
    type: "checkbox", "aria-label": "Seleccionar todas las visibles", "data-focus-key": "check-all", checked: all,
    onchange: (e) => {
      for (const r of shown) {
        if (e.target.checked) state.selected.add(r.id); else state.selected.delete(r.id);
      }
      renderList();
    },
  });
  check.indeterminate = some && !all;
  return el("div", { class: "list-head" },
    el("span", { class: "cell-check" }, check),
    el("div", { class: "row-grid head-grid" },
      el("span", { class: "c-id" }, sortButton("id", "N°")),
      el("span", { class: "c-main" }, "Prestación"),
      el("span", { class: "c-fecha" }, sortButton("fecha", "Fecha")),
      el("span", { class: "c-monto" }, sortButton("monto", "Monto")),
      el("span", { class: "c-docs" }, "Docs"),
      el("span", { class: "c-status" }, sortButton("estado", "Estado"))));
}

function row(r) {
  const issues = blockers(r);
  const attention = issues.length > 0 && r.status !== "lista";
  const open = state.query.r === r.id;
  const selected = state.selected.has(r.id);
  const docs = supportDocs(r).length;
  const prov = providerOf(r);
  const sub = [];
  // The badge already says "Error": the line gives the reason instead, when there is one.
  if (r.status === "error") {
    if (r.error_message) sub.push(el("span", { class: "issue bad reason", title: r.error_message }, icon("alert"), el("span", {}, r.error_message)));
  } else if (attention) {
    sub.push(el("span", { class: `issue ${issues[0].level}` }, icon("alert"), issues[0].text.replace(/:.*$/, ""),
      issues.length > 1 ? ` +${issues.length - 1}` : ""));
  }
  if (prov) sub.push(el("span", { class: "prov" }, prov));

  const check = el("input", {
    type: "checkbox", "aria-label": `Seleccionar rendición #${r.id}`, "data-focus-key": `check-${r.id}`, checked: selected,
    onchange: (e) => {
      if (e.target.checked) state.selected.add(r.id); else state.selected.delete(r.id);
      renderList();
    },
  });
  const main = el("button", {
    class: "row-main row-grid", type: "button", "data-focus-key": `row-${r.id}`, "aria-current": open ? "true" : null,
    onclick: () => setQuery({ r: r.id }, { push: state.query.r == null }),
  },
  el("span", { class: "c-id" }, `#${r.id}`),
  el("span", { class: "c-main" },
    el("span", { class: `c-title${r.tipo_prestacion ? "" : " empty"}` }, tipoLabel(r.tipo_prestacion) || "Sin tipo de prestación"),
    sub.length ? el("span", { class: "c-sub" }, ...sub.flatMap((s, i) => (i ? [el("span", { class: "sep", "aria-hidden": "true" }, "·"), s] : [s]))) : null),
  el("span", { class: `c-fecha${r.fecha_atencion ? "" : " empty"}` }, fmtDate(r.fecha_atencion)),
  el("span", { class: `c-monto${r.monto == null ? " empty" : ""}` }, fmtMoney(r.monto)),
  el("span", { class: "c-docs", title: plural(docs, "documento", "documentos") }, icon("file"), String(docs),
    el("span", { class: "sr-only" }, docs === 1 ? " documento" : " documentos")),
  el("span", { class: "c-status" }, statusBadge(r.status)));

  const li = el("li", {
    class: `row${attention ? " attention" : ""}${selected ? " selected" : ""}${open ? " current" : ""}`,
    "data-id": r.id,
  }, el("span", { class: "cell-check" }, check), main);
  dropTarget(li, r);
  return li;
}

/** Rows accept documents dragged from the side panel. */
function dropTarget(li, r) {
  const accepts = () => drag.docIds && isEditable(r) && drag.docIds.some((id) => state.docOwner.get(id) !== r.id);
  li.addEventListener("dragover", (e) => {
    if (!accepts()) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    li.classList.add("drop-over");
  });
  li.addEventListener("dragleave", (e) => { if (!li.contains(e.relatedTarget)) li.classList.remove("drop-over"); });
  li.addEventListener("drop", (e) => {
    if (!accepts()) return;
    e.preventDefault();
    e.stopPropagation();
    li.classList.remove("drop-over");
    const ids = drag.docIds;
    drag.docIds = null;
    moveDocs(ids, r.id);
  });
}

function skeleton() {
  return el("ul", { class: "rows skeleton", "aria-busy": "true", "aria-label": "Cargando rendiciones" },
    [1, 2, 3, 4].map(() => el("li", { class: "row" }, el("span", { class: "bar w1" }), el("span", { class: "bar w2" }), el("span", { class: "bar w3" }))));
}

function firstRun() {
  return el("div", { class: "empty-state first-run" },
    el("div", { class: "empty-icon" }, icon("upload")),
    el("h3", {}, "Sube tus primeras boletas"),
    el("p", { class: "muted" }, "Boletas, órdenes médicas, recetas o presupuestos en PDF o foto. Los agrupamos solos en rendiciones y te mostramos qué revisar."),
    el("div", { class: "empty-actions" },
      el("button", { class: "button", type: "button", onclick: () => chooseFiles("files") }, icon("upload"), "Elegir archivos"),
      el("button", { class: "button secondary", type: "button", onclick: () => chooseFiles("folder") }, icon("folder"), "Elegir carpeta")),
    el("p", { class: "muted small-print" }, "También puedes arrastrarlos a cualquier parte de esta página."));
}

function noResults() {
  const view = viewByKey(currentView());
  const chips = filterChips();
  if (!hasFacets()) {
    const msg = currentView() === "atencion" ? "Todo en orden: nada requiere tu atención." : `No hay rendiciones en «${view.label}».`;
    return el("div", { class: "empty-state" },
      el("div", { class: "empty-icon ok" }, icon("check")),
      el("h3", {}, msg),
      el("button", { class: "button secondary small", type: "button", onclick: () => setQuery({ view: "todas" }, { push: true }) }, "Ver todas"));
  }
  return el("div", { class: "empty-state" },
    el("div", { class: "empty-icon" }, icon("search")),
    el("h3", {}, "Nada coincide con estos filtros"),
    el("div", { class: "empty-actions" },
      chips.slice(0, 3).map((c) => el("button", { class: "button secondary small", type: "button", onclick: () => setQuery(c.patch) },
        icon("x"), `Quitar ${c.label}`)),
      el("button", { class: "button ghost small", type: "button", onclick: clearAll }, "Limpiar filtros")),
    currentView() !== "todas" ? el("p", { class: "muted small-print" }, `Solo se buscó en «${view.label}». `,
      el("button", { class: "link-button", type: "button", onclick: () => setQuery({ view: "todas" }, { push: true }) }, "Buscar en todas")) : null);
}

function loadFailed() {
  return el("div", { class: "empty-state" },
    el("div", { class: "empty-icon bad" }, icon("alert")),
    el("h3", {}, "No se pudieron cargar las rendiciones"),
    el("p", { class: "muted" }, state.loadError),
    el("button", { class: "button secondary small", type: "button", onclick: () => refresh() }, icon("refresh"), "Reintentar"));
}

function renderTotals(shown) {
  const box = $("#totals");
  if (!shown.length) { box.textContent = ""; return; }
  const sum = (f) => shown.reduce((acc, r) => acc + (f(r) || 0), 0);
  const parts = [plural(shown.length, "rendición", "rendiciones"), `${fmtMoney(sum((r) => r.monto))} rendido`];
  for (const a of shared.workflow.accounts) {
    const nodes = shown.map((r) => (r.progress || []).find((n) => n.account === a.key)).filter((n) => n && n.monto != null);
    const who = a.label.replace(/^Abono /, "");
    if (nodes.length) parts.push(`${nodes[0].monto_label} ${who} ${fmtMoney(nodes.reduce((acc, n) => acc + n.monto, 0))}`);
  }
  box.textContent = parts.join(" · ");
}

function bulkButton(label, iconName, ids, reason, run, variant = "secondary") {
  const disabled = !ids.length;
  return el("button", {
    class: `button small ${variant}`, type: "button", "aria-disabled": disabled ? "true" : null,
    title: disabled ? reason : null, "data-focus-key": `bulk-${iconName}`,
    onclick: async (e) => {
      if (disabled) { toast(reason, "info"); return; }
      const btn = e.currentTarget;
      btn.setAttribute("aria-busy", "true");
      try { await run(); } finally { btn.removeAttribute("aria-busy"); }
    },
  }, icon(iconName), label);
}

function renderBulkBar(shown) {
  const bar = $("#bulk-bar");
  const picked = shown.filter((r) => state.selected.has(r.id));
  if (!picked.length) { bar.hidden = true; bar.replaceChildren(); return; }
  const { ok: ready, skipped } = readiness(picked);
  const back = picked.filter((r) => ["lista", "colmena_rechazada", "error"].includes(r.status)).map((r) => r.id);
  const editable = picked.filter(isEditable);
  const canMerge = picked.length >= 2 && editable.length === picked.length;
  const total = picked.reduce((acc, r) => acc + (r.monto || 0), 0);
  bar.hidden = false;
  bar.replaceChildren(
    el("span", { class: "bulk-count" }, el("strong", {}, plural(picked.length, "seleccionada", "seleccionadas")), ` · ${fmtMoney(total)}`),
    el("div", { class: "bulk-actions" },
      bulkButton(ready.length && ready.length < picked.length ? `Marcar listas (${ready.length})` : "Marcar listas", "check", ready,
        "Solo se marcan borradores con tipo, fecha y monto", async () => {
          await setStatus(ready, "lista");
          if (skipped.length) toast(`Quedaron en borrador: ${skipped.join(", ")}`, "info");
        }, "primary"),
      bulkButton("Volver a borrador", "pencil", back, "Solo para listas, rechazadas por Colmena o con error", () => setStatus(back, "borrador")),
      bulkButton("Unir en una rendición", "merge", canMerge ? picked.map((r) => r.id) : [],
        picked.length < 2 ? "Selecciona al menos dos rendiciones" : "Solo se pueden unir borradores o listas", () => {
          const [target, ...rest] = [...picked].sort((a, b) => a.id - b.id).map((r) => r.id);
          state.selected.clear();
          return mergeInto(target, rest);
        }),
      bulkButton("Eliminar", "trash", editable.map((r) => r.id), "Solo se pueden eliminar borradores o listas",
        async () => { if (await deleteRendiciones(editable)) state.selected.clear(); }, "danger")),
    el("button", {
      class: "button ghost small icon-only", type: "button", "aria-label": "Quitar selección",
      onclick: () => { state.selected.clear(); renderList(); },
    }, icon("x")));
}

export function renderList() {
  keepFocus(() => {
    syncToolbar();
    renderViewTabs();
    renderActiveFilters();
    const container = $("#list");
    if (!state.loaded) {
      container.replaceChildren(state.loadError ? loadFailed() : skeleton());
      renderTotals([]);
      renderBulkBar([]);
      return;
    }
    const shown = visibleItems();
    const visible = new Set(shown.map((r) => r.id));
    for (const id of [...state.selected]) if (!visible.has(id)) state.selected.delete(id);
    renderTotals(shown);
    if (!state.items.length) container.replaceChildren(firstRun());
    else if (!shown.length) container.replaceChildren(noResults());
    else container.replaceChildren(header(shown), el("ul", { class: "rows", "aria-label": "Rendiciones" }, shown.map(row)));
    renderBulkBar(shown);
  });
}

export function initList() {
  // Arrow keys move between rows; Enter/Space open (native button behaviour).
  $("#list").addEventListener("keydown", (e) => {
    if (!e.target.classList.contains("row-main") || !["ArrowDown", "ArrowUp"].includes(e.key)) return;
    const rows = [...document.querySelectorAll("#list .row-main")];
    const next = rows[rows.indexOf(e.target) + (e.key === "ArrowDown" ? 1 : -1)];
    if (next) { e.preventDefault(); next.focus(); }
  });
  subscribe(renderList);
}
