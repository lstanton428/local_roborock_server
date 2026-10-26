"""Home Assistant ingress panel for the add-on.

Home Assistant builds an add-on's webui link from the hostname the browser used
to reach Home Assistant, which is never the stack hostname the certificate
covers. Ingress opens this panel instead. It shows the server's health, lets a
Home Assistant admin open or close new connections, lists and runs the vacuum's
routines and, unless turned off, creates and edits them. It links to the full
admin dashboard on the stack hostname. Everything else stays on that dashboard,
behind its own password.
"""

from __future__ import annotations

import html
import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

if TYPE_CHECKING:
    from .routine_panel import RoutinePanel

# Home Assistant's ingress proxy always connects from this address.
HA_INGRESS_PROXY = "172.30.32.2"

LOG = logging.getLogger(__name__)

# The panel is framed by Home Assistant and reached under its ingress path, so
# 'self' is Home Assistant's origin and every URL below is relative.
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src data:; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
    ),
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Roborock Local Server</title>
<link rel="stylesheet" href="panel.css">
<script src="panel.js" defer></script>
</head>
<body>
<main>
  <header>
    <h1>Roborock Local Server</h1>
    <span id="health" class="badge">Loading…</span>
  </header>

  <section class="card">
    <h2>New connections</h2>
    <p class="hint">Needed when a phone app, Home Assistant or a vacuum signs in or pairs for the first time.</p>
    <p id="nc-state" class="state">Loading…</p>
    <div id="nc-actions" class="actions" hidden>
      <button id="nc-open" type="button" class="primary">Open</button>
      <button id="nc-close" type="button">Close now</button>
    </div>
    <p id="nc-readonly" class="hint" hidden>Opening and closing from Home Assistant is turned off
      (<code>ingress_new_connections_controls</code>). Use the admin dashboard.</p>
    <p id="nc-error" class="error" hidden></p>
  </section>

  <section class="card">
    <h2>Vacuums</h2>
    <ul id="vacuums" class="list"><li class="muted">Loading…</li></ul>
  </section>

  <section id="rt-card" class="card" hidden>
    <div class="row">
      <h2>Routines</h2>
      <button id="rt-new" type="button" class="primary small" hidden>New routine</button>
    </div>
    <ul id="routines" class="list routines"><li class="muted">Loading…</li></ul>
    <p id="rt-readonly" class="hint" hidden>Creating and editing routines here is turned off
      (<code>ingress_routine_editor</code>). They can still be run.</p>
    <p id="rt-message" class="hint" hidden></p>
    <p id="rt-error" class="error" hidden></p>
  </section>

  <section id="editor" class="card" hidden>
    <h2 id="ed-title">New routine</h2>
    <label class="field">Name <input id="ed-name" type="text" maxlength="64" autocomplete="off"></label>
    <label id="ed-vacuum-row" class="field">Vacuum <select id="ed-vacuum"></select></label>
    <div id="ed-map-wrap" class="map-wrap" hidden><img id="ed-map" alt="Vacuum map"><canvas id="ed-canvas"></canvas></div>
    <p id="ed-map-hint" class="hint">Loading the map from the vacuum…</p>
    <ol id="ed-steps" class="steps"></ol>
    <div class="actions">
      <button id="ed-add-zone" type="button">Add zone step</button>
      <button id="ed-add-rooms" type="button">Add rooms step</button>
    </div>
    <div class="actions end">
      <button id="ed-cancel" type="button">Cancel</button>
      <button id="ed-save" type="button" class="primary">Save</button>
    </div>
    <p id="ed-error" class="error" hidden></p>
  </section>

  <section class="card">
    <h2>Server</h2>
    <ul class="list">
      <li><span>Certificate</span><span id="cert" class="muted">Loading…</span></li>
      <li id="services-row" hidden><span>Not running</span><span id="services" class="bad"></span></li>
    </ul>
  </section>

  <a id="admin" class="button" href="{admin_url}" target="_blank" rel="noopener noreferrer">Open admin dashboard</a>
  <p class="hint">Cloud import, onboarding and app sessions are in the admin dashboard.</p>
