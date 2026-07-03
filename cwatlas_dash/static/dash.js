"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const kHz = (hz) => (hz / 1e3).toFixed(2) + " kHz";
const gb = (b) => (b / 1e9).toFixed(2) + " GB";
const ts = (t) => new Date(t * 1000).toLocaleString();
const dur = (s) => s == null ? "—" :
  s < 3600 ? Math.round(s / 60) + " m" : (s / 3600).toFixed(1) + " h";

function errCard(el, err) {
  el.innerHTML = `<div class="error">unavailable — ${esc(err)}</div>`;
}
const failed = (d) => d && !Array.isArray(d) && d.error !== undefined;

function renderStatus(el, svc, sdr, adc, totals) {
  if (failed(svc)) return errCard(el, svc.error);
  const stateCls = svc.active_state === "active" ? "ok" : "bad";
  const sdrHtml = failed(sdr)
    ? `<span class="bad">unreachable</span>`
    : `<span class="ok">ok</span> <span class="sub">gps ${esc(sdr.gps ?? "?")}</span>`;
  const ov = failed(adc) ? "?" : adc.ov_mask;
  const ovCls = ov === "0" ? "ok" : "bad";
  const d = svc.disk || {};
  const freePct = d.total ? (100 * d.free / d.total).toFixed(0) : "?";
  el.innerHTML = `
    <div class="item"><b>collector</b>
      <span class="${stateCls}">${esc(svc.active_state)}</span>
      <span class="sub">up ${dur(svc.uptime_s)} · ${svc.n_restarts ?? 0} restarts</span></div>
    <div class="item"><b>sdr</b> ${sdrHtml}</div>
    <div class="item"><b>adc overload</b> <span class="${ovCls}">${esc(ov)}</span></div>
    <div class="item"><b>in flight</b> ${failed(totals) ? "?" : totals.in_flight}</div>
    <div class="item"><b>disk (${esc(d.path ?? "?")})</b>
      ${d.free ? gb(d.free) : "?"} free (${freePct}%)</div>`;
}

const card = (title, big, sub = "") =>
  `<div class="card"><h3>${esc(title)}</h3><div class="big">${big}</div>
   <div class="sub">${sub}</div></div>`;

function renderTotals(el, t) {
  if (failed(t)) { el.innerHTML = ""; return errCard(el, t.error); }
  el.innerHTML =
    card("captures (all time)", t.captures) +
    card("IQ hours", t.iq_hours) +
    card("corpus size", gb(t.bytes)) +
    card("contaminated", t.contaminated,
         t.captures ? (100 * t.contaminated / t.captures).toFixed(1) + " %" : "");
}

function renderWindows(el, windows) {
  el.innerHTML = Object.entries(windows).map(([w, s]) => {
    if (failed(s)) return card(w, `<span class="bad">err</span>`, esc(s.error));
    const bands = Object.entries(s.by_band)
      .map(([b, v]) => `${esc(b)} ${v.captures}`).join(" · ") || "—";
    return card(`last ${w}`, s.captures,
      `${s.iq_hours} IQ h · ${s.contaminated} contam.<br>${bands}`);
  }).join("");
}

