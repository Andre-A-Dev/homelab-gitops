// Galatea UI — plain JS, no build step, no external requests.
"use strict";

const $ = (id) => document.getElementById(id);

const STATE_TEXT = {
  IDLE: "Idle", READY: "Ready", PRINTING: "Printing", PAUSED: "Paused",
  FINISHED: "Finished", STOPPED: "Stopped", ATTENTION: "Needs attention",
  ERROR: "Error", BUSY: "Busy",
};
const ACTION_TEXT = {
  power_on: "Power on", power_off: "Power off",
  pause: "Pause", resume: "Resume", stop: "Stop",
};
const RESULT_TEXT = {
  done: "done", forced: "forced", blocked: "blocked", refused: "refused",
  error: "failed", unreachable: "printer unreachable",
};
const HOT_NOZZLE_C = 50;

async function api(path, options = {}) {
  const init = { headers: { "Content-Type": "application/json" }, ...options };
  if (init.body && typeof init.body !== "string") init.body = JSON.stringify(init.body);
  try {
    const res = await fetch(path, init);
    let body = null;
    try { body = await res.json(); } catch { /* empty body */ }
    return { status: res.status, body };
  } catch {
    return { status: 0, body: null };
  }
}

// --- Formatting --------------------------------------------------------------

const deg = (v) => (v == null ? "—" : `${Math.round(v)} °C`);

function duration(s) {
  if (s == null) return "";
  const h = Math.floor(s / 3600);
  const m = Math.round((s % 3600) / 60);
  return h > 0 ? `${h} h ${m} min` : `${m} min`;
}

function ago(s) {
  if (s == null) return "unknown";
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  return `${Math.round(s / 3600)} h ago`;
}

function clock(ts) {
  const d = new Date(ts * 1000);
  const today = new Date().toDateString() === d.toDateString();
  return today
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString([], { day: "2-digit", month: "2-digit" });
}

let toastTimer;
function toast(message, isError = false) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("error", isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 5000);
}

// --- Printer -----------------------------------------------------------------

async function refreshPrinter() {
  const { status, body } = await api("/api/printer/status");
  const fresh = $("freshness");

  if (status !== 200 || !body || !body.reachable) {
    $("state").textContent = "Not reachable";
    fresh.textContent = "Printer does not answer. Is it switched on?";
    fresh.classList.add("stale");
    ["btn-pause", "btn-resume", "btn-stop"].forEach((id) => { $(id).disabled = true; });
    $("job").hidden = true;
    ["nozzle", "bed"].forEach((id) => { $(id).textContent = "—"; });
    ["nozzle-target", "bed-target"].forEach((id) => { $(id).textContent = ""; });
    return;
  }

  const s = body.state;
  $("state").textContent = STATE_TEXT[s] || (s ? s.toLowerCase() : "Unknown");
  fresh.textContent = `Updated ${new Date(body.fetched_at * 1000).toLocaleTimeString()}`;
  fresh.classList.remove("stale");

  $("nozzle").textContent = deg(body.nozzle_c);
  $("nozzle").classList.toggle("hot", (body.nozzle_c ?? 0) > HOT_NOZZLE_C);
  $("nozzle-target").textContent = body.nozzle_target_c ? `of ${deg(body.nozzle_target_c)}` : "";
  $("bed").textContent = deg(body.bed_c);
  $("bed-target").textContent = body.bed_target_c ? `of ${deg(body.bed_target_c)}` : "";

  const hasJob = body.job_id != null && body.progress != null;
  $("job").hidden = !hasJob;
  if (hasJob) {
    const pct = Math.round(body.progress);
    $("job-fill").style.width = `${pct}%`;
    $("job-bar").setAttribute("aria-valuenow", String(pct));
    const left = body.time_remaining_s != null ? `, ${duration(body.time_remaining_s)} left` : "";
    $("job-label").textContent = `${pct} % printed${left}`;
  }

  $("btn-pause").disabled = s !== "PRINTING";
  $("btn-resume").disabled = s !== "PAUSED";
  $("btn-stop").disabled = !["PRINTING", "PAUSED", "ATTENTION"].includes(s);
}