</main>
</body>
</html>
"""

_CSS = """
:root { color-scheme: light dark; --bg: #f5f5f5; --card: #ffffff; --text: #1f1f1f; --muted: #6b6b6b;
  --line: #e3e3e3; --accent: #03a9f4; --accent-text: #ffffff; --good: #2e7d32; --bad: #c62828; --warn: #b26a00; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #111111; --card: #1c1c1c; --text: #e1e1e1; --muted: #9b9b9b; --line: #2c2c2c;
    --good: #66bb6a; --bad: #ef5350; --warn: #ffa726; }
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
main { max-width: 640px; margin: 0 auto; padding: 16px; }
header { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin: 4px 0 16px; }
h1 { font-size: 20px; font-weight: 500; margin: 0; }
h2 { font-size: 15px; font-weight: 600; margin: 0 0 6px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 16px; margin-bottom: 12px; }
.hint, .muted { color: var(--muted); }
.hint { font-size: 13px; margin: 0 0 10px; }
.state { font-size: 17px; margin: 0 0 12px; }
.badge { font-size: 13px; padding: 3px 10px; border-radius: 999px; border: 1px solid var(--line); white-space: nowrap; }
.good { color: var(--good); }
.bad { color: var(--bad); }
.warn { color: var(--warn); }
.actions { display: flex; gap: 8px; flex-wrap: wrap; }
button, .button { font: inherit; font-weight: 500; border-radius: 8px; padding: 8px 16px; cursor: pointer;
  border: 1px solid var(--line); background: transparent; color: var(--text); text-decoration: none; }
button.primary, .button { background: var(--accent); border-color: var(--accent); color: var(--accent-text); }
button:disabled { opacity: 0.5; cursor: default; }
.button { display: block; text-align: center; margin: 4px 0 8px; }
.list { list-style: none; margin: 0; padding: 0; }
.list li { display: flex; justify-content: space-between; gap: 12px; padding: 6px 0; border-top: 1px solid var(--line); }
.list li:first-child { border-top: none; }
.error { color: var(--bad); margin: 10px 0 0; }
code { font-size: 12px; }
.row { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 6px; }
.row h2 { margin: 0; }
button.small { padding: 4px 10px; font-size: 13px; }
button.danger { color: var(--bad); border-color: var(--bad); }
.routines li { flex-wrap: wrap; align-items: center; }
.routine-name { font-weight: 500; }
.routine-meta { display: block; font-size: 13px; color: var(--muted); }
.routine-main { flex: 1 1 200px; min-width: 0; overflow-wrap: anywhere; }
.routine-buttons { display: flex; gap: 6px; flex-wrap: wrap; }
.routine-buttons input { width: 160px; }
.field { display: flex; flex-direction: column; gap: 4px; font-size: 13px; color: var(--muted); margin-bottom: 10px; }
input, select { font: inherit; color: var(--text); background: var(--bg); border: 1px solid var(--line);
  border-radius: 8px; padding: 6px 8px; }
.map-wrap { position: relative; margin: 4px 0 6px; border: 1px solid var(--line); border-radius: 8px; overflow: hidden;
  background: #ffffff; }
.map-wrap img { display: block; width: 100%; height: auto; }
.map-wrap canvas { position: absolute; inset: 0; width: 100%; height: 100%; cursor: crosshair; }
.steps { list-style: none; margin: 8px 0; padding: 0; }
.step { border: 1px solid var(--line); border-radius: 10px; padding: 10px; margin-bottom: 8px; }
.step.active { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.step-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 8px; }
.step-title { font-weight: 600; }
.settings { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 8px; }
.settings .field { margin-bottom: 0; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0; }
.chip { font-size: 13px; padding: 3px 10px; border-radius: 999px; border: 1px solid var(--line); background: transparent;
  color: var(--text); cursor: pointer; }
.chip.on { background: var(--accent); border-color: var(--accent); color: var(--accent-text); }
.actions.end { justify-content: flex-end; margin-top: 8px; }
"""

_JS = """
"use strict";
let status = null;
let fetchedAt = 0;

function text(id, value, className) {
  const element = document.getElementById(id);
  element.textContent = value;
  if (className !== undefined) element.className = className;
}

function remainingSeconds() {
  const seconds = status.new_connections.seconds_remaining;
  if (seconds === null || seconds === undefined) return null;
  return Math.max(0, seconds - Math.floor((Date.now() - fetchedAt) / 1000));
}

function windowLabel(minutes) {
  return minutes > 0 ? "Open for " + minutes + " min" : "Open";
}

function renderNewConnections() {
  const nc = status.new_connections;
  const remaining = remainingSeconds();
  let state;
  let className = "state";
  if (!nc.turned_on) {
    state = "Closed (turned off)";
    className += " bad";
  } else if (!nc.enabled || remaining === 0) {
    state = "Closed (the window ended)";
    className += " bad";
  } else if (remaining === null) {
    state = "Open until closed";
    className += " good";
  } else {
    const minutes = Math.floor(remaining / 60);
    const seconds = String(remaining % 60).padStart(2, "0");
    state = "Open, closes in " + minutes + ":" + seconds;
    className += " good";
  }
  text("nc-state", state, className);
  document.getElementById("nc-actions").hidden = !status.controls;
  document.getElementById("nc-readonly").hidden = status.controls;
  const open = nc.turned_on && nc.enabled && remaining !== 0;
  document.getElementById("nc-open").textContent = open && remaining !== null ? "Restart window" : windowLabel(nc.window_minutes);
  document.getElementById("nc-open").disabled = open && remaining === null;
  document.getElementById("nc-close").disabled = !open;
}

function render() {
  if (status.overall_ok) text("health", "Healthy", "badge good");
  else text("health", "Needs attention", "badge bad");
  renderNewConnections();

  const list = document.getElementById("vacuums");
  list.replaceChildren();
  if (!status.vacuums.length) {
    const item = document.createElement("li");
    item.className = "muted";
    item.textContent = "No vacuums yet";
    list.appendChild(item);
  }
  for (const vacuum of status.vacuums) {
    const item = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = vacuum.model ? vacuum.name + " (" + vacuum.model + ")" : vacuum.name;
    const state = document.createElement("span");
    state.textContent = vacuum.connected ? "Connected" : "Offline";
    state.className = vacuum.connected ? "good" : "muted";
    item.append(name, state);
    list.appendChild(item);
  }

  const certificate = status.certificate;
  if (certificate) {
    const expires = new Date(certificate.not_after);
    const days = Math.floor((expires - Date.now()) / 86400000);
    const className = days < 0 ? "bad" : days < 14 ? "warn" : "muted";
    text("cert", (days < 0 ? "Expired " : "Expires ") + expires.toLocaleDateString() + " (" + days + " days)", className);
  } else {
    text("cert", "Managed outside the add-on", "muted");
  }
  document.getElementById("services-row").hidden = !status.services_down.length;
  text("services", status.services_down.join(", "));
}

function showError(message) {
  const element = document.getElementById("nc-error");
  element.textContent = message;
  element.hidden = !message;
}

async function refresh() {
  try {
    const response = await fetch("api/status", { cache: "no-store" });
    if (!response.ok) throw new Error("Status request failed (" + response.status + ")");
    status = await response.json();
    fetchedAt = Date.now();
    render();
  } catch (error) {
    text("health", "Unreachable", "badge bad");
  }
}

async function setNewConnections(enabled) {
  showError("");
  for (const id of ["nc-open", "nc-close"]) document.getElementById(id).disabled = true;
  try {
    const response = await fetch("api/new-connections", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: enabled }),
    });
    if (!response.ok) throw new Error("The server refused the change (" + response.status + ")");
    status = await response.json();
    fetchedAt = Date.now();
    render();
  } catch (error) {
    showError(error.message);
    if (status) render();
  }
}

