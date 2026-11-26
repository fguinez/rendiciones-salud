// "Envío automático" bar: saved route, queue per account and the pass started from the UI.

import { $, accountLabel, api, el, fmtDateTime, fmtMoney, plural, shared } from "./core.js";
import { confirmDialog, toast } from "./feedback.js";
import { refresh, state, subscribe } from "./rendiciones/store.js";

const REPORT_LABELS = [
  ["colmena_submitted", "enviada(s) a Colmena"], ["colmena_approved", "aprobada(s) por Colmena"],
  ["colmena_rejected", "rechazada(s) por Colmena"], ["chubb_submitted", "enviada(s) a Chubb"],
  ["chubb_approved", "aprobada(s) por Chubb"], ["chubb_rejected", "rechazada(s) por Chubb"],
  ["chubb_liquidaciones", "liquidación(es) de Chubb adjuntada(s)"],
  ["pending", "aún en revisión"], ["dry_run", "probada(s) sin enviar"],
  ["errors", "con error"], ["skipped", "omitida(s)"],
  ["abonos_confirmed", "abono(s) confirmado(s) en el banco"],
  ["abonos_waiting", "esperando abono"], ["abonos_missing", "abono(s) que no llegaron"],
];
let pipelineTimer = null;
let pipelineWasRunning = false;

export async function loadWorkflow() {
  try {
    shared.workflow = await api("GET", "/api/workflow");
  } catch (err) {
    toast("No se pudo leer el flujo: " + err.message, "error");
  }
  renderRoute();
}

function routeNodes(stages) {
  const parts = [];
  stages.forEach((stage, i) => {
    if (i) parts.push(el("span", { class: "arrow", "aria-hidden": "true" }, "→"));
    stage.forEach((key, j) => {
      if (j) parts.push(el("span", { class: "plus" }, "+"));
      parts.push(el("span", { class: "chip-account" }, accountLabel(key)));
    });
  });
  return parts;
}

export function renderRoute() {
  const stages = shared.workflow.stages;
  $("#route-subtitle").textContent = stages.map((s) => s.map(accountLabel).join(" + ")).join(" → ");
  $("#pipeline-route").replaceChildren(...routeNodes(stages));
  renderQueue();
}

function queueCounts() {
  const counts = {};
  for (const key of shared.workflow.stages.flat()) counts[key] = { por_enviar: 0, en_revision: 0 };
  for (const r of state.items) {
    for (const n of r.progress || []) {
      if (!counts[n.account]) counts[n.account] = { por_enviar: 0, en_revision: 0 };
      if (n.state in counts[n.account]) counts[n.account][n.state] += 1;
    }
  }
  return counts;
}

function renderQueue() {
  const counts = queueCounts();
  const flow = shared.workflow.stages.flat();
  const items = Object.entries(counts)
    .filter(([key, c]) => flow.includes(key) || c.por_enviar || c.en_revision)
    .map(([key, c]) => el("div", { class: "queue-item" },
      el("strong", {}, accountLabel(key)),
      ...(key.startsWith("abono_")
        ? [el("span", { class: c.en_revision ? "" : "muted" }, `${c.en_revision} esperando abono`)]
        : [el("span", { class: c.por_enviar ? "queue-hot" : "muted" }, `${c.por_enviar} por enviar`),
          el("span", { class: "muted" }, `${c.en_revision} en revisión`)])));
  $("#pipeline-queue").replaceChildren(...items);
}

subscribe(renderQueue);

function describeReport(report) {
  const parts = REPORT_LABELS
    .filter(([key]) => report[key] && report[key].length)
    .map(([key, label]) => `${report[key].length} ${label}`);
  return parts.length ? parts.join(" · ") : "no había nada que enviar ni consultar";
}

