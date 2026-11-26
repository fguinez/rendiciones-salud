// Quick views (tabs with counts), search, facet filters, sort and applied-filter chips.

import { $, debounce, el, ENUMS, fmtDate, fmtMoney, icon, plural, tipoLabel } from "../core.js";
import {
  currentView, DEFAULT_SORT, emptyQuery, hasFacets, matchesFacets, providers, setQuery, SORTS, state, VIEWS,
} from "./store.js";

export function renderViewTabs() {
  const base = state.items.filter((r) => matchesFacets(r));
  const active = currentView();
  const tabs = VIEWS.map((v) => {
    const count = base.filter(v.test).length;
    const pressed = v.key === active;
    return el("button", {
      class: `view-tab${v.key === "atencion" && count ? " has-attention" : ""}`, type: "button",
      "aria-pressed": String(pressed), "data-focus-key": `view-${v.key}`,
      onclick: () => setQuery({ view: v.key }, { push: true }),
    }, el("span", { class: "tab-long" }, v.label), el("span", { class: "tab-short", "aria-hidden": "true" }, v.short),
    el("span", { class: "count", "aria-label": `(${count})` }, String(count)));
  });
  const box = $("#view-tabs");
  box.replaceChildren(...tabs);
  // Keep the selected tab visible when the row still overflows.
  const current = box.querySelector('[aria-pressed="true"]');
  if (current && box.scrollWidth > box.clientWidth) {
    const left = current.offsetLeft - box.offsetLeft;
    if (left < box.scrollLeft || left + current.offsetWidth > box.scrollLeft + box.clientWidth) {
      box.scrollLeft = left - (box.clientWidth - current.offsetWidth) / 2;
    }
  }
}

const pushSearch = debounce((value) => setQuery({ q: value.trim() }), 250);

const NARROW_SEARCH = window.matchMedia("(max-width: 520px)");

export function initToolbar() {
  const search = $("#search");
  const placeholder = () => { search.placeholder = NARROW_SEARCH.matches ? "Buscar…" : "Buscar por #, prestador, documento o monto"; };
  placeholder();
  NARROW_SEARCH.addEventListener("change", placeholder);
  search.addEventListener("input", () => pushSearch(search.value));
  search.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && search.value) { e.stopPropagation(); search.value = ""; setQuery({ q: "" }); }
  });
  const sort = $("#sort");
  sort.replaceChildren(...SORTS.map((s) => el("option", { value: s.key }, s.label)));
  sort.addEventListener("change", () => setQuery({ orden: sort.value === DEFAULT_SORT ? "" : sort.value }));
  initPopover();
}

export function syncToolbar() {
  const search = $("#search");
  if (document.activeElement !== search && search.value.trim() !== state.query.q) search.value = state.query.q;
  $("#sort").value = state.query.orden || DEFAULT_SORT;
  const n = facetCount();
  const toggle = $("#filters-toggle");
  toggle.querySelector(".count").textContent = n ? String(n) : "";
  toggle.classList.toggle("active", n > 0);
  syncPopover();
}

const facetCount = (q = state.query) =>
  (q.tipo.length ? 1 : 0) + (q.desde || q.hasta ? 1 : 0) + (q.min !== "" || q.max !== "" ? 1 : 0) + (q.prestador ? 1 : 0);

function initPopover() {
  const toggle = $("#filters-toggle");
  const pop = $("#filter-popover");
  const close = (focus = true) => {
    if (pop.hidden) return;
    pop.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
    if (focus) toggle.focus();
  };
  toggle.addEventListener("click", () => {
    if (!pop.hidden) { close(); return; }
    pop.hidden = false;
    toggle.setAttribute("aria-expanded", "true");
    syncPopover(true);
    pop.querySelector("input, select, button")?.focus();
  });
  pop.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } });
  document.addEventListener("pointerdown", (e) => {
    if (!pop.hidden && !pop.contains(e.target) && !toggle.contains(e.target)) close(false);
  });

  const tipos = el("fieldset", { class: "facet" }, el("legend", {}, "Tipo de prestación"),
    el("div", { class: "facet-checks", id: "facet-tipos" }));
  const range = (legend, a, b, type, labels) => el("fieldset", { class: "facet" }, el("legend", {}, legend),
    el("div", { class: "facet-range" },
      el("label", {}, labels[0], el("input", { type, name: a, min: type === "number" ? "0" : null, step: type === "number" ? "1" : null, inputmode: type === "number" ? "numeric" : null })),
      el("label", {}, labels[1], el("input", { type, name: b, min: type === "number" ? "0" : null, step: type === "number" ? "1" : null, inputmode: type === "number" ? "numeric" : null }))));
  const prov = el("fieldset", { class: "facet" }, el("legend", {}, "Prestador"),
    el("select", { name: "prestador", "aria-label": "Prestador" }));
  const form = el("form", { class: "facets", onsubmit: (e) => { e.preventDefault(); close(); } },
    tipos, range("Fecha de atención", "desde", "hasta", "date", ["Desde", "Hasta"]),
    range("Monto ($)", "min", "max", "number", ["Mínimo", "Máximo"]), prov,
    el("div", { class: "facet-actions" },
      el("button", { class: "button ghost small", type: "button", onclick: () => setQuery(clearedFacets()) }, "Limpiar"),
      el("button", { class: "button small", type: "submit" }, "Listo")));
  pop.replaceChildren(form);

  const pushNumbers = debounce(() => setQuery({ min: form.min.value, max: form.max.value }), 350);
  form.addEventListener("change", (e) => {
    const t = e.target;
    if (t.name === "tipo") {
      setQuery({ tipo: [...form.querySelectorAll("input[name=tipo]:checked")].map((c) => c.value) });
    } else if (t.name === "desde" || t.name === "hasta") {
      setQuery({ [t.name]: t.value });
    } else if (t.name === "prestador") {
      setQuery({ prestador: t.value });
    }
  });
  form.addEventListener("input", (e) => { if (e.target.name === "min" || e.target.name === "max") pushNumbers(); });
}