// Routines. The editor only exists when the server allows it (ingress_routine_editor).
let routines = null;
let editor = null;
let mapData = null;
let drag = null;
const MAX_ZONES = 5;
const MIN_ZONE_MM = 200;

function el(tag, props, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (key === "className") element.className = value;
    else if (key === "text") element.textContent = value;
    else if (key.startsWith("on")) element.addEventListener(key.slice(2), value);
    else if (value === true) element.setAttribute(key, "");
    else if (value !== false && value !== null && value !== undefined) element.setAttribute(key, value);
  }
  for (const child of children) element.append(child);
  return element;
}

function showText(id, message) {
  const element = document.getElementById(id);
  element.textContent = message || "";
  element.hidden = !message;
}

async function api(path, body) {
  const options = body === undefined ? { cache: "no-store" } : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  const response = await fetch(path, options);
  let payload = null;
  try {
    payload = await response.json();
  } catch (error) {
    payload = null;
  }
  if (!response.ok) {
    const error = new Error((payload && payload.error) || "Request failed (" + response.status + ")");
    error.status = response.status;
    throw error;
  }
  return payload;
}

async function loadRoutines() {
  try {
    routines = await api("api/routines");
    showText("rt-error", "");
    renderRoutines();
  } catch (error) {
    if (error.status === 404) return;
    showText("rt-error", error.message);
  }
  if (!editor) document.getElementById("rt-card").hidden = false;
}

function stepLabel(step) {
  if (step.name) return step.name;
  return step.kind === "zone" ? "Zone" : step.kind === "rooms" ? "Rooms" : "Step";
}

function renderRoutines() {
  const several = routines.vacuums.length > 1;
  document.getElementById("rt-new").hidden = !routines.editor || !routines.vacuums.length;
  document.getElementById("rt-readonly").hidden = routines.editor;
  const list = document.getElementById("routines");
  list.replaceChildren();
  if (!routines.routines.length) list.append(el("li", { className: "muted", text: "No routines yet" }));
  for (const routine of routines.routines) {
    const meta = routine.steps.map(stepLabel).join(" → ");
    const main = el("div", { className: "routine-main" },
      el("span", { className: "routine-name", text: routine.name || "Routine " + routine.id }),
      el("span", { className: "routine-meta", text: several ? routine.device_name + (meta ? " · " + meta : "") : meta }));
    const buttons = el("div", { className: "routine-buttons" },
      el("button", { type: "button", className: "small primary", text: "Run", onclick: () => runRoutine(routine) }));
    if (routines.editor) {
      if (routine.editable) {
        buttons.append(el("button", { type: "button", className: "small", text: "Edit", onclick: () => openEditor(routine) }));
      }
      buttons.append(
        el("button", { type: "button", className: "small", text: "Rename", onclick: () => startRename(routine, buttons) }),
        el("button", { type: "button", className: "small danger", text: "Delete",
          onclick: (event) => deleteRoutine(routine, event.currentTarget) }));
    }
    list.append(el("li", {}, main, buttons));
  }
}

function clearRoutineMessages() {
  showText("rt-error", "");
  showText("rt-message", "");
}