function renderPipeline(data) {
  const job = data.job;
  const running = job && job.state === "running";
  const busyElsewhere = !running && data.busy_by;
  const status = $("#pipeline-status");
  const warning = $("#pipeline-warning");
  status.className = "pipeline-status";
  warning.replaceChildren();
  for (const id of ["#run-dry", "#run-submit"]) $(id).disabled = Boolean(running || busyElsewhere);

  if (running) {
    const what = job.mode === "dry_run" ? "Probando sin enviar" : "Enviando y consultando estados";
    status.classList.add("running");
    status.textContent = `${what}… (desde ${fmtDateTime(job.started_at)})`;
  } else if (busyElsewhere) {
    status.classList.add("running");
    status.textContent = `El navegador está en uso: ${data.busy_by}.`;
  } else if (job) {
    const when = fmtDateTime(job.finished_at);
    const what = job.mode === "dry_run" ? "Última prueba" : "Último envío";
    if (job.state === "failed") {
      status.classList.add("failed");
      status.textContent = `${what} (${when}): ${job.message}`;
    } else {
      status.textContent = `${what} (${when}): ${describeReport(job.report || {})}.`;
    }
    const bankErrors = (job.report && job.report.bank_errors) || [];
    if (bankErrors.length) {
      warning.append(`No se pudo verificar en el banco: ${bankErrors.join("; ")}. `,
        el("a", { href: "#cuentas" }, "Revisa la cuenta del Banco de Chile"), ". ");
    }
    const expired = (job.report && job.report.sessions_expired) || [];
    if (expired.length) {
      warning.append(`La sesión de ${expired.map(accountLabel).join(" y ")} expiró y no se pudo volver a iniciar. `,
        el("a", { href: "#cuentas" }, "Inicia sesión desde Cuentas"),
        " y luego usa «Reintentar» en las rendiciones con error.");
    }
  } else {
    status.textContent = "";
  }
  return running || busyElsewhere;
}

export async function loadPipeline() {
  clearTimeout(pipelineTimer);
  let active = false;
  try {
    const data = await api("GET", "/api/pipeline");
    active = renderPipeline(data);
    const running = Boolean(data.job && data.job.state === "running");
    if (pipelineWasRunning && !running) {
      refresh();
      if (data.job) toast(data.job.state === "failed" ? "La pasada falló" : "Pasada terminada", data.job.state === "failed" ? "error" : "ok");
    }
    pipelineWasRunning = running;
  } catch (err) {
    $("#pipeline-status").textContent = "No se pudo leer el estado del envío: " + err.message;
  }
  if (active && !$("#view-rendiciones").hidden) pipelineTimer = setTimeout(loadPipeline, 2000);
}

/** What a real pass would send: rendiciones with an account waiting to receive them. */
function submitSummary() {
  const counts = queueCounts();
  const pending = state.items.filter((r) => (r.progress || []).some((n) => n.state === "por_enviar"));
  const total = pending.reduce((acc, r) => acc + (r.monto || 0), 0);
  const lines = shared.workflow.stages.flat()
    .map((key) => el("li", {}, el("strong", {}, accountLabel(key)), `: ${counts[key]?.por_enviar || 0} por enviar`));
  return el("div", {},
    el("p", { class: "dialog-lead" },
      pending.length ? `${plural(pending.length, "rendición", "rendiciones")} por ${fmtMoney(total)}.` : "No hay rendiciones por enviar; solo se consultarán los estados."),
    el("ul", { class: "dialog-list" }, lines),
    el("p", { class: "muted" }, "Se envía en el orden del flujo: lo de la etapa siguiente sale cuando la anterior lo aprueba. También se consultan los estados."));
}

export async function startPipeline(mode, button) {
  if (mode === "submit") {
    const ok = await confirmDialog({
      title: "¿Enviar a los portales?", body: submitSummary(), confirmLabel: "Enviar ahora",
    });
    if (!ok) return;
  }
  button.disabled = true;
  try {
    await api("POST", "/api/pipeline/runs", { mode });
    pipelineWasRunning = true; // a fast pass may end before the first poll
  } catch (err) {
    toast("No se pudo iniciar: " + err.message, "error");
  }
  loadPipeline();
}