const clearedFacets = () => ({ tipo: [], desde: "", hasta: "", min: "", max: "", prestador: "" });

function syncPopover(force = false) {
  const pop = $("#filter-popover");
  if (pop.hidden && !force) return;
  const form = pop.querySelector("form");
  const q = state.query;
  const checks = $("#facet-tipos");
  const present = new Set(state.items.map((r) => r.tipo_prestacion || ""));
  const values = ENUMS.tipo.filter((t) => present.has(t) || q.tipo.includes(t));
  if (present.has("") || q.tipo.includes("")) values.push("");
  // Rebuild only when the options change, so focus stays on the checkbox just toggled.
  if (checks.dataset.values !== values.join(",")) {
    checks.dataset.values = values.join(",");
    checks.replaceChildren(...(values.length ? values.map((t) => el("label", { class: "check" },
      el("input", { type: "checkbox", name: "tipo", value: t }),
      t ? tipoLabel(t) : "Sin tipo")) : [el("span", { class: "muted" }, "Aún no hay tipos")]));
  }
  for (const c of checks.querySelectorAll("input")) c.checked = q.tipo.includes(c.value);
  for (const key of ["desde", "hasta", "min", "max"]) {
    if (document.activeElement !== form[key]) form[key].value = q[key];
  }
  const names = providers();
  if (form.prestador.dataset.values !== names.join("\n")) {
    form.prestador.dataset.values = names.join("\n");
    form.prestador.replaceChildren(el("option", { value: "" }, "Todos"), ...names.map((n) => el("option", { value: n }, n)));
  }
  form.prestador.value = q.prestador;
}

export function filterChips() {
  const q = state.query;
  const chips = [];
  const add = (label, patch) => chips.push({ label, patch });
  if (q.ids.length) add(`Lote: ${plural(q.ids.length, "rendición", "rendiciones")}`, { ids: [] });
  for (const t of q.tipo) add(t ? tipoLabel(t) : "Sin tipo", { tipo: q.tipo.filter((x) => x !== t) });
  if (q.desde || q.hasta) {
    const label = q.desde && q.hasta ? `${fmtDate(q.desde)} – ${fmtDate(q.hasta)}` : q.desde ? `Desde ${fmtDate(q.desde)}` : `Hasta ${fmtDate(q.hasta)}`;
    add(label, { desde: "", hasta: "" });
  }
  if (q.min !== "" || q.max !== "") {
    const label = q.min !== "" && q.max !== "" ? `${fmtMoney(q.min)} – ${fmtMoney(q.max)}` : q.min !== "" ? `Desde ${fmtMoney(q.min)}` : `Hasta ${fmtMoney(q.max)}`;
    add(label, { min: "", max: "" });
  }
  if (q.prestador) add(q.prestador, { prestador: "" });
  if (q.q) add(`“${q.q}”`, { q: "" });
  return chips;
}

export function renderActiveFilters() {
  const chips = filterChips();
  const box = $("#active-filters");
  if (!chips.length) { box.replaceChildren(); return; }
  box.replaceChildren(
    ...chips.map((c, i) => el("button", {
      class: "filter-chip", type: "button", "data-focus-key": `chip-${i}`,
      "aria-label": `Quitar filtro ${c.label}`, onclick: () => setQuery(c.patch),
    }, el("span", {}, c.label), icon("x"))),
    el("button", { class: "link-button", type: "button", onclick: clearAll }, "Limpiar filtros"));
}

export function clearAll() {
  const { view, orden, r } = state.query;
  setQuery({ ...emptyQuery(), view, orden, r });
}

export { hasFacets };