async function runRoutine(routine) {
  clearRoutineMessages();
  try {
    const result = await api("api/routines/" + routine.id + "/run", {});
    if (result.status === "cancel_requested") showText("rt-message", "Stopping " + routine.name + ".");
    else if (result.status === "routine_in_progress") {
      showText("rt-error", (result.activeSceneName || "Another routine") + " is still running.");
    } else showText("rt-message", routine.name + " started.");
  } catch (error) {
    showText("rt-error", error.message);
  }
}

function startRename(routine, buttons) {
  clearRoutineMessages();
  const input = el("input", { type: "text", maxlength: "64", value: routine.name, "aria-label": "Routine name" });
  const save = el("button", { type: "button", className: "small primary", text: "Save" });
  const cancel = el("button", { type: "button", className: "small", text: "Cancel", onclick: () => renderRoutines() });
  save.addEventListener("click", async () => {
    save.disabled = true;
    try {
      await api("api/routines/" + routine.id + "/rename", { name: input.value });
      await loadRoutines();
    } catch (error) {
      showText("rt-error", error.message);
      save.disabled = false;
    }
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") save.click();
    if (event.key === "Escape") renderRoutines();
  });
  buttons.replaceChildren(input, save, cancel);
  input.select();
}

async function deleteRoutine(routine, button) {
  clearRoutineMessages();
  if (button.dataset.confirm !== "yes") {
    button.dataset.confirm = "yes";
    button.textContent = "Confirm delete";
    setTimeout(() => {
      if (!button.isConnected) return;
      button.dataset.confirm = "";
      button.textContent = "Delete";
    }, 4000);
    return;
  }
  button.disabled = true;
  try {
    await api("api/routines/" + routine.id + "/delete", {});
    showText("rt-message", routine.name + " deleted.");
    await loadRoutines();
  } catch (error) {
    showText("rt-error", error.message);
    button.disabled = false;
  }
}

function newStep(kind) {
  return { kind: kind, name: "", rooms: [], zones: [], keep_tid: "", zone_count: 0,
    fan_power: 102, water_box_mode: 200, mop_mode: 300, repeat: 1 };
}

function keepsAppZones(step) {
  return step.kind === "zone" && Boolean(step.keep_tid) && !step.zones.length;
}

function openEditor(routine) {
  clearRoutineMessages();
  const vacuums = routines.vacuums;
  editor = {
    id: routine ? routine.id : null,
    deviceId: routine ? routine.device_id : vacuums[0].duid,
    steps: routine ? routine.steps.map((step) => ({
      ...newStep(step.kind),
      ...step,
      rooms: (step.rooms || []).slice(),
      zones: (step.zones || []).map((zone) => zone.slice()),
      keep_tid: step.kind === "zone" ? step.tid || "" : "",
    })) : [newStep("zone")],
    active: 0,
  };
  document.getElementById("ed-title").textContent = routine ? "Edit " + routine.name : "New routine";
  document.getElementById("ed-name").value = routine ? routine.name : "";
  const select = document.getElementById("ed-vacuum");
  select.replaceChildren(...vacuums.map((vacuum) => el("option", { value: vacuum.duid, text: vacuum.name })));
  select.value = editor.deviceId;
  document.getElementById("ed-vacuum-row").hidden = routine !== null || vacuums.length < 2;
  showText("ed-error", "");
  document.getElementById("rt-card").hidden = true;
  document.getElementById("editor").hidden = false;
  renderSteps();
  loadMap(editor.deviceId);
}

function closeEditor() {
  editor = null;
  mapData = null;
  drag = null;
  document.getElementById("editor").hidden = true;
  document.getElementById("rt-card").hidden = false;
}

async function loadMap(deviceId) {
  mapData = null;
  document.getElementById("ed-map-wrap").hidden = true;
  showText("ed-map-hint", "Loading the map from the vacuum…");
  try {
    const data = await api("api/map?device=" + encodeURIComponent(deviceId));
    const image = document.getElementById("ed-map");
    await new Promise((resolve, reject) => {
      image.onload = resolve;
      image.onerror = () => reject(new Error("the map image did not load"));
      image.src = data.image;
    });
    if (!editor || editor.deviceId !== deviceId) return;
    mapData = data;
    const canvas = document.getElementById("ed-canvas");
    canvas.width = data.width;
    canvas.height = data.height;
    document.getElementById("ed-map-wrap").hidden = false;
    renderSteps();
  } catch (error) {
    if (!editor || editor.deviceId !== deviceId) return;
    showText("ed-map-hint", "Could not read the map from the vacuum: " + error.message + ". Picking rooms and drawing zones need the map.");
  }
}

function roomName(segmentId) {
  const room = mapData && mapData.rooms.find((candidate) => candidate.segment_id === segmentId);
  return room ? room.name : "Room " + segmentId;
}

function settingSelect(label, field, step, options) {
  const select = el("select", { onchange: (event) => { step[field] = Number(event.target.value); } });
  const values = Object.keys(options).map(Number);
  if (!values.includes(step[field])) values.push(step[field]);
  for (const value of values) {
    select.append(el("option", { value: String(value), text: options[value] || "Other (" + value + ")" }));
  }
  select.value = String(step[field]);
  return el("label", { className: "field" }, label, select);
}

