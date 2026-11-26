// User feedback: toasts, announcements, confirm dialog, busy buttons and menus.

import { $, el, icon } from "./core.js";

/**
 * Show a toast. ``opts`` may be a kind string ("info" | "ok" | "error") for the
 * legacy signature, or ``{ kind, ms, action: { label, run } }``.
 */
export function toast(message, opts = "info", legacyMs) {
  const { kind = "info", ms, action } = typeof opts === "string" ? { kind: opts, ms: legacyMs } : opts;
  const life = ms || (action ? 9000 : kind === "error" ? 8000 : 4500);
  const node = el("div", { class: `toast ${kind}`, role: kind === "error" ? "alert" : "status" },
    el("span", { class: "toast-text" }, message));
  let timer = null;
  const close = () => { clearTimeout(timer); node.remove(); };
  const arm = () => { clearTimeout(timer); timer = setTimeout(close, life); };
  if (action) {
    const undo = action.label === "Deshacer";
    const btn = el("button", { class: "toast-action", type: "button" }, icon(undo ? "undo" : "arrowRight"), action.label);
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      close();
      try {
        await action.run();
      } catch (err) {
        toast((undo ? "No se pudo deshacer: " : "No se pudo completar: ") + err.message, "error");
      }
    });
    node.append(btn);
  }
  node.append(el("button", { class: "toast-close", type: "button", "aria-label": "Cerrar aviso", onclick: close }, icon("x")));
  // Pause while the user is reading or about to click "Deshacer".
  node.addEventListener("mouseenter", () => clearTimeout(timer));
  node.addEventListener("mouseleave", arm);
  node.addEventListener("focusin", () => clearTimeout(timer));
  node.addEventListener("focusout", arm);
  toastHost().append(node);
  arm();
  return { close };
}

/** A modal <dialog> makes the rest of the page inert, so toasts live inside it while open. */
function toastHost() {
  let box = $("#toasts");
  if (!box) box = el("div", { id: "toasts", class: "toasts", "aria-live": "polite" });
  const host = document.querySelector("dialog[open]") || document.body;
  if (box.parentElement !== host) host.append(box);
  return box;
}

// Non-bubbling "close" still reaches capture listeners: bring the toasts back to <body>.
document.addEventListener("close", () => {
  const box = $("#toasts");
  if (box && box.parentElement !== document.body) document.body.append(box);
}, true);

/** Polite screen-reader announcement (e.g. "Documento movido a #12"). */
export function announce(text) {
  const region = $("#announcer");
  region.textContent = "";
  requestAnimationFrame(() => { region.textContent = text; });
}

/** Run ``fn`` with ``button`` disabled and marked busy; ignores re-entry. */
export async function busy(button, fn) {
  if (button && button.getAttribute("aria-busy") === "true") return undefined;
  if (button) {
    button.setAttribute("aria-busy", "true");
    button.disabled = true;
  }
  try {
    return await fn();
  } finally {
    if (button && button.isConnected) {
      button.removeAttribute("aria-busy");
      button.disabled = false;
    }
  }
}

/**
 * Accessible replacement for ``confirm()``. Resolves true when confirmed.
 * ``body`` may be a string or DOM nodes.
 */
export function confirmDialog({ title, body, confirmLabel = "Confirmar", cancelLabel = "Cancelar", danger = false }) {
  const dialog = $("#confirm-dialog");
  const returnFocus = document.activeElement;
  const ok = el("button", { class: `button ${danger ? "danger-solid" : ""}`.trim(), type: "button", value: "ok" }, confirmLabel);
  const cancel = el("button", { class: "button secondary", type: "button", value: "cancel" }, cancelLabel);
  let inner = dialog.querySelector(".dialog-inner");
  if (!inner) dialog.prepend((inner = el("div", { class: "dialog-inner" })));
  inner.replaceChildren(
    el("h2", { id: "confirm-title", class: "dialog-title" }, title),
    el("div", { class: "dialog-body", id: "confirm-body" }, body),
    el("div", { class: "dialog-actions" }, cancel, ok));
  dialog.setAttribute("aria-labelledby", "confirm-title");
  dialog.setAttribute("aria-describedby", "confirm-body");
  return new Promise((resolve) => {
    const listeners = new AbortController();
    const finish = (value) => {
      listeners.abort();
      dialog.close();
      resolve(value);
      if (returnFocus && returnFocus.isConnected) returnFocus.focus();
    };
    ok.addEventListener("click", () => finish(true));
    cancel.addEventListener("click", () => finish(false));
    dialog.addEventListener("cancel", (e) => { e.preventDefault(); finish(false); }, { signal: listeners.signal });
    dialog.showModal();
    (danger ? cancel : ok).focus();
  });
}

