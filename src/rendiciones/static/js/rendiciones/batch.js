// Batch review: regroup an upload's documents (move, split, merge) and confirm.

import { $, docLabel, el, fmtDate, fmtMoney, icon, keepFocus, pct, plural, tipoLabel } from "../core.js";
import { openMenu } from "../feedback.js";
import { moveDocs } from "./actions.js";
import { drag, endDocDrag, startDocDrag } from "./dnd.js";
import { statusBadge } from "./list.js";
import { isEditable, LOW_GROUPING, setQuery, state, subscribe } from "./store.js";

const review = { docs: new Set(), selected: new Set(), unsubscribe: null, returnFocus: null };

export function openBatchReview({ docIds }) {
  const dialog = $("#batch-dialog");
  review.docs = new Set(docIds);
  review.selected.clear();
  review.returnFocus = document.activeElement;
  if (!review.unsubscribe) review.unsubscribe = subscribe(() => { if (dialog.open) render(); });
  render();
  if (!dialog.open) dialog.showModal();
  dialog.querySelector(".batch-groups")?.focus();
}

function close() {
  const dialog = $("#batch-dialog");
  if (dialog.open) dialog.close();
  review.unsubscribe?.();
  review.unsubscribe = null;
  const back = review.returnFocus;
  if (back && back.isConnected) back.focus();
}

function groups() {
  const ids = new Set();
  for (const docId of review.docs) {
    const rid = state.docOwner.get(docId);
    if (rid != null) ids.add(rid);
  }
  return [...ids].sort((a, b) => a - b).map((id) => state.byId.get(id)).filter(Boolean);
}

const movable = (r, d) => isEditable(r) && d.role === "soporte";

async function move(docIds, target, message) {
  for (const id of docIds) review.docs.add(id); // keep them on screen wherever they land
  review.selected.clear();
  await moveDocs(docIds, target, { message });
}

function targetItems(docIds) {
  const owners = new Set(docIds.map((id) => state.docOwner.get(id)));
  const inBatch = groups().filter((g) => !(owners.size === 1 && owners.has(g.id)));
  const ids = new Set(groups().map((g) => g.id));
  const others = state.items.filter((r) => isEditable(r) && !ids.has(r.id)).sort((a, b) => b.id - a.id).slice(0, 8);
  const item = (r) => ({
    label: `#${r.id} · ${tipoLabel(r.tipo_prestacion) || "Sin tipo"}`,
    hint: plural(r.documents.length, "doc", "docs"), run: () => move(docIds, r.id),
  });
  return [
    { label: "Nueva rendición", icon: "plus", run: () => move(docIds, null) },
    ...(inBatch.length ? [{ heading: "En este lote" }, ...inBatch.map(item)] : []),
    ...(others.length ? [{ heading: "Otras rendiciones" }, ...others.map(item)] : []),
  ];
}

function dropZone(node, target) {
  const accepts = () => drag.docIds && drag.docIds.some((id) => state.docOwner.get(id) !== target);
  node.addEventListener("dragover", (e) => {
    if (!accepts()) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    node.classList.add("drop-over");
  });
  node.addEventListener("dragleave", (e) => { if (!node.contains(e.relatedTarget)) node.classList.remove("drop-over"); });
  node.addEventListener("drop", (e) => {
    if (!accepts()) return;
    e.preventDefault();
    const ids = drag.docIds;
    endDocDrag();
    move(ids, target);
  });
}

function chip(r, d, extended) {
  const canMove = movable(r, d);
  const selected = review.selected.has(d.id);
  const meta = [docLabel(d.doc_type), d.monto != null ? fmtMoney(d.monto) : null, d.fecha ? fmtDate(d.fecha) : null].filter(Boolean).join(" · ");
  const li = el("li", { class: `doc-chip${selected ? " selected" : ""}${canMove ? "" : " fixed"}`, draggable: canMove ? "true" : null },
    canMove ? el("input", {
      type: "checkbox", checked: selected, "aria-label": `Seleccionar ${d.original_name}`, "data-focus-key": `bchk-${d.id}`,
      onchange: (e) => { if (e.target.checked) review.selected.add(d.id); else review.selected.delete(d.id); render(); },
    }) : el("span", { class: "chip-spacer" }),
    el("span", { class: "chip-body" },
      el("span", { class: "chip-name", title: d.original_name }, d.original_name),
      el("span", { class: "chip-meta" }, meta)),
    extended && review.docs.has(d.id) ? el("span", { class: "tag new" }, "nuevo") : null,
    canMove ? el("button", {
      class: "button ghost small icon-only", type: "button", "aria-label": `Mover ${d.original_name} a…`, "data-focus-key": `chip-${d.id}`,
      onclick: (e) => openMenu(e.currentTarget, targetItems([d.id]), { label: "Mover a" }),
    }, icon("move")) : null);
  if (canMove) {
    li.addEventListener("dragstart", (e) => {
      const ids = review.selected.has(d.id) ? [...review.selected] : [d.id];
      startDocDrag(e, ids, d.original_name);
      li.classList.add("dragged");
    });
    li.addEventListener("dragend", () => { li.classList.remove("dragged"); endDocDrag(); });
  }
  return li;
}