function roomsBody(step) {
  const chosen = el("div", { className: "chips" });
  if (!step.rooms.length) chosen.append(el("span", { className: "hint", text: "Pick rooms in cleaning order." }));
  step.rooms.forEach((segmentId, position) => chosen.append(el("button", {
    type: "button", className: "chip on", text: position + 1 + ". " + roomName(segmentId) + " ✕",
    "aria-label": "Remove " + roomName(segmentId),
    onclick: () => { step.rooms.splice(position, 1); renderSteps(); },
  })));
  const body = el("div", {}, chosen);
  const available = mapData ? mapData.rooms.filter((room) => !step.rooms.includes(room.segment_id)) : [];
  if (available.length) {
    body.append(el("div", { className: "chips" }, ...available.map((room) => el("button", {
      type: "button", className: "chip", text: "+ " + room.name,
      onclick: () => { step.rooms.push(room.segment_id); renderSteps(); },
    }))));
  }
  return body;
}

function zoneBody(step) {
  if (keepsAppZones(step)) {
    const count = step.zone_count || 1;
    return el("div", {},
      el("p", { className: "hint", text: count + (count === 1 ? " zone" : " zones") +
        " saved on the vacuum by the Roborock app. They stay as they are unless you redraw them." }),
      el("button", { type: "button", className: "small", text: "Redraw",
        onclick: () => { step.keep_tid = ""; renderSteps(); } }));
  }
  const chips = el("div", { className: "chips" });
  if (!step.zones.length) {
    chips.append(el("span", { className: "hint", text: mapData ? "Drag on the map to draw a zone." : "Zones need the map." }));
  }
  step.zones.forEach((zone, position) => chips.append(el("button", {
    type: "button", className: "chip on", text: "Zone " + (position + 1) + " ✕", "aria-label": "Remove zone " + (position + 1),
    onclick: () => { step.zones.splice(position, 1); step.keep_tid = ""; renderSteps(); },
  })));
  return chips;
}

function setActive(index) {
  if (!editor || editor.active === index) return;
  editor.active = index;
  document.querySelectorAll("#ed-steps > li").forEach((item, position) => item.classList.toggle("active", position === index));
  drawOverlay();
  updateMapHint();
}

function moveStep(index, delta) {
  const [step] = editor.steps.splice(index, 1);
  editor.steps.splice(index + delta, 0, step);
  editor.active = index + delta;
  renderSteps();
}

function removeStep(index) {
  editor.steps.splice(index, 1);
  editor.active = Math.min(editor.active, editor.steps.length - 1);
  renderSteps();
}

function addStep(kind) {
  editor.steps.push(newStep(kind));
  editor.active = editor.steps.length - 1;
  renderSteps();
}

function renderSteps() {
  if (!editor) return;
  const options = routines.options;
  const list = document.getElementById("ed-steps");
  list.replaceChildren();
  editor.steps.forEach((step, index) => {
    const last = editor.steps.length - 1;
    const head = el("div", { className: "step-head" },
      el("span", { className: "step-title", text: index + 1 + ". " + (step.kind === "zone" ? "Zone" : "Rooms") }),
      el("span", { className: "routine-buttons" },
        el("button", { type: "button", className: "small", text: "↑", "aria-label": "Move step up", disabled: index === 0,
          onclick: () => moveStep(index, -1) }),
        el("button", { type: "button", className: "small", text: "↓", "aria-label": "Move step down", disabled: index === last,
          onclick: () => moveStep(index, 1) }),
        el("button", { type: "button", className: "small danger", text: "Remove", disabled: last === 0,
          onclick: () => removeStep(index) })));
    const settings = el("div", { className: "settings" },
      settingSelect("Suction", "fan_power", step, options.fan_power),
      settingSelect("Water", "water_box_mode", step, options.water_box_mode),
      settingSelect("Mop route", "mop_mode", step, options.mop_mode),
      settingSelect("Repeat", "repeat", step, { 1: "Once", 2: "Twice", 3: "3 times" }));
    // The head's buttons move and remove steps, so their clicks must not pick the old index.
    const pick = (event) => { if (!event.target.closest(".step-head")) setActive(index); };
    list.append(el("li", { className: "step" + (index === editor.active ? " active" : ""), onclick: pick },
      head, step.kind === "zone" ? zoneBody(step) : roomsBody(step), settings));
  });
  drawOverlay();
  updateMapHint();
}

function updateMapHint() {
  if (!editor || !mapData) return;
  const step = editor.steps[editor.active];
  const prefix = "Step " + (editor.active + 1) + ": ";
  if (step.kind === "rooms") showText("ed-map-hint", prefix + "tap rooms on the map or below, in cleaning order.");
  else if (keepsAppZones(step)) showText("ed-map-hint", prefix + "keeps the zones saved by the Roborock app.");
  else showText("ed-map-hint", prefix + "drag on the map to draw up to " + MAX_ZONES + " zones.");
}

