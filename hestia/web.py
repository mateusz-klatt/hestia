"""Bootstrap web UI — operator browses to ``http://hestia:8927`` and confirms
``name`` / ``room`` / ``type`` for every discovered node.

This is *bootstrap* tooling: the operator runs it once to teach hestia what each
Z-Wave node is, then the confirmed metadata lets hestia (and downstream Home
Assistant) talk about devices by their real names instead of node ids. Phase 3
will graduate proxy mode to standalone once enough nodes are confirmed.

Architecture: aiohttp runs in the same asyncio loop as the proxy/server
runtime, so handlers read ``rt`` directly and every write still goes through
``process_control_op`` / ``_persist``'s ``save_lock``.

Per-field UI saves — the row's name / room / type each have an independent
``[Save]`` (or ``[✓]`` for confirm-type) button. Each posts only its own field
so naming a device never accidentally freezes whatever ``type`` is currently
shown. The confirm button is **disabled** when the classifier hasn't yet
inferred a type, so the operator can't accidentally pin a node to ``unknown``.

Security: the listener defaults to ``127.0.0.1`` (loopback). Non-loopback binds
require ``HESTIA_WEB_ALLOW_REMOTE=1`` — same guard as the control port. The web
UI is unauthenticated; do not expose it.

Run: wired automatically by `hestia.proxy.main()` and `hestia.server.main()`.
Env: ``HESTIA_WEB_HOST`` (default ``127.0.0.1``), ``HESTIA_WEB_PORT``
(default ``8927``), ``HESTIA_WEB_ALLOW_REMOTE=1`` to bypass the loopback guard.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path

from aiohttp import ClientConnectionError, web

from .classifier import DeviceType
from .automations import rule_vocab
from .proxy import (IR_BUTTONS, KLIMA, _CLOSED_SENTINEL, _LOOPBACK, _merged_discovery,
                    globals_snapshot, process_control_op)

log = logging.getLogger("hestia.web")

MAX_BODY = 8192                                      # cap POST body size (bytes)
MAX_RULE_BODY = 65536                                # larger cap for an automation rule
MAX_STRING = 256                                     # cap name / room length (chars)
SSE_IDLE_TIMEOUT = 15.0                              # inner: queue.get idle → keepalive
SSE_MAX_LIFETIME = float(os.environ.get("HESTIA_SSE_LIFETIME", "3600"))
_TYPES = {t.value for t in DeviceType}
_clock = time.monotonic                              # module-level rebindable for tests
_NAME_FIELDS = {"op", "node", "name", "room", "type", "ep"}   # allowlist for /api/name (ep = endpoint label)
_BODY_NOT_OBJECT = "body must be a JSON object"   # shared 4xx message (/api/ir, /api/name, /api/control)
_CONTROL_OPS = {"switch", "level", "cover", "thermostat", "thermostat_power"}
_CONTROL_FIELDS = {
    "switch": {"op", "node", "on"},
    "level": {"op", "node", "value"},
    "cover": {"op", "node", "value"},
    "thermostat": {"op", "node", "celsius"},
    "thermostat_power": {"op", "node", "on"},
}
_RT_KEY = web.AppKey("rt", object)
_UI_DIST_ENV = "HESTIA_UI_DIST"
_DEFAULT_UI_DIST = Path(__file__).resolve().parent.parent / "ui" / "dist"


def _require_safe_web_bind(host: str) -> None:
    """Refuse to expose the unauthenticated web UI beyond loopback unless the
    operator explicitly opted in. Mirrors :func:`hestia.proxy._require_safe_control_bind`."""
    if host not in _LOOPBACK and os.environ.get("HESTIA_WEB_ALLOW_REMOTE") != "1":
        raise RuntimeError(
            f"refusing to bind the unauthenticated web UI to {host!r}; "
            "set HESTIA_WEB_ALLOW_REMOTE=1 to override (not recommended)"
        )


def _json(status, payload):
    body = json.dumps(payload).encode("utf-8")
    return web.Response(body=body, status=status, content_type="application/json")


def _empty(status, extra_headers=()):
    headers = {"Content-Length": "0"}
    headers.update(dict(extra_headers))
    return web.Response(status=status, body=b"", headers=headers)


async def _wait_event(queue: asyncio.Queue):
    """Wait on a subscriber queue inside the event loop, capped at ``SSE_IDLE_TIMEOUT``
    via an ``asyncio.timeout`` scope. Returns the event on success or
    ``None`` on idle (so the handler writes a keepalive comment instead of dying)."""
    try:
        async with asyncio.timeout(SSE_IDLE_TIMEOUT):
            return await queue.get()
    except asyncio.TimeoutError:
        return None


def _summary(devices: dict) -> dict:
    """Aggregate counters for the operator: how many nodes total, how many
    user-confirmed, how many still untyped by the classifier."""
    return {
        "total": len(devices),
        "confirmed": sum(1 for d in devices.values() if d.get("confidence") == "confirmed"),
        "unknown": sum(1 for d in devices.values() if d.get("type") == "unknown"),
    }


_INDEX_HTML = """<!doctype html>
<meta charset="utf-8">
<title>hestia — devices</title>
<style>
  body { font-family: ui-monospace, monospace; margin: 1.5rem; }
  h1 { font-size: 1.1rem; margin: 0 0 1rem 0; }
  table { border-collapse: collapse; }
  th, td { padding: 0.3rem 0.7rem; text-align: left; vertical-align: middle; }
  thead { border-bottom: 1px solid #888; }
  tbody tr { border-bottom: 1px solid #eee; }
  input { font: inherit; padding: 0.15rem 0.3rem; min-width: 8rem; }
  button { font: inherit; padding: 0.15rem 0.5rem; cursor: pointer; }
  button[disabled] { cursor: not-allowed; opacity: 0.5; }
  .status { color: #666; margin-left: 0.5rem; font-size: 0.85rem; }
  .err { color: #b00; }
  .confirmed { color: #060; font-weight: bold; }
  tr.recent td { background: rgba(255, 220, 0, 0.16); transition: background 1.5s ease-out; }
  tr.active td { background: rgba(255, 220, 0, 0.7); transition: background 2s ease-out; }
  td.seen { color: #888; font-size: 0.85rem; white-space: nowrap; }
  td.seen.fresh { color: #060; }
  td.batt { white-space: nowrap; }
  td.batt.low { color: #b00; font-weight: bold; }
  .scene-badge { margin-left: 0.5rem; color: #06c; font-weight: bold; opacity: 0; transition: opacity 0.6s ease-out; }
  .scene-badge.on { opacity: 1; transition: none; }
  tr.subrow td { border-top: none; padding-top: 1px; padding-bottom: 1px; }
  td.sub-label { color: #888; padding-left: 1.5rem; white-space: nowrap; }
  .ep-name { width: 9rem; }
  #conn { float: right; font-size: 0.85rem; color: #888; }
  #refresh { float: right; margin-left: 1rem; }
</style>
<h1 id="hdr">hestia — devices<span id="conn"></span><button id="refresh">Refresh</button></h1>
<div id="globals" style="margin:-0.4rem 0 0.9rem 0;color:#444;font-size:0.95rem;">
  🍼 crib: <span id="g-crib">—</span> &nbsp;·&nbsp; 🌡 outdoor: <span id="g-outdoor">—</span>
</div>
<div id="ir-buttons" style="margin:0 0 0.9rem 0;"></div>
<div id="klima" style="margin:0 0 0.9rem 0;"></div>
<div id="mode" style="margin:0 0 0.9rem 0;font-size:0.92rem;"></div>
<table>
  <thead><tr>
    <th>node</th><th>last seen</th><th>battery</th><th>inferred type</th><th>stan</th><th>akcje</th><th>name</th><th>room</th>
  </tr></thead>
  <tbody id="rows"></tbody>
</table>

<h1 id="auto-hdr" style="font-size:1.05rem;margin:1.8rem 0 0.6rem 0;">automations</h1>
<table>
  <thead><tr>
    <th>id</th><th>on</th><th>trigger</th><th>cond</th><th>actions</th><th></th>
  </tr></thead>
  <tbody id="auto-rows"></tbody>
</table>
<div id="rule-form" style="margin-top:0.9rem;font-size:0.92rem;"></div>
<div id="auto-editor" style="margin-top:0.9rem;">
  <textarea id="rule-json" rows="11" spellcheck="false"
            style="font:inherit;width:44rem;max-width:96%;display:block;"
            placeholder="rule JSON — click &quot;New rule template&quot; for a skeleton"></textarea>
  <div style="margin-top:0.4rem;">
    <button id="rule-template">New rule template</button>
    <button id="save-rule">Save rule</button>
    <span id="rule-status" class="status"></span>
  </div>
</div>
<script>
async function fetchDiscovery() {
  const r = await fetch('api/discovery');     // relative → works at / and behind a proxy subpath
  return r.ok ? r.json() : null;
}
async function post(payload) {
  const r = await fetch('api/name', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  return { ok: r.ok, status: r.status, body: await r.text() };
}
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
// Global (node-less) fields: crib_temp / outdoor_temp. `—` when null (poller off / no reading).
function fmtTemp(v) { return v == null ? '—' : v.toFixed(1) + '°'; }
function renderGlobals(g) {
  if (!g) return;
  if ('crib_temp' in g) document.getElementById('g-crib').textContent = fmtTemp(g.crib_temp);
  if ('outdoor_temp' in g) document.getElementById('g-outdoor').textContent = fmtTemp(g.outdoor_temp);
}
// SSE globals delta: queue during a rebuild (drainPending replays it) so the /api/discovery
// snapshot can't roll back a newer value that arrived mid-refresh — mirrors pendingState.
function applyGlobals(g) {
  if (!g) return;
  if (refreshing) { pendingGlobals = Object.assign(pendingGlobals || {}, g); return; }
  renderGlobals(g);
}
// IR buttons (configured via HESTIA_IR_BUTTONS) — each transmits a saved Flipper signal via /api/ir.
async function postIr(file, button) {
  const r = await fetch('api/ir', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({file: file, button: button}),
  });
  let body = {};
  try { body = await r.json(); } catch (e) {}
  return { ok: r.ok && !!body.ok, error: body.error };
}
async function postControl(op) {
  const r = await fetch('api/control', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(op),
  });
  let body = {};
  try { body = await r.json(); } catch (e) {}
  if (!r.ok && !body.error) body.error = 'error ' + r.status;
  body.ok = r.ok && !!body.ok;
  return body;
}
function renderIrButtons(buttons) {
  const box = document.getElementById('ir-buttons');
  if (!buttons || !buttons.length || box.dataset.built) return;   // static config → build once
  box.dataset.built = '1';
  const status = document.createElement('span');
  status.className = 'status';
  status.style.marginLeft = '0.5rem';
  for (const b of buttons) {
    const btn = document.createElement('button');
    btn.textContent = b.label;
    btn.style.marginRight = '0.4rem';
    btn.onclick = async () => {
      btn.disabled = true;
      status.textContent = '…';
      const res = await postIr(b.file, b.button);
      status.textContent = res.ok ? '✓ ' + b.label : '✗ ' + (res.error || 'failed');
      btn.disabled = false;
    };
    box.appendChild(btn);
  }
  box.appendChild(status);
}
// LG A/C panel — built from klima.ir signal NAMES (data-driven; no hard-coded modes). A mode+temp
// dropdown plus preset buttons (off/fan), each transmitting a saved Flipper signal via /api/ir. Every
// name goes in via textContent / option.value (it comes from a file — never interpolate it as HTML).
function renderKlima(klima) {
  const box = document.getElementById('klima');
  if (!klima || box.dataset.built) return;
  // Each "program" is the idempotent power-on signal on_<mode>_<temp> (operator-validated: it turns an
  // OFF unit on AND just re-programs a running one), so one "Ustaw" applies it; "Wyłącz" sends off.
  const programs = klima.power_on || {};
  const modeNames = Object.keys(programs).sort();
  const canOff = (klima.presets || []).includes('off');
  if (!modeNames.length && !canOff) return;   // nothing usable parsed → no panel
  box.dataset.built = '1';
  const lbl = document.createElement('span');
  lbl.textContent = '❄️ LG: ';
  box.appendChild(lbl);
  const status = document.createElement('span');
  status.className = 'status';
  status.style.marginLeft = '0.5rem';
  // One in-flight transmit at a time: a shared lock disables ALL klima buttons for the round-trip (no
  // concurrent/racing sends), re-enabled in finally so a failed send can't wedge them; errors surfaced.
  const buttons = [];
  let busy = false;
  const send = async (button, tag) => {
    if (busy) return;
    busy = true;
    buttons.forEach((b) => { b.disabled = true; });
    status.textContent = '…';
    try {
      const res = await postIr(klima.file, button);
      status.textContent = res.ok ? '✓ ' + tag : '✗ ' + (res.error || 'failed');
    } catch (e) {
      status.textContent = '✗ ' + ((e && e.message) || 'błąd');
    } finally {
      busy = false;
      buttons.forEach((b) => { b.disabled = false; });
    }
  };
  if (modeNames.length) {
    const mode = document.createElement('select');
    mode.style.marginRight = '0.3rem';
    for (const m of modeNames) {
      const o = document.createElement('option');
      o.value = m; o.textContent = m;
      mode.appendChild(o);
    }
    const temp = document.createElement('select');
    temp.style.marginRight = '0.3rem';
    const fillTemps = () => {
      while (temp.firstChild) temp.removeChild(temp.firstChild);
      for (const t of (programs[mode.value] || [])) {
        const o = document.createElement('option');
        o.value = t; o.textContent = t + '°';
        temp.appendChild(o);
      }
    };
    mode.onchange = fillTemps;
    fillTemps();
    const set = document.createElement('button');
    set.textContent = 'Ustaw';
    set.style.marginRight = '0.4rem';
    set.onclick = () => {
      if (!mode.value || !temp.value) return;
      send('on_' + mode.value + '_' + temp.value, mode.value + ' ' + temp.value + '°');
    };
    buttons.push(set);
    box.appendChild(mode); box.appendChild(temp); box.appendChild(set);
  }
  if (canOff) {
    const off = document.createElement('button');
    off.textContent = 'Wyłącz';
    off.style.marginRight = '0.4rem';
    off.onclick = () => send('off', 'Wyłącz');
    buttons.push(off);
    box.appendChild(off);
  }
  box.appendChild(status);
}
// Battery level (%) for nodes that report one; '—' for mains (no report). A
// value above 100 is the Z-Wave low-battery sentinel (e.g. 0xff) — show "low"
// rather than a bogus "255%". null/undefined → mains.
function battFmt(pct) {
  if (pct == null) return '—';
  if (pct > 100) return 'low';
  return pct + '%';
}
function battLow(pct) { return pct != null && (pct > 100 || pct < 20); }
// Type-aware live state ("stan") cell. Uses `!= null` throughout so a blind at
// 0% or a switch that's `false` still renders. Only `door` is string-origin, so
// it's the only field that needs escaping; the rest are numbers/booleans.
function stateStr(info) {
  if (info.type === 'blind')
    return info.level == null ? '—' : `▣ ${info.level}%`;
  if (info.type === 'thermostat') {
    let s = '';
    if (info.temperature != null) s += info.temperature + '°';
    if (info.setpoint != null) s += (s ? ' → ' : '→ ') + info.setpoint + '°';
    if (info.thermostat_on != null) s += (s ? ' ' : '') + (info.thermostat_on ? '⏻on' : 'off');
    return s || '—';
  }
  if (info.type === 'light') {
    if (info.endpoints) {
      const eps = Object.keys(info.endpoints);
      if (eps.length > 1) return '';            // multi-gang: each channel renders as its own sub-row
      return info.endpoints[eps[0]] ? 'on' : 'off';   // single channel → flat
    }
    return info.switch == null ? '—' : (info.switch ? 'on' : 'off');
  }
  if (info.type === 'plug') {
    const parts = [];
    if (info.switch != null) parts.push(info.switch ? 'on' : 'off');   // most important first
    if (info.power_w != null) parts.push(info.power_w + ' W');
    if (info.energy_kwh != null) parts.push(info.energy_kwh + ' kWh');
    if (info.voltage_v != null) parts.push(info.voltage_v + ' V');
    return parts.length ? parts.join(' · ') : '—';
  }
  if (info.type === 'door')
    return info.door == null ? '—' : esc(info.door);
  return '—';                                  // motion / smoke / water / unknown — no numeric state yet
}
function row(node, info) {
  const tr = document.createElement('tr');
  tr.dataset.node = node;
  tr.dataset.type = info.type || '';
  const confirmed = info.confidence === 'confirmed';
  const isUnknown = !info.type || info.type === 'unknown';
  tr.innerHTML = `
    <td>${esc(node)}</td>
    <td class="seen">—</td>
    <td class="batt${battLow(info.battery) ? ' low' : ''}">${battFmt(info.battery)}</td>
    <td><span class="${confirmed ? 'confirmed' : ''}">${esc(info.type || '?')} (${esc(info.confidence || '?')})</span>
        <button class="confirm" ${isUnknown || confirmed ? 'disabled' : ''}>✓ confirm</button>
        <span class="status"></span></td>
    <td class="stan"><span class="stanval">${stateStr(info)}</span><span class="scene-badge"></span></td>
    <td class="actions"></td>
    <td><input class="name" value="${esc(info.name)}">
        <button class="save-name">Save</button>
        <span class="status"></span></td>
    <td><input class="room" value="${esc(info.room)}">
        <button class="save-room">Save</button>
        <span class="status"></span></td>`;
  return tr;
}
function renderActions(cell, node, info) {
  if (!cell) return;
  // Endpoint-addressed control is deferred; multi-gang rows stay read-only in v1.
  if (info.endpoints != null) return;
  const buttons = [];
  const status = document.createElement('span');
  status.className = 'status';
  let busy = false;
  const setButtons = (disabled) => {
    buttons.forEach((b) => { b.disabled = disabled; });
  };
  const send = async (payload, pending) => {
    if (busy) return;
    busy = true;
    setButtons(true);
    status.textContent = pending ? '… ' + pending : '…';
    status.className = 'status';
    try {
      const res = await postControl(payload);
      status.textContent = res.ok ? '✓ wysłano' : '✗ ' + (res.error || 'failed');
      status.className = 'status' + (res.ok ? '' : ' err');
    } catch (e) {
      status.textContent = '✗ ' + ((e && e.message) || 'błąd');
      status.className = 'status err';
    } finally {
      busy = false;
      setButtons(false);
    }
  };
  const addButton = (label, payload, pending) => {
    const btn = document.createElement('button');
    btn.textContent = label;
    btn.style.marginRight = '0.3rem';
    btn.onclick = () => send(typeof payload === 'function' ? payload() : payload,
                             typeof pending === 'function' ? pending() : pending);
    buttons.push(btn);
    cell.appendChild(btn);
    return btn;
  };
  const addLevelSelect = () => {
    const sel = document.createElement('select');
    sel.style.marginRight = '0.3rem';
    for (const value of [10, 25, 50, 75, 99]) {
      const o = document.createElement('option');
      o.value = value;
      o.textContent = value + '%';
      sel.appendChild(o);
    }
    cell.appendChild(sel);
    addButton('Ustaw', () => ({op: 'level', node, value: parseInt(sel.value, 10)}));
  };
  const clampSetpoint = (delta) => {
    const raw = info.setpoint == null ? 21 : Number(info.setpoint);
    const current = Number.isFinite(raw) ? raw : 21;
    return Math.min(30, Math.max(5, current + delta));
  };
  if (info.type === 'light') {
    if (info.level != null) {
      addButton('Wył', {op: 'level', node, value: 0});
      addButton('Wł', {op: 'level', node, value: 99});
      addLevelSelect();
    } else {
      addButton('Wł', {op: 'switch', node, on: true});
      addButton('Wył', {op: 'switch', node, on: false});
    }
  } else if (info.type === 'plug') {
    addButton('Wł', {op: 'switch', node, on: true});
    addButton('Wył', {op: 'switch', node, on: false});
  } else if (info.type === 'blind') {
    addButton('Podnieś', {op: 'cover', node, value: 99});
    addButton('Opuść', {op: 'cover', node, value: 0});
  } else if (info.type === 'thermostat') {
    addButton('Wył', {op: 'thermostat_power', node, on: false});
    addButton('Wł', {op: 'thermostat_power', node, on: true});
    addButton('−', () => {
      const celsius = clampSetpoint(-0.5);
      return {op: 'thermostat', node, celsius};
    }, () => clampSetpoint(-0.5).toFixed(1) + '°');
    addButton('+', () => {
      const celsius = clampSetpoint(0.5);
      return {op: 'thermostat', node, celsius};
    }, () => clampSetpoint(0.5).toFixed(1) + '°');
  }
  if (buttons.length) cell.appendChild(status);
}
function bindRow(tr) {
  const node = parseInt(tr.dataset.node, 10);
  const inferred = tr.dataset.type;
  renderActions(tr.querySelector('.actions'), node, infoByNode.get(node) || {});
  const setStatus = (which, text, isErr) => {
    const el = tr.querySelector(`.${which}`).parentElement.querySelector('.status');
    el.textContent = text;
    el.className = 'status' + (isErr ? ' err' : '');
  };
  tr.querySelector('.confirm').onclick = async () => {
    const r = await post({node, type: inferred});
    setStatus('confirm', r.ok ? 'confirmed' : r.body, !r.ok);
    if (r.ok) refresh();
  };
  tr.querySelector('.save-name').onclick = async () => {
    const r = await post({node, name: tr.querySelector('.name').value});
    setStatus('save-name', r.ok ? 'saved' : r.body, !r.ok);
  };
  tr.querySelector('.save-room').onclick = async () => {
    const r = await post({node, room: tr.querySelector('.room').value});
    setStatus('save-room', r.ok ? 'saved' : r.body, !r.ok);
  };
}
// A per-endpoint sub-row of a multi-gang switch: its own on/off + editable label.
// Shares data-node with its parent (so flash etc. find the parent via :not([data-ep]))
// but carries data-ep so it's individually addressable.
function subRow(node, ep, on, name) {
  const tr = document.createElement('tr');
  tr.dataset.node = node;
  tr.dataset.ep = ep;
  tr.className = 'subrow';
  tr.innerHTML = `
    <td></td><td></td><td></td>
    <td class="sub-label">↳ kanał ${esc(ep)}</td>
    <td class="stan ep-stan">${on ? 'on' : 'off'}</td>
    <td class="actions"></td>
    <td><input class="ep-name" value="${esc(name)}">
        <button class="save-ep-name">Save</button>
        <span class="status"></span></td>
    <td></td>`;
  return tr;
}
function bindSubRow(tr) {
  const node = parseInt(tr.dataset.node, 10);
  const ep = parseInt(tr.dataset.ep, 10);          // sent as a JSON number
  tr.querySelector('.save-ep-name').onclick = async () => {
    const r = await post({node, ep, name: tr.querySelector('.ep-name').value});
    const st = tr.querySelector('.status');
    st.textContent = r.ok ? 'saved' : r.body;
    st.className = 'status' + (r.ok ? '' : ' err');
  };
}
const flashTimers = new Map();             // node OR `node:ep` → setTimeout handle
const sceneTimers = new Map();              // node → scene-badge clear timer
const lastActiveByNode = new Map();         // node → wall-clock ms of last activity
const pendingFlash = new Set();             // nodes flashed before their row existed
const infoByNode = new Map();               // node(int) → last-known discovery info (for live patches)
const pendingState = new Map();             // state deltas that arrived mid-refresh → replayed after
const pendingScene = new Map();             // scene flashes that arrived mid-refresh → replayed after
let pendingGlobals = null;                  // globals delta that arrived mid-refresh → replayed after
let refreshing = false;
let refreshAgain = false;                   // a refresh was requested while one was in flight
const HIGHLIGHT_MS = 2200;
const RECENT_MS = 90000;                    // subtle glow lingers this long after the bright flash

// A `state` SSE delta: merge the changed fields into the cached row and re-render
// its "stan" cell only — no /api/discovery refetch. Text-only, so it's edit-safe.
function applyStatePatch(node, fields) {
  if (refreshing) {                          // a full rebuild is in flight; replay afterwards
    pendingState.set(node, Object.assign(pendingState.get(node) || {}, fields));
    return;
  }
  const info = infoByNode.get(node);
  if (!info) return;                         // row not built yet — the next refresh covers it
  const prevEndpoints = info.endpoints;      // snapshot before the merge — to spot which channel changed
  Object.assign(info, fields);
  // Multi-gang: state lives in per-endpoint sub-rows, not the node-row "stan".
  if (fields.endpoints && Object.keys(info.endpoints).length > 1) {
    let missing = false;
    for (const ep of Object.keys(info.endpoints)) {
      const sub = document.querySelector(`tr[data-node="${node}"][data-ep="${ep}"]`);
      if (!sub) { missing = true; continue; }
      sub.querySelector('.ep-stan').textContent = info.endpoints[ep] ? 'on' : 'off';
      if (!prevEndpoints || prevEndpoints[ep] !== info.endpoints[ep])
        flashRow(sub, `${node}:${ep}`);      // this channel's state changed → highlight the sub-row
    }
    if (missing) refresh();                  // a newly-appeared endpoint → rebuild the sub-rows once
    return;
  }
  const tr = document.querySelector(`tr[data-node="${node}"]:not([data-ep])`);  // node row only
  const cell = tr && tr.querySelector('.stan .stanval');  // not the whole cell — keep the scene badge
  if (cell) cell.textContent = stateStr(info);
}
// Replay deltas that queued during a rebuild, against the freshly-built rows.
function drainPending() {
  for (const [node, fields] of pendingState) applyStatePatch(node, fields);
  pendingState.clear();
  for (const [node, scene] of pendingScene) flashScene(node, scene);
  pendingScene.clear();
  if (pendingGlobals) { renderGlobals(pendingGlobals); pendingGlobals = null; }
}

function flash(node) {
  lastActiveByNode.set(node, Date.now());   // always record — decoupled from DOM
  const tr = document.querySelector(`tr[data-node="${node}"]:not([data-ep])`);
  if (!tr) { pendingFlash.add(node); return; }
  applyHighlight(tr, node);
}
function applyHighlight(tr, node) {
  const elapsed = Date.now() - lastActiveByNode.get(node);
  if (elapsed >= HIGHLIGHT_MS) return;
  clearTimeout(flashTimers.get(node));
  tr.classList.add('active');
  flashTimers.set(node, setTimeout(() => tr.classList.remove('active'),
                                   HIGHLIGHT_MS - elapsed));
}
// Bright-flash an arbitrary row on a state change, keyed by an id so a channel
// sub-row (`node:ep`) and its parent node row never share a timer. Used for
// per-endpoint sub-rows, whose changes arrive as `state` deltas, not activity events.
function flashRow(tr, key) {
  clearTimeout(flashTimers.get(key));
  tr.classList.add('active');
  flashTimers.set(key, setTimeout(() => tr.classList.remove('active'), HIGHLIGHT_MS));
}
const SCENE_MS = 4000;
// A function-button press (Scene Activation / Central Scene). Transient: shows a
// brief "⏏ scena N" badge next to the state, then clears — it's an event, not state,
// so it lives in its own span and is never stored in infoByNode.
function flashScene(node, scene) {
  if (refreshing) { pendingScene.set(node, scene); return; }   // rebuild in flight → replay after
  const tr = document.querySelector(`tr[data-node="${node}"]:not([data-ep])`);
  const badge = tr && tr.querySelector('.scene-badge');
  if (!badge) { pendingScene.set(node, scene); return; }       // row not built yet → replay after
  badge.textContent = `⏏ scena ${scene.id}`;
  badge.classList.add('on');
  clearTimeout(sceneTimers.get(node));
  sceneTimers.set(node, setTimeout(() => {
    badge.classList.remove('on');
    badge.textContent = '';
  }, SCENE_MS));
}

function relTime(ms) {
  if (ms == null) return '—';
  const s = Math.floor((Date.now() - ms) / 1000);
  if (s < 2) return 'now';
  if (s < 60) return s + 's ago';
  const m = Math.floor(s / 60);
  if (m < 60) return m + 'm ago';
  return Math.floor(m / 60) + 'h ago';
}
// Tick once a second: refresh each row's relative "last seen" text and the
// lingering `recent` glow. Text/class only — never rebuilds rows, so it can't
// clobber an in-progress edit and adds no perceptible lag.
function tickActivity() {
  for (const tr of document.querySelectorAll('tr[data-node]:not([data-ep])')) {
    const node = parseInt(tr.dataset.node, 10);
    const ts = lastActiveByNode.get(node);
    const cell = tr.querySelector('.seen');
    if (cell) cell.textContent = relTime(ts);
    if (ts == null) continue;
    const recent = Date.now() - ts < RECENT_MS;
    tr.classList.toggle('recent', recent);
    if (cell) cell.classList.toggle('fresh', recent);
  }
}
setInterval(tickActivity, 1000);

async function refresh() {
  // Don't redraw while the operator is typing — the rebuild below clears
  // every <input>, so a refresh in the middle of an edit would silently
  // erase what was being typed.
  const ae = document.activeElement;
  if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'BUTTON')) return;
  if (refreshing) { refreshAgain = true; return; }   // coalesce overlapping rebuilds
  refreshing = true;                          // queue any state deltas until the rebuild is done
  refreshAgain = false;
  const data = await fetchDiscovery();
  if (!data) { refreshing = false; drainPending(); if (refreshAgain) refresh(); return; }
  renderGlobals(data.globals);
  renderIrButtons(data.ir_buttons);
  renderKlima(data.klima);
  renderRuleForm(data.rule_vocab, data.klima);
  renderMode(data);
  const tb = document.getElementById('rows');
  tb.innerHTML = '';
  infoByNode.clear();
  const nodes = Object.keys(data.devices).sort((a, b) => +a - +b);
  for (const n of nodes) {
    const info = data.devices[n];
    infoByNode.set(+n, info);
    const tr = row(n, info);
    tb.appendChild(tr);
    bindRow(tr);
    // A multi-gang switch (>1 endpoint) gets one editable sub-row per channel.
    if (info.endpoints && Object.keys(info.endpoints).length > 1) {
      const names = info.endpoint_names || {};
      for (const ep of Object.keys(info.endpoints).sort((a, b) => a - b)) {
        const sub = subRow(n, ep, info.endpoints[ep], names[ep] || '');
        tb.appendChild(sub);
        bindSubRow(sub);
      }
    }
  }
  // Reapply highlight to rows that activated during the refresh, plus replay
  // queued flashes for newly-appeared rows.
  for (const tr of tb.querySelectorAll('tr[data-node]:not([data-ep])')) {
    const node = parseInt(tr.dataset.node, 10);
    if (lastActiveByNode.has(node)) applyHighlight(tr, node);
  }
  const queued = Array.from(pendingFlash);
  pendingFlash.clear();
  for (const n of queued) flash(n);
  // Prune entries for nodes that disappeared from the DOM and whose highlight
  // has expired — keeps the map bounded over a long session.
  for (const [node, ts] of lastActiveByNode) {
    if (Date.now() - ts > HIGHLIGHT_MS &&
        !tb.querySelector(`tr[data-node="${node}"]:not([data-ep])`)) {
      lastActiveByNode.delete(node);
      clearTimeout(sceneTimers.get(node));   // tidy the parallel scene-badge timer too
      sceneTimers.delete(node);
    }
  }
  const s = data.summary;
  document.getElementById('hdr').firstChild.textContent =
    `hestia — devices (${s.confirmed}/${s.total} confirmed, ${s.unknown} unknown) `;
  tickActivity();                            // populate "last seen" cells right after a rebuild
  refreshing = false;
  drainPending();
  if (refreshAgain) refresh();               // a refresh landed mid-rebuild → run once more
}

document.getElementById('refresh').onclick = refresh;

// Server-Sent Events: row activity + discovery deltas. Browser auto-reconnects
// on drop; `onopen` re-syncs state.
const conn = document.getElementById('conn');
const es = new EventSource('api/events');     // relative → resolves under the proxy subpath too
es.onopen = () => { conn.textContent = ''; refresh(); loadAutomations(); };
es.onerror = () => { conn.textContent = '(reconnecting…)'; };
es.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.type === 'activity') { flash(m.node); if (m.scene) flashScene(m.node, m.scene); }
  else if (m.type === 'discovery_changed') refresh();
  else if (m.type === 'state') applyStatePatch(m.node, m.fields);
  else if (m.type === 'globals') applyGlobals(m.fields);
};
refresh();

// ---- Automations editor -------------------------------------------------
// CRUD over the api/automations endpoints (GET=list, POST=set, POST /delete).
// Operator-driven, ~0 events/min → simple load-on-mutation, no SSE. The rule
// schema (3 trigger types + ANDed conditions + 7 action ops) is authored as
// JSON in the textarea; `Rule.from_dict` server-side is the validator and its
// error message is surfaced verbatim. esc() guards every rule-derived string.
const RULE_TEMPLATE = {
  id: 'my-rule', enabled: true, modes: ['proxy', 'standalone'], debounce: 0,
  trigger: {type: 'scene', node: 0, scene_id: 1},
  conditions: [],
  actions: [{op: 'switch', node: 0, on: true}],
};
const ruleStatus = document.getElementById('rule-status');
const ruleBox = document.getElementById('rule-json');
function setRuleStatus(text, isErr) {
  ruleStatus.textContent = text;
  ruleStatus.className = 'status' + (isErr ? ' err' : '');
}
function trigSummary(t) {
  if (!t) return '?';
  if (t.type === 'scene') return `scene ${t.scene_id} @node ${t.node}`;
  if (t.type === 'state') return (t.node === undefined            // global (node-less) fields omit node
    ? `${t.field} ${t.op} ${t.value}` : `node ${t.node} ${t.field} ${t.op} ${t.value}`);
  if (t.type === 'time') return `at ${t.at}${t.days ? ' [' + t.days.join(',') + ']' : ''}`;
  if (t.type === 'sun') return `${t.event}${t.offset_min ? (t.offset_min > 0 ? '+' : '') + t.offset_min + 'm' : ''}`
    + `${t.days ? ' [' + t.days.join(',') + ']' : ''}`;
  if (t.type === 'presence') return `${t.mac} ${t.event}`;
  if (t.type === 'cron') return `cron ${t.expr}`;
  return t.type;
}
async function postRule(payload) {                     // → {ok, status, body}
  const r = await fetch('api/automations', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)});
  let body = null;
  try { body = await r.json(); } catch (e) { /* empty/non-JSON (503/504) */ }
  return {ok: r.ok, status: r.status, body};
}
async function loadAutomations() {
  const tb = document.getElementById('auto-rows');
  const r = await fetch('api/automations');
  if (!r.ok) { tb.innerHTML = ''; setRuleStatus('(automations unavailable)', true); return; }
  const data = await r.json();
  tb.innerHTML = '';
  for (const rule of (data.automations || [])) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${esc(rule.id)}</td>
      <td><input type="checkbox" class="auto-en" ${rule.enabled ? 'checked' : ''}></td>
      <td>${esc(trigSummary(rule.trigger))}</td>
      <td>${(rule.conditions || []).length}</td>
      <td>${(rule.actions || []).map(a => esc(a.op)).join(', ')}</td>
      <td><button class="auto-edit">Edit</button>
          <button class="auto-del">Delete</button>
          <span class="status"></span></td>`;
    tb.appendChild(tr);
    const rowStatus = tr.querySelector('.status');
    tr.querySelector('.auto-edit').onclick = () => {
      ruleBox.value = JSON.stringify(rule, null, 2);
      setRuleStatus(`editing ${rule.id}`, false);
    };
    tr.querySelector('.auto-del').onclick = async () => {
      if (!confirm(`Delete rule "${rule.id}"?`)) return;
      const r = await fetch('api/automations/delete', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id: rule.id})});
      if (r.ok) loadAutomations();
      else { const t = await r.text();             // 503/504 send an empty body → fall back to the code
             rowStatus.textContent = t || ('error ' + r.status); rowStatus.className = 'status err'; }
    };
    // Enable/disable inline: re-save the whole rule with `enabled` flipped.
    tr.querySelector('.auto-en').onchange = async (ev) => {
      const want = ev.target.checked;
      const res = await postRule(Object.assign({}, rule, {enabled: want}));
      if (res.ok) loadAutomations();
      else { ev.target.checked = rule.enabled;     // revert the box; surface the error
             rowStatus.textContent = (res.body && res.body.error) || ('error ' + res.status);
             rowStatus.className = 'status err'; }
    };
  }
}
document.getElementById('rule-template').onclick = () => {
  ruleBox.value = JSON.stringify(RULE_TEMPLATE, null, 2);
  setRuleStatus('template loaded — edit then Save', false);
};
document.getElementById('save-rule').onclick = async () => {
  let parsed;
  try { parsed = JSON.parse(ruleBox.value); }
  catch (e) { setRuleStatus('invalid JSON: ' + e.message, true); return; }
  const res = await postRule(parsed);
  if (res.ok) { setRuleStatus('saved', false); ruleBox.value = ''; loadAutomations(); }
  else { setRuleStatus((res.body && res.body.error) || ('error ' + res.status), true); }
};
// ---- M3.1 guided rule form -------------------------------------------------
function _opt(parent, value, label) {
  const o = document.createElement('option');
  o.value = value; o.textContent = (label === undefined ? value : label);
  parent.appendChild(o); return o;
}
function _sel(values) {
  const s = document.createElement('select'); s.style.marginRight = '0.25rem';
  for (const v of values) _opt(s, v);
  return s;
}
function _finp(ph, size) {
  const i = document.createElement('input'); i.type = 'text';
  i.placeholder = ph; i.size = size || 7; i.style.marginRight = '0.25rem';
  return i;
}
function _parseNode(s) {                 // hex (0x..) or decimal -> int, else null
  const t = (s || '').trim();
  if (/^0x[0-9a-fA-F]+$/.test(t)) return parseInt(t, 16);
  if (/^[0-9]+$/.test(t)) return parseInt(t, 10);
  return null;
}
function _coerce(s) {                     // predicate value -> number | bool | string ('' -> undefined)
  const t = (s || '').trim();
  if (t === '') return undefined;
  if (/^-?[0-9]+(\\.[0-9]+)?$/.test(t)) return Number(t);   // decimal incl. floats (e.g. 21.5)
  if (t === 'true') return true;
  if (t === 'false') return false;
  return t;
}
function _num(s, label) {                  // required finite number (blank/NaN/Inf throw) — Rule.from_dict
  const t = (s || '').trim();             // does NOT check action params, so a bad number must fail HERE
  if (t === '') throw new Error(label + ': liczba wymagana');
  const n = Number(t);
  if (!isFinite(n)) throw new Error(label + ': nieprawidłowa liczba');
  return n;
}
function daysPicker() {                   // Mon=0..Sun=6 (matches _validate_days); none checked -> null
  const names = ['Pn', 'Wt', 'Śr', 'Cz', 'Pt', 'So', 'Nd'];
  const wrap = document.createElement('span'); wrap.style.marginRight = '0.3rem';
  const boxes = names.map((nm) => {
    const c = document.createElement('input'); c.type = 'checkbox';
    const l = document.createElement('label'); l.style.marginRight = '0.15rem';
    l.append(c, document.createTextNode(nm)); wrap.appendChild(l); return c;
  });
  return { el: wrap, read() {
    const d = boxes.map((c, i) => (c.checked ? i : -1)).filter((i) => i >= 0);
    return d.length ? d : null;
  }};
}
// state predicate editor (field/op/value [+node when the field is not GLOBAL]); reused by the
// `state` trigger and each condition. read() -> {field,op,value[,node]} or throws Error.
function predicateEditor(vocab) {
  const wrap = document.createElement('span');
  const field = _sel(Object.keys(vocab.state_fields));
  const op = _sel(vocab.cmp_ops);
  const val = _finp('value', 7);
  const node = _finp('node', 6);
  const syncNode = () => { node.style.display = vocab.state_fields[field.value] ? 'none' : ''; };
  field.onchange = syncNode;
  wrap.append(field, op, val, node); syncNode();
  return { el: wrap, read() {
    const f = field.value, v = _coerce(val.value);
    if (v === undefined) throw new Error('predykat „' + f + '": brak wartości');
    const p = { field: f, op: op.value, value: v };
    if (!vocab.state_fields[f]) {
      const n = _parseNode(node.value);
      if (n === null) throw new Error('predykat „' + f + '": node wymagany');
      p.node = n;
    }
    return p;
  }};
}
// Builds a rule object from dropdowns/inputs and writes it into #rule-json (operator reviews → existing
// "Save rule" validates server-side via Rule.from_dict). Vocab comes from data.rule_vocab so the form
// can't drift from validation; the klima action reuses data.klima. raw/lights ops are not offered here
// (author those in the JSON box). Edit is one-way (form fills the textarea). All text via value/
// textContent (XSS-safe). Built once.
function renderRuleForm(vocab, klima) {
  const box = document.getElementById('rule-form');
  if (!vocab || box.dataset.built) return;
  box.dataset.built = '1';
  const mk = (label, el) => {
    const w = document.createElement('span'); w.style.marginRight = '0.6rem';
    const l = document.createElement('label'); l.textContent = label + ' '; l.style.color = '#555';
    w.append(l, el); box.appendChild(w); return el;
  };
  const line = () => box.appendChild(document.createElement('br'));
  const hdr = document.createElement('div'); hdr.textContent = 'Kreator reguły';
  hdr.style.cssText = 'font-weight:bold;margin-bottom:0.3rem;'; box.appendChild(hdr);
  const idIn = mk('id', _finp('rule-id', 14));
  const enIn = document.createElement('input'); enIn.type = 'checkbox'; enIn.checked = true; mk('enabled', enIn);
  const dbIn = _finp('0', 4); dbIn.value = '0'; mk('debounce s', dbIn);
  const modeBoxes = {};
  for (const m of vocab.modes) {
    const c = document.createElement('input'); c.type = 'checkbox'; c.checked = true;
    modeBoxes[m] = c; mk(m, c);
  }
  line();
  // trigger
  const tType = mk('trigger', _sel(vocab.trigger_types));
  const tFields = document.createElement('span'); box.appendChild(tFields);
  let tRead = () => ({});
  const buildTrigger = () => {
    while (tFields.firstChild) tFields.removeChild(tFields.firstChild);
    const t = tType.value;
    if (t === 'scene') {
      const node = _finp('node', 6), sid = _finp('scene_id', 5); tFields.append(node, sid);
      tRead = () => { const n = _parseNode(node.value); if (n === null) throw new Error('scene: node'); return { node: n, scene_id: _num(sid.value, 'scene_id') }; };
    } else if (t === 'state') {
      const pe = predicateEditor(vocab); tFields.append(pe.el); tRead = () => pe.read();
    } else if (t === 'time') {
      const at = _finp('HH:MM', 6); const days = daysPicker(); tFields.append(at, days.el);
      tRead = () => { const a = at.value.trim(); if (!a) throw new Error('time: at'); const o = { at: a }; const d = days.read(); if (d) o.days = d; return o; };
    } else if (t === 'sun') {
      const ev = _sel(vocab.sun_events), off = _finp('offset min', 6); off.value = '0';
      const days = daysPicker(); tFields.append(ev, off, days.el);
      tRead = () => { const o = { event: ev.value, offset_min: (off.value.trim() === '' ? 0 : _num(off.value, 'offset')) }; const d = days.read(); if (d) o.days = d; return o; };
    } else if (t === 'presence') {
      const mac = _finp('aa:bb:cc:dd:ee:ff', 17), ev = _sel(vocab.presence_events); tFields.append(mac, ev);
      tRead = () => { const m = mac.value.trim(); if (!m) throw new Error('presence: mac'); return { mac: m, event: ev.value }; };
    } else {
      const expr = _finp('* * * * *', 14); tFields.append(expr);
      tRead = () => { const e = expr.value.trim(); if (!e) throw new Error('cron: expr'); return { expr: e }; };
    }
  };
  tType.onchange = buildTrigger; buildTrigger();
  line();
  // conditions (0+)
  const condLbl = document.createElement('span'); condLbl.textContent = 'warunki: '; condLbl.style.color = '#555'; box.appendChild(condLbl);
  const condBox = document.createElement('span'); box.appendChild(condBox);
  const conds = [];
  const addCondBtn = document.createElement('button'); addCondBtn.textContent = '+ warunek';
  addCondBtn.onclick = () => {
    const pe = predicateEditor(vocab);
    const row = document.createElement('span'); row.style.marginRight = '0.3rem';
    const entry = { read: pe.read };
    const rm = document.createElement('button'); rm.textContent = '×'; rm.title = 'usuń';
    rm.onclick = () => { condBox.removeChild(row); conds.splice(conds.indexOf(entry), 1); };
    row.append(pe.el, rm); condBox.appendChild(row); conds.push(entry);
  };
  box.appendChild(addCondBtn);
  line();
  // actions (1+)
  const actLbl = document.createElement('span'); actLbl.textContent = 'akcje: '; actLbl.style.color = '#555'; box.appendChild(actLbl);
  const actBox = document.createElement('span'); box.appendChild(actBox);
  const acts = [];
  const klimaModes = (klima && klima.power_on) ? Object.keys(klima.power_on).sort() : [];
  const addAction = () => {
    const row = document.createElement('span'); row.style.marginRight = '0.3rem';
    const op = _sel((klimaModes.length ? ['klima'] : []).concat(['switch', 'level', 'cover', 'thermostat', 'thermostat_power', 'ir']));
    const fields = document.createElement('span');
    let aRead = () => ({});
    const buildAct = () => {
      while (fields.firstChild) fields.removeChild(fields.firstChild);
      const k = op.value;
      if (k === 'klima') {
        const mode = _sel(klimaModes.concat(['off'])), temp = _sel([]);
        const fill = () => {
          while (temp.firstChild) temp.removeChild(temp.firstChild);
          temp.style.display = (mode.value === 'off') ? 'none' : '';
          for (const t of (klima.power_on[mode.value] || [])) _opt(temp, t, t + '°');
        };
        mode.onchange = fill; fields.append(mode, temp); fill();
        aRead = () => ({ op: 'ir', file: klima.file, button: (mode.value === 'off') ? 'off' : ('on_' + mode.value + '_' + temp.value) });
      } else if (k === 'ir') {
        const file = _finp('/ext/infrared/x.ir', 18), btn = _finp('button', 10); fields.append(file, btn);
        aRead = () => { const f = file.value.trim(), b = btn.value.trim(); if (!f || !b) throw new Error('ir: file+button'); return { op: 'ir', file: f, button: b }; };
      } else if (k === 'switch' || k === 'thermostat_power') {
        const node = _finp('node', 6), on = _sel(['on', 'off']); fields.append(node, on);
        aRead = () => { const n = _parseNode(node.value); if (n === null) throw new Error(k + ': node'); return { op: k, node: n, on: (on.value === 'on') }; };
      } else if (k === 'level' || k === 'cover') {
        const node = _finp('node', 6), value = _finp('value', 5); fields.append(node, value);
        aRead = () => { const n = _parseNode(node.value); if (n === null) throw new Error(k + ': node'); return { op: k, node: n, value: _num(value.value, k + ' value') }; };
      } else {
        const node = _finp('node', 6), c = _finp('°C', 5); fields.append(node, c);
        aRead = () => { const n = _parseNode(node.value); if (n === null) throw new Error('thermostat: node'); return { op: 'thermostat', node: n, celsius: _num(c.value, 'celsius') }; };
      }
    };
    op.onchange = buildAct; buildAct();
    const entry = { read: () => aRead() };
    const rm = document.createElement('button'); rm.textContent = '×'; rm.title = 'usuń';
    rm.onclick = () => { if (acts.length <= 1) return; actBox.removeChild(row); acts.splice(acts.indexOf(entry), 1); };
    row.append(op, fields, rm); actBox.appendChild(row); acts.push(entry);
  };
  const addActBtn = document.createElement('button'); addActBtn.textContent = '+ akcja'; addActBtn.onclick = addAction;
  box.appendChild(addActBtn); addAction();
  line();
  // build → JSON
  const buildBtn = document.createElement('button'); buildBtn.textContent = 'Zbuduj JSON';
  const formStatus = document.createElement('span'); formStatus.className = 'status'; formStatus.style.marginLeft = '0.5rem';
  buildBtn.onclick = () => {
    try {
      const id = idIn.value.trim(); if (!id) throw new Error('id wymagane');
      const modes = vocab.modes.filter((m) => modeBoxes[m].checked);
      if (!modes.length) throw new Error('wybierz tryb');
      const rule = {
        id: id, enabled: enIn.checked, modes: modes,
        debounce: (dbIn.value.trim() === '' ? 0 : _num(dbIn.value, 'debounce')),
        trigger: Object.assign({ type: tType.value }, tRead()),
        conditions: conds.map((c) => c.read()),
        actions: acts.map((a) => a.read()),
      };
      ruleBox.value = JSON.stringify(rule, null, 2);
      formStatus.textContent = 'zbudowano — sprawdź i „Save rule"'; formStatus.className = 'status';
    } catch (e) {
      formStatus.textContent = '✗ ' + e.message; formStatus.className = 'status err';
    }
  };
  box.append(buildBtn, formStatus);
}
// Phase-3 mode panel — shows the running mode + a "switch to standalone" button that PERSISTS the
// choice (applied on hestia restart); surfaces a HESTIA_MODE env override honestly. Built once.
function renderMode(data) {
  const box = document.getElementById('mode');
  if (!data || box.dataset.built) return;
  box.dataset.built = '1';
  const running = data.mode || 'proxy', target = data.target_mode || 'proxy', envOverride = data.env_override;
  const label = document.createElement('span'); label.textContent = 'tryb: ' + running; box.appendChild(label);
  const status = document.createElement('span'); status.className = 'status'; status.style.marginLeft = '0.5rem';
  const note = (text, color) => {
    const s = document.createElement('span');
    s.style.cssText = 'color:' + color + ';font-size:0.82rem;margin-left:0.5rem;';
    s.textContent = text; box.appendChild(s);
  };
  if (envOverride) {
    note('(HESTIA_MODE=' + envOverride + ' wymusza tryb; zapisany: ' + target + ')', '#888');
  } else if (running === 'standalone') {
    note('(cloud-free)', '#2a2');
  } else if (target === 'standalone') {
    note('→ standalone zapisane — zrestartuj hestię, aby zadziałało', '#a60');
  } else {
    const btn = document.createElement('button'); btn.textContent = 'Przełącz na standalone'; btn.style.marginLeft = '0.5rem';
    btn.onclick = async () => {
      if (!confirm('Przełączyć w tryb standalone (bez chmury)? Wymaga restartu hestii.')) return;
      btn.disabled = true; status.textContent = '…';
      try {
        const r = await fetch('api/graduate', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
        let body = {}; try { body = await r.json(); } catch (e) {}
        if (r.ok && body.ok) { status.textContent = '✓ zapisane — zrestartuj hestię'; btn.remove(); }
        else { status.textContent = '✗ ' + (body.error || ('error ' + r.status)); btn.disabled = false; }
      } catch (e) { status.textContent = '✗ ' + ((e && e.message) || 'błąd'); btn.disabled = false; }
    };
    box.appendChild(btn);
  }
  box.appendChild(status);
}
loadAutomations();
</script>
"""


@web.middleware
async def _empty_404_405_middleware(request, handler):
    try:
        return await handler(request)
    except web.HTTPMethodNotAllowed as exc:
        return _empty(HTTPStatus.METHOD_NOT_ALLOWED, [("Allow", exc.headers.get("Allow", ""))])
    except web.HTTPNotFound:
        return _empty(HTTPStatus.NOT_FOUND)


def _rt(request):
    return request.app[_RT_KEY]


async def _index(_request):  # NOSONAR S7503: aiohttp route handlers must be coroutines (the framework awaits them)
    return web.Response(text=_INDEX_HTML, content_type="text/html")


def _ui_dist_dir() -> Path:
    return Path(os.environ.get(_UI_DIST_ENV, str(_DEFAULT_UI_DIST)))


def _ui_built_file_response(path: Path):
    # 404 unless the UI is built (index.html present) AND the requested file exists — an asset not in
    # the bundle (or any request before `npm run build`) must 404, never hand a missing path to FileResponse.
    if not (_ui_dist_dir() / "index.html").is_file() or not path.is_file():
        raise web.HTTPNotFound
    return web.FileResponse(path)


async def _ui_index(_request):  # NOSONAR S7503: aiohttp route handlers must be coroutines (the framework awaits them)
    return _ui_built_file_response(_ui_dist_dir() / "index.html")


async def _ui_asset(request):  # NOSONAR S7503: aiohttp route handlers must be coroutines (the framework awaits them)
    return _ui_built_file_response(_ui_dist_dir() / "assets" / request.match_info["path"])


async def _discovery(request):  # NOSONAR S7503: aiohttp route handlers must be coroutines (the framework awaits them)
    rt = _rt(request)
    devices = _merged_discovery(rt)
    globs = globals_snapshot(rt.state)
    # mode = the RUNNING server; target_mode = the PERSISTED registry mode (Phase-3 graduation,
    # applied on next restart); env_override = HESTIA_MODE if it is pinning the mode (else null).
    return _json(HTTPStatus.OK,
                 {"devices": devices, "summary": _summary(devices), "globals": globs,
                  "ir_buttons": IR_BUTTONS, "klima": KLIMA, "rule_vocab": rule_vocab(),
                  "mode": rt.mode, "target_mode": rt.registry.mode,
                  "env_override": os.environ.get("HESTIA_MODE")})


async def _events(request):
    """Server-Sent Events stream — pushes `activity` (`[1e 09]` frame
    → row flash) and `discovery_changed` (name op / classifier update
    → UI re-fetch). Heatmap UX for onboarding: operator wiggles a
    switch, the row lights up, they know which to name."""
    sub = await _rt(request).event_bus.try_subscribe()
    if sub is None:                            # cap reached or bus closing
        return _empty(HTTPStatus.TOO_MANY_REQUESTS)

    resp = web.StreamResponse(
        status=HTTPStatus.OK,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    try:
        await resp.prepare(request)
        deadline = _clock() + SSE_MAX_LIFETIME
        while _clock() < deadline:
            event = await _wait_event(sub.queue)
            if event is _CLOSED_SENTINEL:
                break                          # graceful shutdown
            payload = (b":keepalive\n\n" if event is None
                       else f"data: {json.dumps(event)}\n\n".encode("utf-8"))
            try:
                await resp.write(payload)
            except (ConnectionResetError, ClientConnectionError, asyncio.CancelledError):
                break                          # browser EventSource auto-reconnects
    finally:
        sub.close()
    return resp


async def _read_json_body(request, max_body=MAX_BODY):
    """Read and JSON-parse the request body, enforcing framing limits. On any
    content-type/framing/size/parse error this returns ``(response, True)``;
    on success returns ``(obj, False)``. A zero-length body parses to ``{}``
    (preserving the original ``/api/name`` contract); ``max_body`` caps the byte
    size per endpoint (names are small, rules larger).

    Requiring ``Content-Type: application/json`` is the CSRF guard for these
    device-controlling mutations: it is NOT a CORS "simple" content type, so a
    cross-origin browser request must clear a preflight first — which this server
    never grants — blocking a malicious page from POSTing a forged body (e.g. a
    time rule that actuates devices) to the loopback UI or the auth'd reverse proxy.
    First-party fetches already send it; non-browser clients set it trivially."""
    ctype = request.headers.get("Content-Type", "")
    if ctype.split(";")[0].strip().lower() != "application/json":
        return _json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                     {"ok": False, "error": "Content-Type must be application/json"}), True
    cl_raw = request.headers.get("Content-Length")
    if cl_raw is None:
        return _json(HTTPStatus.LENGTH_REQUIRED,
                     {"ok": False, "error": "Content-Length required"}), True
    cl = int(cl_raw)
    if cl > max_body:
        return _json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                     {"ok": False, "error": f"body must be ≤ {max_body} bytes"}), True
    body = await request.content.read(cl) if cl > 0 else b""
    try:
        return (json.loads(body) if body else {}), False
    except ValueError:
        return _json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid JSON"}), True


async def _dispatch_op(rt, op, *, fail_status=HTTPStatus.INTERNAL_SERVER_ERROR):
    """Run a control op and map the outcome: ValueError/KeyError/TypeError → 400,
    ``ok`` → 200, else → ``fail_status``."""
    try:
        resp = await process_control_op(rt, op)
    except (ValueError, KeyError, TypeError) as exc:
        return _json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
    if resp.get("ok"):
        return _json(HTTPStatus.OK, resp)
    return _json(fail_status, resp)


async def _name(request):
    op, err = await _read_json_body(request)
    if err:
        return op
    error = _validate_name_payload(op)
    if error is not None:
        return _json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": error})
    op["op"] = "name"                          # normalise after validation
    return await _dispatch_op(_rt(request), op)


async def _control(request):
    op, err = await _read_json_body(request)
    if err:
        return op
    error = _validate_control_payload(op)
    if error is not None:
        return _json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": error})
    return await _dispatch_op(_rt(request), op, fail_status=HTTPStatus.SERVICE_UNAVAILABLE)


async def _graduate(request):
    """POST /api/graduate — persist standalone mode (Phase-3; applied on the next restart). Takes
    no body, but still requires ``Content-Type: application/json`` — the same CSRF guard the other
    device-affecting mutations use, so a cross-origin form-POST can't trigger graduation."""
    body, err = await _read_json_body(request)         # enforce the JSON content-type; the parsed body is unused
    if err:
        return body
    return await _dispatch_op(_rt(request), {"op": "graduate"},
                              fail_status=HTTPStatus.SERVICE_UNAVAILABLE)


async def _ir(request):
    """Transmit a saved IR signal via the Flipper. Body: ``{"file","button"}``.
    The op's own ``ok/error`` (disabled / queue full / timed out / failed) is surfaced,
    a non-ok mapping to 503 so the dashboard flags it."""
    op, err = await _read_json_body(request)
    if err:
        return op
    if not isinstance(op, dict):                 # a list/scalar JSON body would crash op.get()
        return _json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": _BODY_NOT_OBJECT})
    ir_file, button = op.get("file"), op.get("button")
    if not (isinstance(ir_file, str) and ir_file and isinstance(button, str) and button):
        return _json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "file and button required"})
    return await _dispatch_op(_rt(request), {"op": "ir", "file": ir_file, "button": button},
                              fail_status=HTTPStatus.SERVICE_UNAVAILABLE)


async def _automations_list(request):
    """List every authored rule. The ``automations`` op is read-only and returns ``ok: True``."""
    resp = await process_control_op(_rt(request), {"op": "automations"})
    return _json(HTTPStatus.OK, resp)


async def _automations_set(request):
    """Create/replace a rule. The body IS the rule spec; ``Rule.from_dict`` (inside
    the op) is the authoritative validator — its ValueError maps to 400."""
    body, err = await _read_json_body(request, MAX_RULE_BODY)
    if err:
        return body
    return await _dispatch_op(_rt(request), {"op": "automation_set", "rule": body})


async def _automations_delete(request):
    """Delete a rule by id. Pull ``id`` out only when the body is an object, so a
    non-dict JSON body yields ``id=None`` → the op's ValueError → 400 (never an
    uncaught AttributeError from ``body.get`` on a list/str/number)."""
    body, err = await _read_json_body(request, MAX_RULE_BODY)   # same cap as set → any settable id is deletable
    if err:
        return body
    rid = body.get("id") if isinstance(body, dict) else None
    return await _dispatch_op(_rt(request), {"op": "automation_delete", "id": rid})


def _validate_name_payload(op) -> "str | None":
    if not isinstance(op, dict):
        return _BODY_NOT_OBJECT
    unknown = set(op) - _NAME_FIELDS
    if unknown:
        return f"unknown field(s): {sorted(unknown)}"      # explicit allowlist
    explicit_op = op.get("op")
    if explicit_op is not None and explicit_op != "name":
        return "/api/name only accepts op=name"
    if "node" not in op:
        return "'node' field is required"
    fields_present = [k for k in ("name", "room", "type") if k in op]
    if not fields_present:
        return "at least one of name, room, type is required"
    error = _validate_name_strings(op)
    if error is not None:
        return error
    dtype = op.get("type")
    if dtype is not None and dtype not in _TYPES:
        return f"invalid type {dtype!r}"
    ep = op.get("ep")
    if ep is not None and (not isinstance(ep, int) or isinstance(ep, bool) or ep < 0):
        return "ep must be a non-negative integer"
    return None


def _validate_name_strings(op) -> "str | None":
    for key in ("name", "room"):
        value = op.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > MAX_STRING):
            return f"{key} must be a string ≤ {MAX_STRING} chars"
    return None


def _control_node_error(op) -> "str | None":
    node = op.get("node")
    if not isinstance(node, int) or isinstance(node, bool) or not 0 <= node <= 255:
        return "node must be an integer 0..255"
    return None


def _control_on_error(op) -> "str | None":
    if not isinstance(op.get("on"), bool):
        return "on must be a boolean"
    return None


def _control_value_error(op) -> "str | None":
    value = op.get("value")
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 99:
        return "value must be an integer 0..99"
    return None


def _control_celsius_error(op) -> "str | None":
    # Reject non-numbers, bool (True == 1), and json's NaN/Infinity (a float that isn't finite)
    # before the range check; round(°C*10) is the wire encoding, so 5..30 °C is the safe band.
    celsius = op.get("celsius")
    if (not isinstance(celsius, (int, float)) or isinstance(celsius, bool)
            or (isinstance(celsius, float) and not math.isfinite(celsius))
            or not 5 <= celsius <= 30):
        return "celsius must be a number between 5 and 30"
    return None


# Per-op field validator (node is checked separately, for every op).
_CONTROL_FIELD_VALIDATORS = {
    "switch": _control_on_error,
    "thermostat_power": _control_on_error,
    "level": _control_value_error,
    "cover": _control_value_error,
    "thermostat": _control_celsius_error,
}


def _validate_control_payload(op) -> "str | None":
    """Validate a /api/control body: a strict allowlist of device ops (never `raw`/`lights`),
    no unknown fields, and per-op operand bounds. Returns an error string or None when valid."""
    if not isinstance(op, dict):
        return _BODY_NOT_OBJECT
    name = op.get("op")
    if name not in _CONTROL_OPS:
        return f"unsupported control op {name!r}"
    unknown = set(op) - _CONTROL_FIELDS[name]
    if unknown:
        return f"unknown field(s): {sorted(unknown)}"
    return _control_node_error(op) or _CONTROL_FIELD_VALIDATORS[name](op)


def make_app(rt):
    app = web.Application(
        client_max_size=MAX_RULE_BODY + 4096,
        middlewares=[_empty_404_405_middleware],
    )
    app[_RT_KEY] = rt
    app.router.add_get("/", _index, allow_head=False)
    app.router.add_get("/ui/", _ui_index, allow_head=False)
    app.router.add_get("/ui/assets/{path:[A-Za-z0-9._-]+}", _ui_asset, allow_head=False)
    app.router.add_get("/api/discovery", _discovery, allow_head=False)
    app.router.add_get("/api/events", _events, allow_head=False)
    app.router.add_get("/api/automations", _automations_list, allow_head=False)
    app.router.add_post("/api/name", _name)
    app.router.add_post("/api/ir", _ir)
    app.router.add_post("/api/control", _control)
    app.router.add_post("/api/automations", _automations_set)
    app.router.add_post("/api/automations/delete", _automations_delete)
    app.router.add_post("/api/graduate", _graduate)
    return app


@dataclass(frozen=True)
class _WebHandle:
    runner: web.AppRunner
    address: tuple[str, int]


async def start_web(rt, host: str = None, port: int = None):
    """Bind and start aiohttp in the current asyncio loop; return a small handle.

    ``host`` defaults to ``$HESTIA_WEB_HOST`` (``127.0.0.1``); ``port`` defaults
    to ``$HESTIA_WEB_PORT`` (``8927``). A non-loopback ``host`` requires
    ``HESTIA_WEB_ALLOW_REMOTE=1``."""
    if host is None:
        host = os.environ.get("HESTIA_WEB_HOST", "127.0.0.1")
    if port is None:
        port = int(os.environ.get("HESTIA_WEB_PORT", "8927"))
    _require_safe_web_bind(host)
    runner = web.AppRunner(make_app(rt), shutdown_timeout=2.0, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    bound = runner.addresses[0]
    address = (bound[0], bound[1])
    log.info("web UI listening on http://%s:%d", *address)
    return _WebHandle(runner=runner, address=address)


async def stop_web(handle: _WebHandle) -> None:
    await handle.runner.cleanup()