async function printerCommand(action, body) {
  const { status, body: res } = await api(`/api/printer/${action}`, { method: "POST", body: body || {} });
  toast(res?.message || `Request failed (${status}).`, status !== 200);
  refreshAll();
}

// --- Power -------------------------------------------------------------------

async function refreshPower() {
  const panel = $("power");
  const btn = $("btn-power");
  const lock = $("interlock");
  const { status, body } = await api("/api/plug");

  if (status !== 200 || !body || !body.reachable) {
    panel.dataset.status = "unknown";
    $("power-state").textContent = "Unknown";
    $("power-draw").textContent = "Plug does not answer.";
    lock.hidden = true;
    btn.disabled = true;
    btn.textContent = "Turn off";
    $("btn-force").hidden = true;
    return;
  }

  btn.disabled = false;
  if (!body.on) {
    panel.dataset.status = "off";
    $("power-state").textContent = "Off";
    $("power-draw").textContent = "";
    lock.hidden = true;
    btn.textContent = "Turn on";
    btn.className = "";
    btn.dataset.action = "on";
    $("btn-force").hidden = true;
    return;
  }

  $("power-state").textContent = "On";
  $("power-draw").textContent = body.power_w != null ? `${Math.round(body.power_w)} W` : "";
  btn.textContent = "Turn off";
  btn.dataset.action = "off";

  const preview = body.power_off;
  lock.hidden = false;
  btn.className = preview && preview.allowed ? "" : "secondary";
  if (preview && preview.allowed) {
    panel.dataset.status = "safe";
    lock.textContent = "Safe to turn off: printer idle and cool.";
    $("btn-force").hidden = true;
  } else {
    panel.dataset.status = "blocked";
    lock.textContent = preview ? preview.message : "Cannot check whether turning off is safe.";
  }
}

async function powerOff(force) {
  const { status, body } = await api("/api/plug/off", { method: "POST", body: { force } });
  if (status === 409 && body?.blocked) {
    toast(body.message, true);
    $("btn-force").hidden = false;
  } else {
    toast(body?.message || `Request failed (${status}).`, status !== 200);
    if (status === 200) $("btn-force").hidden = true;
  }
  refreshAll();
}

// --- Enclosure ---------------------------------------------------------------

async function refreshClimate() {
  const { status, body } = await api("/api/climate");
  const note = $("climate-note");
  if (status !== 200 || !body || !body.reachable) {
    $("air").textContent = "—";
    $("humidity").textContent = "—";
    note.textContent = "Sensor gateway does not answer.";
    note.classList.add("warn");
    return;
  }
  $("air").textContent = body.temperature_c != null ? `${body.temperature_c.toFixed(1)} °C` : "—";
  $("humidity").textContent = body.humidity_pct != null ? `${Math.round(body.humidity_pct)} %` : "—";
  if (body.stale) {
    note.textContent = `No reading since ${ago(body.age_s)}. Sensor out of range or battery empty. Values may be outdated.`;
    note.classList.add("warn");
  } else {
    const battery = body.battery_pct != null ? `, battery ${body.battery_pct} %` : "";
    note.textContent = `Measured ${ago(body.age_s)}${battery}.`;
    note.classList.remove("warn");
  }
}

// --- Filament ----------------------------------------------------------------

function spoolRow(sp) {
  const tr = document.createElement("tr");

  const name = document.createElement("td");
  name.textContent = `${sp.material} ${sp.color}`;
  if (sp.is_active) {
    const tag = document.createElement("span");
    tag.className = "loaded";
    tag.textContent = "Loaded";
    name.append(tag);
  }
  if (sp.manufacturer) {
    const sub = document.createElement("span");
    sub.className = "sub";
    sub.textContent = sp.manufacturer;
    name.append(sub);
  }

  const left = document.createElement("td");
  left.className = "left";
  left.textContent = `${Math.round(sp.remaining_g)} g`;
  const of = document.createElement("span");
  of.className = "sub";
  of.textContent = `of ${Math.round(sp.total_weight_g)} g`;
  left.append(of);

  const cell = document.createElement("td");
  cell.className = "row-actions";
  const acts = document.createElement("div");
  acts.className = "row-buttons";
  cell.append(acts);
  const mk = (label, cls, fn) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = cls;
    b.textContent = label;
    b.addEventListener("click", fn);
    return b;
  };
  if (!sp.is_active) {
    acts.append(mk("Mark loaded", "secondary", () => patchSpool(sp.id, { is_active: true }, "Marked as loaded.")));
  }
  acts.append(mk("Update weight", "secondary", () => {
    const v = prompt(`Filament left on ${sp.material} ${sp.color}, in grams:`, Math.round(sp.remaining_g));
    if (v !== null && v.trim() !== "") patchSpool(sp.id, { remaining_g: Number(v) }, "Weight updated.");
  }));
  acts.append(mk("Delete", "danger", async () => {
    if (!confirm(`Delete ${sp.material} ${sp.color}?`)) return;
    const { status, body } = await api(`/api/filament/${sp.id}`, { method: "DELETE" });
    toast(status === 200 ? "Spool deleted." : body?.message || "Delete failed.", status !== 200);
    refreshFilament();
  }));

  tr.append(name, left, cell);
  return tr;
}