function transform(matrix, x, y) {
  return [matrix[0] * x + matrix[1] * y + matrix[2], matrix[3] * x + matrix[4] * y + matrix[5]];
}

function zonePixels(zone) {
  const a = transform(mapData.to_px, zone[0], zone[1]);
  const b = transform(mapData.to_px, zone[2], zone[3]);
  return [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[0], b[0]), Math.max(a[1], b[1])];
}

function drawBox(context, box, label, unit, fill, stroke) {
  if (fill) {
    context.fillStyle = fill;
    context.fillRect(box[0], box[1], box[2] - box[0], box[3] - box[1]);
  }
  context.lineWidth = 2 * unit;
  context.strokeStyle = stroke;
  context.strokeRect(box[0], box[1], box[2] - box[0], box[3] - box[1]);
  if (label) drawLabel(context, label, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, unit);
}

function drawLabel(context, label, x, y, unit) {
  context.lineWidth = 3 * unit;
  context.strokeStyle = "rgba(255, 255, 255, 0.9)";
  context.fillStyle = "#1f1f1f";
  context.strokeText(label, x, y);
  context.fillText(label, x, y);
}

function drawOverlay() {
  if (!editor || !mapData) return;
  const canvas = document.getElementById("ed-canvas");
  const context = canvas.getContext("2d");
  context.clearRect(0, 0, canvas.width, canvas.height);
  const width = canvas.getBoundingClientRect().width;
  const unit = width ? canvas.width / width : 1;
  const accent = "#03a9f4";
  context.font = "600 " + Math.round(12 * unit) + "px sans-serif";
  context.textAlign = "center";
  context.textBaseline = "middle";
  const step = editor.steps[editor.active];
  const order = step.kind === "rooms" ? step.rooms : [];
  for (const room of mapData.rooms) {
    if (!room.label) continue;
    const position = order.indexOf(room.segment_id);
    if (position >= 0) drawBox(context, room.box, "", unit, null, accent);
    drawLabel(context, position >= 0 ? position + 1 + ". " + room.name : room.name, room.label[0], room.label[1], unit);
  }
  const drawing = step.kind === "zone" && !keepsAppZones(step);
  canvas.style.touchAction = drawing ? "none" : "auto";
  if (!drawing) return;
  step.zones.forEach((zone, position) => drawBox(context, zonePixels(zone), String(position + 1), unit, "rgba(3, 169, 244, 0.3)", accent));
  if (drag && drag.current) {
    const box = [Math.min(drag.start[0], drag.current[0]), Math.min(drag.start[1], drag.current[1]),
      Math.max(drag.start[0], drag.current[0]), Math.max(drag.start[1], drag.current[1])];
    drawBox(context, box, "", unit, "rgba(3, 169, 244, 0.15)", accent);
  }
}

function canvasPoint(event) {
  const canvas = document.getElementById("ed-canvas");
  const rect = canvas.getBoundingClientRect();
  return [(event.clientX - rect.left) * canvas.width / rect.width, (event.clientY - rect.top) * canvas.height / rect.height];
}

function toggleRoomAt(step, point) {
  // Room boxes overlap where rooms are not rectangles; the smallest one is the likeliest.
  let best = null;
  for (const room of mapData.rooms) {
    const box = room.box;
    if (!box || point[0] < box[0] || point[0] > box[2] || point[1] < box[1] || point[1] > box[3]) continue;
    const area = (box[2] - box[0]) * (box[3] - box[1]);
    if (!best || area < best.area) best = { room: room, area: area };
  }
  if (!best) return;
  const position = step.rooms.indexOf(best.room.segment_id);
  if (position >= 0) step.rooms.splice(position, 1);
  else step.rooms.push(best.room.segment_id);
  renderSteps();
}

function onPointerDown(event) {
  if (!editor || !mapData || event.button > 0) return;
  drag = { start: canvasPoint(event), current: null };
  event.currentTarget.setPointerCapture(event.pointerId);
}

function onPointerMove(event) {
  if (!drag) return;
  const step = editor.steps[editor.active];
  if (step.kind !== "zone" || keepsAppZones(step)) return;
  drag.current = canvasPoint(event);
  drawOverlay();
}

function onPointerUp(event) {
  if (!drag || !editor) return;
  const start = drag.start;
  drag = null;
  const end = canvasPoint(event);
  const step = editor.steps[editor.active];
  if (step.kind === "rooms") {
    if (Math.hypot(end[0] - start[0], end[1] - start[1]) < 8) toggleRoomAt(step, end);
    return;
  }
  if (keepsAppZones(step)) return;
  const a = transform(mapData.to_mm, start[0], start[1]);
  const b = transform(mapData.to_mm, end[0], end[1]);
  const zone = [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[0], b[0]), Math.max(a[1], b[1])].map(Math.round);
  if (zone[2] - zone[0] < MIN_ZONE_MM || zone[3] - zone[1] < MIN_ZONE_MM) {
    drawOverlay();
    return;
  }
  if (step.zones.length >= MAX_ZONES) {
    showText("ed-error", "A step can have at most " + MAX_ZONES + " zones.");
    drawOverlay();
    return;
  }
  showText("ed-error", "");
  step.zones.push(zone);
  renderSteps();
}