function renderChart(el, hourly) {
  if (failed(hourly)) return errCard(el, hourly.error);
  const W = 960, H = 140, PAD = 20, bw = (W - PAD) / hourly.length;
  const max = Math.max(1, ...hourly.map((b) => b.captures));
  const bars = hourly.map((b, i) => {
    const h = (H - 30) * b.captures / max;
    const hc = (H - 30) * b.contaminated / max;
    const x = PAD + i * bw, y = H - 20 - h;
    const label = (b.ago_h % 6 === 0)
      ? `<text x="${x + bw / 2}" y="${H - 6}" text-anchor="middle">-${b.ago_h}h</text>` : "";
    return `<rect class="bar" x="${x}" y="${y}" width="${bw - 2}" height="${h}">
      <title>${b.ago_h}h ago: ${b.captures} captures, ${b.contaminated} contaminated, ${b.iq_hours} IQ h</title></rect>
      <rect class="contam" x="${x}" y="${H - 20 - hc}" width="${bw - 2}" height="${hc}"/>${label}`;
  }).join("");
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img"
    aria-label="captures per hour, last 24 hours">
    <text x="${PAD}" y="10">${max}</text>${bars}</svg>`;
}

const table = (heads, rows) => `<table><tr>${heads.map(([h, cls]) =>
  `<th class="${cls || ""}">${esc(h)}</th>`).join("")}</tr>${rows}</table>`;

function renderInflight(el, rows) {
  if (failed(rows)) return errCard(el, rows.error);
  if (!rows.length) { el.innerHTML = `<div class="sub">idle — no captures in flight</div>`; return; }
  el.innerHTML = table(
    [["freq"], ["band"], ["dwell", "num"], ["snr dB", "num"], ["keyed", "num"], [""]],
    rows.map((r) => `<tr><td>${kHz(r.freq_hz)}</td><td>${esc(r.band)}</td>
      <td class="num">${dur(r.dwell_s)}</td>
      <td class="num">${r.strength_db?.toFixed(0) ?? "—"}</td>
      <td class="num">${r.keyed_conf?.toFixed(2) ?? "—"}</td>
      <td>${r.stale ? '<span class="warn">stale?</span>' : ""}</td></tr>`).join(""));
}

function renderRecent(el, rows) {
  if (failed(rows)) return errCard(el, rows.error);
  el.innerHTML = table(
    [["started"], ["freq"], ["band"], ["dur", "num"], ["snr dB", "num"],
     ["keyed", "num"], [""]],
    rows.map((r) => `<tr><td>${ts(r.started_utc)}</td><td>${kHz(r.freq_hz)}</td>
      <td>${esc(r.band)}</td><td class="num">${dur(r.duration_s)}</td>
      <td class="num">${r.strength_db?.toFixed(0) ?? "—"}</td>
      <td class="num">${r.keyed_conf?.toFixed(2) ?? "—"}</td>
      <td>${r.contaminated ? '<span class="bad">contam.</span>' : ""}</td></tr>`).join(""));
}

function renderSolar(el, s) {
  if (failed(s)) return errCard(el, s.error);
  const rows = Object.entries(s.weights).map(([b, w]) =>
    `<tr><td>${esc(b)}</td><td class="num">${w.toFixed(1)}</td></tr>`).join("");
  el.innerHTML = `<div class="sub">phase: <b>${esc(s.phase)}</b> · nudges: n/a (MCP offline)</div>
    ${table([["band"], ["weight", "num"]], rows)}`;
}

function renderJournal(el, j) {
  if (failed(j)) return errCard(el, j.error);
  const cls = j.errors ? "bad" : "ok";
  el.innerHTML = `<div class="sub"><span class="${cls}">${j.errors} error lines</span>
    in last ${j.lines.length}</div><pre>${esc(j.lines.slice(-30).join("\n"))}</pre>`;
}

let missedPolls = 0;
async function poll() {
  try {
    const [sumR, capR] = await Promise.all([
      fetch("/api/summary"), fetch("/api/captures?limit=50")]);
    const d = await sumR.json(), caps = (await capR.json()).captures;
    missedPolls = 0;
    renderStatus($("panel-status"), d.service, d.sdr, d.adc, d.totals);
    renderTotals($("panel-totals"), d.totals);
    renderWindows($("panel-windows"), d.windows);
    renderChart($("panel-chart").querySelector(".body"), d.hourly);
    renderInflight($("panel-inflight").querySelector(".body"), d.inflight);
    renderRecent($("panel-recent").querySelector(".body"), caps);
    renderSolar($("panel-solar").querySelector(".body"), d.solar);
    renderJournal($("panel-journal").querySelector(".body"), d.journal);
  } catch (e) {
    missedPolls += 1;
  }
  $("stale-banner").classList.toggle("hidden", missedPolls < 2);
}
poll();
setInterval(poll, 15000);
