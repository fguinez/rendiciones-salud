// "Cuentas" view: portal and bank credentials kept in the macOS keychain.

import { $, api, el, fmtDateTime, shared } from "./core.js";
import { toast } from "./feedback.js";

const LOGIN_RESULT = {
  ok: "Sesión iniciada", rejected: "Usuario o contraseña rechazados",
  needs_user: "El portal pidió un paso adicional", failed: "No se pudo iniciar sesión",
};
let accountsTimer = null;

function renderAccount(a) {
  const tpl = $("#account-template").content.cloneNode(true);
  const card = tpl.querySelector(".card");
  card.querySelector(".card-title").textContent = a.label;
  const uses = (key) => key === a.portal || (shared.workflow.accounts.find((s) => s.key === key) || {}).credential === a.portal;
  const stages = shared.workflow.stages.map((s, i) => (s.some(uses) ? i + 1 : 0)).filter(Boolean);
  card.querySelector(".card-sub").textContent = stages.length
    ? `${stages.length > 1 ? "Etapas" : "Etapa"} ${stages.join(" y ")} del flujo`
    : (a.portal === "bancochile" ? "Fuera del flujo: agrega «Abono Colmena» o «Abono Chubb» en Flujo" : "Fuera del flujo");
  const badge = card.querySelector(".badge");
  badge.textContent = a.configured ? `Guardada · ${a.username_hint}` : "Sin configurar";
  badge.classList.add(a.configured ? "ok" : "borrador");

  const status = card.querySelector(".account-status");
  const last = a.last_login;
  const parts = [];
  if (a.configured) parts.push(`Actualizada: ${fmtDateTime(a.updated_at)}`);
  if (last) parts.push(`Último inicio de sesión: ${LOGIN_RESULT[last.result] || last.result}, ${fmtDateTime(last.at)} (${last.detail})`);
  status.textContent = parts.join(" · ");

  const warning = card.querySelector(".account-warning");
  if (a.store_error) warning.textContent = a.store_error;
  else if (a.auto_login_paused) {
    warning.textContent = "Inicio de sesión automático en pausa para no bloquear la cuenta. " +
      "Guarda la contraseña de nuevo o usa «Iniciar sesión ahora».";
  }

  const form = card.querySelector(".account-form");
  card.querySelector(".user-label").textContent = a.username_help;
  form.username.placeholder = a.configured ? `Actual: ${a.username_hint}` : a.username_help;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = { username: form.username.value, password: form.password.value };
    form.password.value = "";
    try {
      await api("PUT", `/api/accounts/${a.portal}`, body);
      form.reset();
      toast(`Cuenta de ${a.label} guardada en el llavero`, "ok");
    } catch (err) {
      toast("No se pudo guardar: " + err.message, "error", 7000);
    }
    loadAccounts();
  });

  const job = a.login_job;
  const running = job && job.state === "running";
  const footer = card.querySelector(".card-actions");
  const bank = a.portal === "bancochile";
  const login = el("button", { class: "button small secondary", type: "button" },
    running ? (bank ? "Consultando el banco…" : "Esperando inicio de sesión…") : (bank ? "Probar conexión" : "Iniciar sesión ahora"));
  login.disabled = running;
  login.addEventListener("click", async () => {
    try { await api("POST", `/api/accounts/${a.portal}/login`); }
    catch (err) { toast("No se pudo abrir: " + err.message, "error", 7000); }
    loadAccounts();
  });
  footer.append(login);
  if (a.configured) {
    const del = el("button", { class: "button small danger", type: "button" }, "Borrar cuenta");
    del.addEventListener("click", async () => {
      if (!confirm(`¿Borrar la cuenta de ${a.label} del llavero?`)) return;
      try { await api("DELETE", `/api/accounts/${a.portal}`); toast("Cuenta borrada", "ok"); }
      catch (err) { toast("No se pudo borrar: " + err.message, "error", 7000); }
      loadAccounts();
    });
    footer.append(del);
  }
  if (job) card.querySelector(".job-msg").textContent = job.message;
  if (bank) card.querySelector(".card-actions").before(bankSettingsForm());
  return card;
}

let bankSettings = null;

function bankSettingsForm() {
  const box = el("form", { class: "fields bank-settings", autocomplete: "off" });
  const field = (label, name, help) => el("label", {}, label,
    el("input", { name, type: "text", maxlength: "120", spellcheck: "false", placeholder: help }));
  box.append(
    el("p", { class: "muted bank-settings-title" },
      "Qué buscar en la cartola: abonos cuya glosa contenga este texto (separa varias con «;»)."),
    field("Glosa del abono de Colmena", "colmena", "Pago:proveedores 0762966190"),
    field("Glosa del abono de Chubb", "chubb", "Pago:proveedores 0774088202"),
    el("label", {}, "Días de espera tras la aprobación",
      el("input", { name: "wait_days", type: "number", min: "1", max: "365", required: "" })),
    el("button", { class: "button small", type: "submit" }, "Guardar búsqueda"));
  const fill = (data) => {
    box.colmena.value = (data.glosas.colmena || []).join("; ");
    box.chubb.value = (data.glosas.chubb || []).join("; ");
    box.wait_days.value = data.wait_days;
  };
  if (bankSettings) fill(bankSettings);
  else api("GET", "/api/bank/settings").then((data) => { bankSettings = data; fill(data); })
    .catch((err) => toast("No se pudo leer la búsqueda del banco: " + err.message, "error"));
  box.addEventListener("submit", async (e) => {
    e.preventDefault();
    const split = (v) => v.split(";").map((x) => x.trim()).filter(Boolean);
    const body = { glosas: { colmena: split(box.colmena.value), chubb: split(box.chubb.value) },
      wait_days: Number(box.wait_days.value) };
    try {
      bankSettings = await api("PUT", "/api/bank/settings", body);
      fill(bankSettings);
      toast("Búsqueda del banco guardada", "ok");
    } catch (err) {
      toast("No se pudo guardar: " + err.message, "error", 7000);
    }
  });
  return box;
}

export async function loadAccounts() {
  clearTimeout(accountsTimer);
  const container = $("#accounts");
  try {
    const accounts = await api("GET", "/api/accounts");
    container.replaceChildren(...accounts.map(renderAccount));
    if (accounts.some((a) => a.login_job && a.login_job.state === "running") && !$("#view-cuentas").hidden) {
      accountsTimer = setTimeout(loadAccounts, 2000);
    }
  } catch (err) {
    container.replaceChildren(el("p", { class: "error-msg" }, "No se pudieron cargar las cuentas: " + err.message));
  }
}