function stepPayload(step) {
  const payload = { kind: step.kind, fan_power: step.fan_power, water_box_mode: step.water_box_mode,
    mop_mode: step.mop_mode, repeat: step.repeat };
  if (step.kind === "rooms") {
    payload.rooms = step.rooms;
    payload.name = mapData ? step.rooms.map(roomName).join(", ").slice(0, 64) : step.name;
  } else if (step.keep_tid) {
    payload.keep_tid = step.keep_tid;
    payload.name = step.name;
  } else {
    payload.zones = step.zones;
    payload.name = step.zones.length > 1 ? step.zones.length + " zones" : "Zone";
  }
  return payload;
}

async function saveRoutine() {
  showText("ed-error", "");
  const name = document.getElementById("ed-name").value.trim();
  if (!name) {
    showText("ed-error", "Give the routine a name.");
    return;
  }
  const body = { name: name, steps: editor.steps.map(stepPayload) };
  if (editor.id === null) body.device_id = editor.deviceId;
  const button = document.getElementById("ed-save");
  button.disabled = true;
  try {
    await api(editor.id === null ? "api/routines" : "api/routines/" + editor.id, body);
    closeEditor();
    showText("rt-message", name + " saved.");
    await loadRoutines();
  } catch (error) {
    showText("ed-error", error.message);
  } finally {
    button.disabled = false;
  }
}