async function patchSpool(id, data, okText) {
  const { status, body } = await api(`/api/filament/${id}`, { method: "PATCH", body: data });
  toast(status === 200 ? okText : body?.message || "Update failed.", status !== 200);
  refreshFilament();
}

async function refreshFilament() {
  const { status, body } = await api("/api/filament");
  if (status !== 200 || !Array.isArray(body)) return;
  $("spools").replaceChildren(...body.map(spoolRow));
  $("spools-empty").hidden = body.length > 0;
}

// --- Activity ----------------------------------------------------------------

async function refreshActivity() {
  const { status, body } = await api("/api/actions");
  if (status !== 200 || !Array.isArray(body)) return;
  $("actions").replaceChildren(...body.slice(0, 20).map((a) => {
    const li = document.createElement("li");
    const t = document.createElement("time");
    t.dateTime = new Date(a.ts * 1000).toISOString();
    t.textContent = clock(a.ts);
    const txt = document.createElement("span");
    txt.className = a.result;
    const why = a.result === "blocked" && a.detail ? ` (${a.detail.replaceAll("_", " ")})` : "";
    txt.textContent = `${ACTION_TEXT[a.action] || a.action} ${RESULT_TEXT[a.result] || a.result}${why}`;
    li.append(t, txt);
    return li;
  }));
}

// --- Wiring ------------------------------------------------------------------

function refreshAll() {
  refreshPrinter();
  refreshPower();
  refreshClimate();
  refreshFilament();
  refreshActivity();
}

$("btn-pause").addEventListener("click", () => printerCommand("pause"));
$("btn-resume").addEventListener("click", () => printerCommand("resume"));
$("btn-stop").addEventListener("click", () => {
  if (confirm("Stop the print? This cannot be undone.")) printerCommand("stop", { confirm: true });
});

$("btn-power").addEventListener("click", async (e) => {
  if (e.currentTarget.dataset.action === "on") {
    const { status, body } = await api("/api/plug/on", { method: "POST" });
    toast(body?.message || `Request failed (${status}).`, status !== 200);
    refreshAll();
  } else {
    powerOff(false);
  }
});

$("btn-force").addEventListener("click", () => {
  const reason = $("interlock").textContent;
  if (confirm(`Cut power anyway?\n\n${reason}\n\nThis can ruin a running print or clog a hot nozzle.`)) {
    powerOff(true);
  }
});

$("spool-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.currentTarget;
  const data = Object.fromEntries(new FormData(form).entries());
  const { status, body } = await api("/api/filament", { method: "POST", body: data });
  if (status === 201) {
    toast("Spool added.");
    form.reset();
    $("add-spool").open = false;
    refreshFilament();
  } else {
    toast(body?.message || "Could not add spool.", true);
  }
});

// Poll only while the page is visible — no point hammering PrusaLink from a background tab.
let timers = [];
function startPolling() {
  stopPolling();
  refreshAll();
  timers = [
    setInterval(() => { refreshPrinter(); refreshPower(); }, 5000),
    setInterval(refreshClimate, 30000),
    setInterval(refreshActivity, 30000),
  ];
}
function stopPolling() { timers.forEach(clearInterval); timers = []; }

document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopPolling(); else startPolling();
});
startPolling();