let openMenuState = null;

export function closeMenu({ restoreFocus = true } = {}) {
  if (!openMenuState) return;
  const { node, anchor, onDoc } = openMenuState;
  openMenuState = null;
  node.remove();
  document.removeEventListener("pointerdown", onDoc, true);
  anchor.setAttribute("aria-expanded", "false");
  if (restoreFocus && anchor.isConnected) anchor.focus();
}

/**
 * Open a popover menu under ``anchor``. Items: ``{ label, hint, run, disabled, reason }``
 * or ``{ heading }`` / ``{ separator: true }``.
 */
export function openMenu(anchor, items, { label = "Opciones" } = {}) {
  if (openMenuState && openMenuState.anchor === anchor) { closeMenu(); return; }
  closeMenu({ restoreFocus: false });
  const node = el("div", { class: "menu", role: "menu", "aria-label": label });
  const buttons = [];
  for (const item of items) {
    if (item.separator) { node.append(el("div", { class: "menu-sep", role: "separator" })); continue; }
    if (item.heading) { node.append(el("div", { class: "menu-heading", role: "presentation" }, item.heading)); continue; }
    const btn = el("button", {
      class: `menu-item${item.danger ? " danger" : ""}`, type: "button", role: "menuitem", tabindex: "-1",
      "aria-disabled": item.disabled ? "true" : null, title: item.disabled ? item.reason : null,
    }, item.icon ? icon(item.icon) : null, el("span", { class: "menu-label" }, item.label),
    item.hint ? el("span", { class: "menu-hint" }, item.hint) : null);
    btn.addEventListener("click", () => {
      if (item.disabled) return;
      closeMenu();
      item.run();
    });
    buttons.push(btn);
    node.append(btn);
  }
  if (!buttons.length) node.append(el("div", { class: "menu-heading" }, "No hay opciones"));
  node.addEventListener("keydown", (e) => {
    const i = buttons.indexOf(document.activeElement);
    if (e.key === "ArrowDown") { e.preventDefault(); buttons[(i + 1) % buttons.length]?.focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); buttons[(i - 1 + buttons.length) % buttons.length]?.focus(); }
    else if (e.key === "Home") { e.preventDefault(); buttons[0]?.focus(); }
    else if (e.key === "End") { e.preventDefault(); buttons[buttons.length - 1]?.focus(); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeMenu(); }
    else if (e.key === "Tab") closeMenu({ restoreFocus: false });
  });
  // Inside a modal <dialog> everything else is inert, so the menu must live there.
  (anchor.closest("dialog") || document.body).append(node);
  position(node, anchor);
  const onDoc = (e) => { if (!node.contains(e.target) && !anchor.contains(e.target)) closeMenu({ restoreFocus: false }); };
  document.addEventListener("pointerdown", onDoc, true);
  anchor.setAttribute("aria-haspopup", "menu");
  anchor.setAttribute("aria-expanded", "true");
  openMenuState = { node, anchor, onDoc };
  (buttons.find((b) => !b.getAttribute("aria-disabled")) || buttons[0])?.focus();
}

function position(node, anchor) {
  const r = anchor.getBoundingClientRect();
  const w = node.offsetWidth;
  const h = node.offsetHeight;
  const left = Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8));
  const below = r.bottom + 4 + h <= window.innerHeight - 8;
  node.style.left = `${left}px`;
  node.style.top = `${below ? r.bottom + 4 : Math.max(8, r.top - h - 4)}px`;
}

window.addEventListener("resize", () => closeMenu({ restoreFocus: false }));
// Follow the anchor when something scrolls (focusing it can scroll its container).
document.addEventListener("scroll", (e) => {
  if (!openMenuState || openMenuState.node.contains(e.target)) return;
  const r = openMenuState.anchor.getBoundingClientRect();
  if (r.bottom < 0 || r.top > window.innerHeight) closeMenu({ restoreFocus: false });
  else position(openMenuState.node, openMenuState.anchor);
}, true);