function groupCard(r, all) {
  const docs = r.documents;
  const extended = docs.some((d) => !review.docs.has(d.id));
  const low = r.grouping_confidence < LOW_GROUPING;
  const others = all.filter((g) => g.id !== r.id && isEditable(g));
  const mergeBtn = isEditable(r) && others.length ? el("button", {
    class: "button ghost small", type: "button", "data-focus-key": `merge-${r.id}`,
    onclick: (e) => openMenu(e.currentTarget, others.map((g) => ({
      label: `#${g.id} · ${tipoLabel(g.tipo_prestacion) || "Sin tipo"}`, hint: plural(g.documents.length, "doc", "docs"),
      run: () => move(docs.filter((d) => movable(r, d)).map((d) => d.id), g.id, `#${r.id} unida con #${g.id}`),
    })), { label: "Unir con" }),
  }, icon("merge"), "Unir con…") : null;
  const section = el("section", { class: `group${low ? " low" : ""}`, "aria-labelledby": `g-title-${r.id}` },
    el("header", { class: "group-head" },
      el("div", {},
        el("h3", { id: `g-title-${r.id}` }, `#${r.id} · ${tipoLabel(r.tipo_prestacion) || "Sin tipo de prestación"}`),
        el("p", { class: "group-meta" },
          el("span", { class: "gm-sum" }, [r.monto != null ? fmtMoney(r.monto) : "Sin monto", r.fecha_atencion ? fmtDate(r.fecha_atencion) : null].filter(Boolean).join(" · ")),
          el("span", {}, plural(docs.length, "documento", "documentos")),
          extended ? el("span", { class: "tag" }, "ya existía") : null)),
      r.status === "borrador" ? null : statusBadge(r.status),
      mergeBtn),
    low
      ? el("p", { class: "group-note warn" }, icon("alert"), el("span", {}, el("strong", {}, "Revisa este grupo. "), `${r.grouping_reason} · confianza ${pct(r.grouping_confidence)}`))
      : el("p", { class: "group-note" }, `${r.grouping_reason} · confianza ${pct(r.grouping_confidence)}`),
    el("ul", { class: "doc-chips", "aria-label": `Documentos de #${r.id}` }, docs.map((d) => chip(r, d, extended))));
  if (isEditable(r)) dropZone(section, r.id);
  return section;
}

function selectionBar() {
  const n = review.selected.size;
  if (!n) {
    return el("p", { class: "batch-hint muted" }, "Arrastra documentos entre grupos, o selecciónalos para moverlos. Los cambios se guardan al instante.");
  }
  const ids = [...review.selected];
  return el("div", { class: "batch-actions", role: "toolbar", "aria-label": "Acciones para la selección" },
    el("strong", {}, plural(n, "documento seleccionado", "documentos seleccionados")),
    el("button", {
      class: "button secondary small", type: "button", "data-focus-key": "bsel-move",
      onclick: (e) => openMenu(e.currentTarget, targetItems(ids), { label: "Mover a" }),
    }, icon("move"), "Mover a…"),
    el("button", { class: "button small", type: "button", "data-focus-key": "bsel-new", onclick: () => move(ids, null) }, icon("plus"), "Nueva rendición con la selección"),
    el("button", { class: "button ghost small", type: "button", onclick: () => { review.selected.clear(); render(); } }, "Quitar selección"));
}

function render() {
  const dialog = $("#batch-dialog");
  const all = groups();
  for (const id of [...review.selected]) if (!state.docOwner.has(id)) review.selected.delete(id);
  const low = all.filter((g) => g.grouping_confidence < LOW_GROUPING).length;
  const newZone = el("div", { class: "group new-group", "aria-hidden": "true" }, icon("plus"), "Suelta aquí para crear una rendición nueva");
  dropZone(newZone, null);
  let inner = dialog.querySelector(".batch-inner");
  if (!inner) dialog.prepend((inner = el("div", { class: "batch-inner" })));
  keepFocus(() => {
    inner.replaceChildren(
      el("header", { class: "batch-head" },
        el("div", {},
          el("h2", { id: "batch-title" }, "Revisa cómo agrupamos tus documentos"),
          el("p", { class: "muted" }, all.length
            ? `${plural(review.docs.size, "documento", "documentos")} en ${plural(all.length, "rendición", "rendiciones")}` +
              (low ? ` · ${plural(low, "grupo para revisar", "grupos para revisar")}` : "")
            : "Estos documentos ya no están en ninguna rendición editable.")),
        el("button", { class: "button ghost small icon-only", type: "button", "aria-label": "Cerrar revisión", onclick: close }, icon("x"))),
      el("div", { class: "batch-bar" }, selectionBar()),
      el("div", { class: "batch-groups", tabindex: "-1", "aria-label": "Grupos propuestos" }, all.map((g) => groupCard(g, all)), newZone),
      el("footer", { class: "batch-foot" },
        el("button", { class: "button ghost", type: "button", onclick: close }, "Cerrar"),
        el("button", { class: "button", type: "button", "data-focus-key": "batch-confirm", onclick: () => confirm(all) }, icon("check"), "Confirmar agrupación")));
  });
}

function confirm(all) {
  close();
  if (all.length) setQuery({ ids: all.map((g) => g.id), view: "todas", r: null }, { push: true });
}

export function initBatch() {
  const dialog = $("#batch-dialog");
  dialog.setAttribute("aria-labelledby", "batch-title");
  dialog.addEventListener("cancel", (e) => { e.preventDefault(); close(); });
}