function changeVacuum(event) {
  // Rooms and zones belong to one vacuum's map.
  editor.deviceId = event.target.value;
  editor.steps = [newStep("zone")];
  editor.active = 0;
  renderSteps();
  loadMap(editor.deviceId);
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("nc-open").addEventListener("click", () => setNewConnections(true));
  document.getElementById("nc-close").addEventListener("click", () => setNewConnections(false));
  document.getElementById("rt-new").addEventListener("click", () => openEditor(null));
  document.getElementById("ed-cancel").addEventListener("click", closeEditor);
  document.getElementById("ed-save").addEventListener("click", saveRoutine);
  document.getElementById("ed-add-rooms").addEventListener("click", () => addStep("rooms"));
  document.getElementById("ed-add-zone").addEventListener("click", () => addStep("zone"));
  document.getElementById("ed-vacuum").addEventListener("change", changeVacuum);
  const canvas = document.getElementById("ed-canvas");
  canvas.addEventListener("pointerdown", onPointerDown);
  canvas.addEventListener("pointermove", onPointerMove);
  canvas.addEventListener("pointerup", onPointerUp);
  canvas.addEventListener("pointercancel", () => { drag = null; drawOverlay(); });
  window.addEventListener("resize", drawOverlay);
  refresh();
  loadRoutines();
  setInterval(refresh, 15000);
  setInterval(() => { if (status) renderNewConnections(); }, 1000);
});
"""


def admin_dashboard_url(stack_fqdn: str, https_port: int) -> str:
    port = "" if https_port == 443 else f":{https_port}"
    return f"https://{stack_fqdn}{port}/admin"


# Routine bodies are a few KB; the panel never needs more.
MAX_BODY_BYTES = 64 * 1024


class _BadRequest(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def _error(status_code: int, message: str) -> Response:
    return JSONResponse({"error": message}, status_code=status_code)


async def _json_body(request: Request) -> Any:
    # A JSON content type cannot be sent cross-site without a CORS preflight,
    # which this app never answers.
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise _BadRequest(415, "Content-Type must be application/json")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise _BadRequest(413, "Request body is too large")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise _BadRequest(413, "Request body is too large")
    try:
        return json.loads(body)
    except ValueError as exc:
        raise _BadRequest(400, "Invalid JSON body") from exc


def _user(request: Request) -> str:
    return request.headers.get("x-remote-user-display-name") or request.headers.get("x-remote-user-name") or "unknown user"


def build_ingress_app(
    *,
    admin_url: str,
    status: Callable[[], dict[str, Any]],
    set_new_connections_enabled: Callable[[bool], object] | None,
    routines: RoutinePanel | None = None,
    routine_editor: bool = False,
) -> FastAPI:
    """Build the panel.

    Without set_new_connections_enabled the new-connections window is read-only.
    With routines the panel lists and runs routines; routine_editor also lets it
    create, edit, rename and delete them and read the vacuum's map.
    """
    from .routine_panel import RoutineExecutionError, RoutineNotFound

    controls = set_new_connections_enabled is not None
    editor = routines is not None and routine_editor
    page = _PAGE.replace("{admin_url}", html.escape(admin_url, quote=True))
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def only_home_assistant_ingress(request: Request, call_next: Callable[[Request], Any]) -> Response:
        client_host = request.client.host if request.client is not None else ""
        if client_host != HA_INGRESS_PROXY:
            return PlainTextResponse("Forbidden", status_code=403)
        response = await call_next(request)
        response.headers.update(_SECURITY_HEADERS)
        return response

    @app.get("/")
    async def panel_page() -> Response:
        return HTMLResponse(page)

    @app.get("/panel.css")
    async def panel_css() -> Response:
        return Response(_CSS, media_type="text/css")

    @app.get("/panel.js")
    async def panel_js() -> Response:
        return Response(_JS, media_type="text/javascript")

    @app.get("/api/status")
    async def panel_status() -> Response:
        return JSONResponse({**status(), "controls": controls})

    @app.post("/api/new-connections")
    async def panel_new_connections(request: Request) -> Response:
        if set_new_connections_enabled is None:
            return _error(403, "New-connections controls are turned off for this panel")
        try:
            body = await _json_body(request)
        except _BadRequest as exc:
            return _error(exc.status_code, str(exc))
        enabled = body.get("enabled") if isinstance(body, dict) else None
        if not isinstance(enabled, bool):
            return _error(400, "enabled must be a boolean")
        set_new_connections_enabled(enabled)
        LOG.info("New connections %s from Home Assistant by %s", "opened" if enabled else "closed", _user(request))
        return JSONResponse({**status(), "controls": controls})

    if routines is None:
        return app

    def editor_off() -> Response:
        return _error(403, "Routine editing is turned off for this panel (ingress_routine_editor)")

    def routine_name(scene_id: int) -> str:
        try:
            return repr(routines.routine(scene_id)["name"])
        except RoutineNotFound:
            return str(scene_id)

    @app.get("/api/routines")
    async def panel_routines() -> Response:
        return JSONResponse({**routines.list_routines(), "editor": editor})

    @app.post("/api/routines/{scene_id}/run")
    async def panel_run_routine(scene_id: int, request: Request) -> Response:
        try:
            await _json_body(request)
            name = routine_name(scene_id)
            result = routines.run(scene_id)
        except _BadRequest as exc:
            return _error(exc.status_code, str(exc))
        except RoutineNotFound as exc:
            return _error(404, str(exc))
        except RoutineExecutionError as exc:
            return _error(409, str(exc))
        action = {"started": "started", "cancel_requested": "stop requested"}.get(result.get("status"), "not started")
        LOG.info("Routine %s %s from Home Assistant by %s", name, action, _user(request))
        return JSONResponse(result)

    @app.post("/api/routines")
    async def panel_create_routine(request: Request) -> Response:
        if not editor:
            return editor_off()
        try:
            created = routines.create(await _json_body(request))
        except _BadRequest as exc:
            return _error(exc.status_code, str(exc))
        except RoutineNotFound as exc:
            return _error(404, str(exc))
        except ValueError as exc:
            return _error(400, str(exc))
        LOG.info("Routine %r (%s) created from Home Assistant by %s", created["name"], created["id"], _user(request))
        return JSONResponse(created)

    @app.post("/api/routines/{scene_id}")
    async def panel_update_routine(scene_id: int, request: Request) -> Response:
        if not editor:
            return editor_off()
        try:
            updated = routines.update(scene_id, await _json_body(request))
        except _BadRequest as exc:
            return _error(exc.status_code, str(exc))
        except RoutineNotFound as exc:
            return _error(404, str(exc))
        except ValueError as exc:
            return _error(400, str(exc))
        LOG.info("Routine %r (%s) edited from Home Assistant by %s", updated["name"], scene_id, _user(request))
        return JSONResponse(updated)

    @app.post("/api/routines/{scene_id}/rename")
    async def panel_rename_routine(scene_id: int, request: Request) -> Response:
        if not editor:
            return editor_off()
        try:
            body = await _json_body(request)
            old_name = routine_name(scene_id)
            renamed = routines.rename(scene_id, body.get("name") if isinstance(body, dict) else None)
        except _BadRequest as exc:
            return _error(exc.status_code, str(exc))
        except RoutineNotFound as exc:
            return _error(404, str(exc))
        except ValueError as exc:
            return _error(400, str(exc))
        LOG.info("Routine %s renamed to %r from Home Assistant by %s", old_name, renamed["name"], _user(request))
        return JSONResponse(renamed)

    @app.post("/api/routines/{scene_id}/delete")
    async def panel_delete_routine(scene_id: int, request: Request) -> Response:
        if not editor:
            return editor_off()
        try:
            await _json_body(request)
            name = routine_name(scene_id)
            routines.delete(scene_id)
        except _BadRequest as exc:
            return _error(exc.status_code, str(exc))
        except RoutineNotFound as exc:
            return _error(404, str(exc))
        LOG.info("Routine %s deleted from Home Assistant by %s", name, _user(request))
        return JSONResponse({"deleted": scene_id})

    @app.get("/api/map")
    async def panel_map(device: str = "") -> Response:
        if not editor:
            return editor_off()
        try:
            return JSONResponse(await routines.vacuum_map(device))
        except RoutineNotFound as exc:
            return _error(404, str(exc))
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Could not read the map of vacuum %s: %s", device, exc)
            return _error(502, str(exc) or exc.__class__.__name__)

    return app
