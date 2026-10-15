/* DeskSense - dashboard client
 *
 * Live scores + camera frame arrive via WebSocket push from the WebUI-HTML
 * Brick ("live_sample" messages, pushed by the video-pump thread at ~6-7fps
 * for smooth video; the scores inside only actually change once a second,
 * when the scoring loop last ran).
 * History/today summaries are still simple REST pulls (low frequency, no
 * benefit to pushing them).
 *
 * Mood has been removed entirely - see README. Gaze is now a real signal
 * (face_present / looking_at_screen), sourced from an OpenCV DNN face
 * detector rather than MediaPipe.
 */

const HISTORY_POLL_MS = 20000;

/* ---- Auth ----
 * The session token lives in localStorage and goes out as an Authorization
 * bearer header on every API call, plus in the socket's "auth" handshake
 * message. The server pushes nothing to a socket that hasn't cleared that
 * handshake, so the dashboard stays blank (rather than half-populated) until
 * we're signed in.
 */
const TOKEN_KEY = "desksense_token";

function getToken() {
  try { return localStorage.getItem(TOKEN_KEY) || null; } catch (err) { return null; }
}
function setToken(token) {
  try { localStorage.setItem(TOKEN_KEY, token); } catch (err) { /* private mode */ }
}
function clearToken() {
  try { localStorage.removeItem(TOKEN_KEY); } catch (err) { /* private mode */ }
}

async function authedFetch(path, options) {
  const opts = Object.assign({}, options || {});
  const token = getToken();
  opts.headers = Object.assign({}, opts.headers || {});
  if (token) opts.headers["Authorization"] = "Bearer " + token;

  const res = await fetch(path, opts);
  if (res.status === 401) {
    // Token expired or was revoked server-side - drop straight back to login
    // instead of letting every poller retry against a dead session.
    handleSessionLost();
    throw new Error("unauthenticated");
  }
  return res;
}

async function postJson(path, body) {
  return fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function errorDetail(res, fallback) {
  try {
    const data = await res.json();
    return data.detail || fallback;
  } catch (err) {
    return fallback;
  }
}

function showError(el, message) {
  el.textContent = message;
  el.hidden = false;
}

function setupTabs() {
  const items = document.querySelectorAll(".nav-item");
  const panels = document.querySelectorAll(".tab-panel");
  items.forEach((item) => {
    item.addEventListener("click", () => {
      const target = item.dataset.tab;
      items.forEach((i) => i.classList.toggle("active", i === item));
      panels.forEach((p) => { p.hidden = p.dataset.tabPanel !== target; });
    });
  });
}

/* Time-of-day bucket. The palette no longer shifts with it (the dark theme
 * is fixed), but the bucket still drives the header greeting, and the body
 * class is kept as an existing hook. */
function applyTimeOfDayTheme() {
  const hour = new Date().getHours();
  let bucket;
  if (hour >= 5 && hour < 12) bucket = "morning";
  else if (hour >= 12 && hour < 17) bucket = "afternoon";
  else if (hour >= 17 && hour < 21) bucket = "evening";
  else bucket = "night";

  document.body.classList.remove("time-morning", "time-afternoon", "time-evening", "time-night");
  document.body.classList.add("time-" + bucket);

  const greetings = {
    morning: "Good morning",
    afternoon: "Good afternoon",
    evening: "Good evening",
    night: "Working late",
  };
  const el = document.getElementById("greeting");
  if (el) {
    const sub = el.querySelector(".greeting-sub");
    el.textContent = greetings[bucket];
    if (sub) el.appendChild(sub);
  }
}

/* "4h 32m" with the units muted at half size. Returns HTML, so callers
 * assign it with innerHTML. */
function bigDuration(seconds) {
  const totalMin = Math.max(0, Math.round((seconds || 0) / 60));
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  if (h > 0) return h + '<span class="unit">h</span> ' + m + '<span class="unit">m</span>';
  return m + '<span class="unit">m</span>';
}

const THEME_KEY = "desksense_theme";

function applyTheme(name) {
  const theme = name === "light" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", theme);
  document.querySelectorAll("#themeToggle .theme-option").forEach((b) => {
    b.classList.toggle("active", b.dataset.themeChoice === theme);
  });
}

function setupTheme() {
  let saved = null;
  try { saved = localStorage.getItem(THEME_KEY); } catch (err) { /* private mode */ }
  applyTheme(saved || "dark");

  const toggle = document.getElementById("themeToggle");
  if (!toggle) return;
  toggle.addEventListener("click", (ev) => {
    const btn = ev.target.closest(".theme-option");
    if (!btn) return;
    const choice = btn.dataset.themeChoice;
    applyTheme(choice);
    try { localStorage.setItem(THEME_KEY, choice); } catch (err) { /* private mode */ }
  });
}

/* Day / Week / Month above the Trends charts. Presentation only for now -
 * it toggles its own active pill and deliberately does not refetch or
 * re-window any data. */
function setupRangePills() {
  const group = document.getElementById("trendsRange");
  if (!group) return;
  group.addEventListener("click", (ev) => {
    const btn = ev.target.closest(".range-pill");
    if (!btn) return;
    group.querySelectorAll(".range-pill").forEach((b) => b.classList.toggle("active", b === btn));
  });
}

function setGauge(prefix, value, ringColorVar) {
  const gauge = document.getElementById(prefix + "Gauge");
  const num = document.getElementById(prefix + "Num");
  const pct = Math.max(0, Math.min(100, Math.round(value)));
  gauge.style.setProperty("--pct", pct);
  if (ringColorVar) gauge.style.setProperty("--ring-color", ringColorVar);
  num.textContent = pct;
}

function scoreColor(score) {
  if (score >= 75) return "var(--teal)";
  if (score >= 50) return "var(--amber)";
  return "var(--coral)";
}

function fmtMinutes(seconds) {
  const mins = Math.round((seconds || 0) / 60);
  return mins + " min";
}

function applyLiveSample(data) {
  const pill = document.getElementById("statusPill");
  const statusText = document.getElementById("statusText");

  if (data.status !== "ok") {
    pill.className = "status-pill";
    statusText.textContent = data.status === "starting_up" ? "starting up…" : "connecting…";
    return;
  }

  if (!data.person_present) {
    pill.className = "status-pill away";
    statusText.textContent = "away from desk";
  } else {
    pill.className = "status-pill live";
    statusText.textContent = data.state;
  }

  // Posture (bbox-baseline proxy - see hint copy in index.html)
  const postureLabel = document.getElementById("postureLabel");
  if (data.posture_label === "calibrating") {
    setGauge("posture", 0, "var(--text-faint)");
    document.getElementById("postureNum").textContent = "…";
    postureLabel.textContent = "calibrating";
    document.getElementById("postureHint").textContent = "Sit normally for a few seconds while we set your baseline.";
  } else {
    setGauge("posture", data.posture_score, scoreColor(data.posture_score));
    postureLabel.textContent = data.person_present ? data.posture_label : "no one at desk";
    const issues = (data.posture_issues || []).join(", ");
    document.getElementById("postureHint").textContent = data.person_present
      ? (issues ? "Flagged: " + issues.replaceAll("_", " ") : "Matches your baseline - keep it up.")
      : "No person detected in frame.";
  }

  // Engagement
  setGauge("engagement", data.engagement_score, scoreColor(data.engagement_score));
  document.getElementById("engagementLabel").textContent = data.person_present ? data.state : "away";
  document.getElementById("engagementHint").textContent = data.person_present
    ? (data.phone_detected ? "Phone in view." : (data.looking_at_screen ? "Facing the screen." : "Attention seems to be elsewhere."))
    : "Scoring paused while you're away.";

  // Feed tags
  const tagPresence = document.getElementById("tagPresence");
  tagPresence.textContent = data.person_present ? "person detected" : "no person";
  tagPresence.classList.toggle("on", !data.person_present);

  const tagGaze = document.getElementById("tagGaze");
  if (!data.person_present) {
    tagGaze.textContent = "gaze: —";
  } else if (!data.face_present) {
    tagGaze.textContent = "gaze: face not visible";
  } else {
    tagGaze.textContent = "gaze: " + (data.looking_at_screen ? "on screen" : "turned away");
  }

  const tagPhone = document.getElementById("tagPhone");
  tagPhone.textContent = data.phone_detected ? "phone: in view" : "phone: clear";
  tagPhone.classList.toggle("on", !!data.phone_detected);

  // Physical layer - connected device states
  const phys = data.physical || {};
  const relays = phys.relays || {};

  const setDevice = (dotId, stateId, on) => {
    const dot = document.getElementById(dotId);
    const st = document.getElementById(stateId);
    if (dot) dot.classList.toggle("on", !!on);
    if (st) st.textContent = on ? "ON" : "off";
  };
  setDevice("dotLamp", "stateLamp", relays.lamp);
  setDevice("dotFan", "stateFan", relays.fan);
  setDevice("dotAlert", "stateAlert", relays.alert);

  const physHint = document.getElementById("physicalHint");
  if (physHint) {
    physHint.textContent = phys.available
      ? ""
      : "MCU bridge unavailable - check the sketch compiled and required sketch libraries are added.";
  }

  // Raw metrics readout - for tuning posture thresholds against real numbers
  const debugEl = document.getElementById("debugReadout");
  if (debugEl) {
    const d = data.posture_debug || {};
    const lines = [];
    if (d.calibration_progress !== undefined) {
      lines.push("calibrating: " + Math.round(d.calibration_progress * 100) + "%");
    } else {
      if (d.top_drift !== undefined) lines.push("head drop: " + d.top_drift + "  (trigger 0.02)");
      if (d.aspect_change !== undefined) lines.push("slouch/aspect: " + d.aspect_change + "  (trigger 0.06)");
      if (d.height_vs_baseline !== undefined) lines.push("distance: " + d.height_vs_baseline + "  (trigger <0.94)");
      if (d.center_drift !== undefined) lines.push("lean: " + d.center_drift + "  (trigger 0.05)");
      if (d.penalty !== undefined) lines.push("total penalty: " + d.penalty);
    }
    lines.push("person conf: " + (data.person_confidence ?? "—"));
    lines.push("face conf: " + (data.face_confidence ?? "—"));
    debugEl.textContent = lines.join("\n") || "—";
  }

  // Live frame (pushed as base64 JPEG alongside the scores)
  if (data.frame_b64) {
    const feedImg = document.getElementById("feedImg");
    const placeholder = document.getElementById("feedPlaceholder");
    feedImg.src = "data:image/jpeg;base64," + data.frame_b64;
    feedImg.style.display = "block";
    placeholder.style.display = "none";
  }
}

/* Home hero: the same totals renderStatCards already computes, re-presented
 * as one big number plus a proportional stacked bar.
 *
 * The four measures are shown at their real recorded values (tooltips carry
 * the minutes) and normalised against their own sum, rather than being
 * treated as a strict partition of the day: phone_seconds and
 * looking_away_seconds can both land on the same second, which is the same
 * overlap renderStatCards documents above. No new arithmetic on the data. */
function renderHero(focused, distracted, phone, absent) {
  const big = document.getElementById("heroFocusBig");
  if (big) big.innerHTML = bigDuration(focused);

  const bar = document.getElementById("heroBar");
  if (!bar) return;
  bar.innerHTML = "";
  const parts = [
    ["focused", focused],
    ["distracted", distracted],
    ["phone", phone],
    ["absent", absent],
  ];
  const total = parts.reduce((sum, [, secs]) => sum + (secs || 0), 0);
  if (total <= 0) return;
  parts.forEach(([cls, secs]) => {
    if (!secs || secs <= 0) return;
    const seg = document.createElement("div");
    seg.className = "seg " + cls;
    seg.style.width = (secs / total * 100) + "%";
    seg.title = cls + ": " + Math.round(secs / 60) + " min";
    bar.appendChild(seg);
  });
}

function renderStatCards(rows) {
  if (!rows || rows.length === 0) {
    document.getElementById("statFocused").textContent = "--";
    document.getElementById("statDistracted").textContent = "--";
    document.getElementById("statPosture").textContent = "--";
    document.getElementById("statAway").textContent = "--";
    renderHero(0, 0, 0, 0);
    return;
  }

  let engaged = 0, phone = 0, lookingAway = 0, posturePoor = 0, away = 0;
  rows.forEach(r => {
    engaged += r.engaged_seconds || 0;
    phone += r.phone_seconds || 0;
    lookingAway += r.looking_away_seconds || 0;
    posturePoor += r.posture_poor_seconds || 0;
    away += r.away_seconds || 0;
  });
  // "Distraction" combines phone-in-view and looking-away time - both pull
  // engagement down, but phone_seconds and looking_away_seconds can overlap
  // with each other on the same second, so this is a ceiling, not a precise sum.
  const distracted = Math.max(phone, lookingAway);

  document.getElementById("statFocused").textContent = fmtMinutes(engaged);
  document.getElementById("statDistracted").textContent = fmtMinutes(phone);
  document.getElementById("statPosture").textContent = fmtMinutes(posturePoor);
  document.getElementById("statAway").textContent = fmtMinutes(away);

  renderHero(engaged, lookingAway, phone, away);
}

function renderTodayBreakdown(rows) {
  const setText = (id, text) => { const el = document.getElementById(id); if (el) el.textContent = text; };
  const setRow = (id, secs, total) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (total <= 0) {
      el.textContent = "--";
      return;
    }
    const pct = Math.round((secs / total) * 100);
    el.innerHTML = `${Math.round(secs / 60)} min<span class="breakdown-pct">${pct}%</span>`;
  };

  if (!rows || rows.length === 0) {
    setRow("bdEngaged", 0, 0);
    setRow("bdDistracted", 0, 0);
    setRow("bdAway", 0, 0);
    setText("todayAvgPosture", "--");
    setText("todayAvgEngagement", "--");
    setText("todaySession", "--");
    return;
  }

  let engaged = 0, distracted = 0, away = 0;
  let postureWeighted = 0, postureWeight = 0;
  let engagementWeighted = 0, engagementWeight = 0;

  rows.forEach(r => {
    const e = r.engaged_seconds || 0;
    const d = r.distracted_seconds || 0;
    const a = r.away_seconds || 0;
    engaged += e;
    distracted += d;
    away += a;

    // Weight each period's average by its active (person-present) time,
    // so a short-but-different period doesn't skew the day's average as
    // much as a long one - avg_posture/engagement_score are themselves
    // only computed over active samples in that period.
    const activeSeconds = e + d;
    if (activeSeconds > 0 && r.avg_posture_score !== null && r.avg_posture_score !== undefined) {
      postureWeighted += r.avg_posture_score * activeSeconds;
      postureWeight += activeSeconds;
    }
    if (activeSeconds > 0 && r.avg_engagement_score !== null && r.avg_engagement_score !== undefined) {
      engagementWeighted += r.avg_engagement_score * activeSeconds;
      engagementWeight += activeSeconds;
    }
  });

  const total = engaged + distracted + away;
  setRow("bdEngaged", engaged, total);
  setRow("bdDistracted", distracted, total);
  setRow("bdAway", away, total);

  setText("todayAvgPosture", postureWeight > 0 ? String(Math.round(postureWeighted / postureWeight)) : "--");
  setText("todayAvgEngagement", engagementWeight > 0 ? String(Math.round(engagementWeighted / engagementWeight)) : "--");
  setText("todaySession", total > 0 ? Math.round(total / 60) + " min" : "--");
}

function renderTimeline(rows) {
  const bar = document.getElementById("timelineBar");
  const empty = document.getElementById("todayEmpty");
  renderTodayBreakdown(rows);
  if (!rows || rows.length === 0) {
    bar.innerHTML = "";
    empty.style.display = "block";
    renderStatCards(rows);
    return;
  }
  empty.style.display = "none";
  renderStatCards(rows);

  let engaged = 0, distracted = 0, away = 0;
  rows.forEach(r => {
    engaged += r.engaged_seconds || 0;
    distracted += r.distracted_seconds || 0;
    away += r.away_seconds || 0;
  });
  const total = Math.max(1, engaged + distracted + away);

  bar.innerHTML = "";
  [["engaged", engaged], ["distracted", distracted], ["away", away]].forEach(([cls, secs]) => {
    if (secs <= 0) return;
    const seg = document.createElement("div");
    seg.className = "seg " + cls;
    seg.style.width = (secs / total * 100) + "%";
    seg.title = cls + ": " + Math.round(secs / 60) + " min";
    bar.appendChild(seg);
  });
}

function renderTrend(rows) {
  const container = document.getElementById("trendChart");
  const empty = document.getElementById("trendEmpty");
  container.innerHTML = "";

  if (!rows || rows.length === 0) {
    empty.style.display = "block";
    return;
  }
  empty.style.display = "none";

  const width = container.clientWidth || 900;
  const height = 200;
  const padding = 28;
  const n = rows.length;
  const xStep = n > 1 ? (width - padding * 2) / (n - 1) : 0;

  const series = [
    { key: "avg_posture_score", color: "var(--teal)" },
    { key: "avg_engagement_score", color: "var(--amber)" },
  ];

  const yFor = (v) => height - padding - (v / 100) * (height - padding * 2);

  let svg = `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}">`;

  [0, 50, 100].forEach(v => {
    const y = yFor(v);
    svg += `<line x1="${padding}" y1="${y}" x2="${width - padding}" y2="${y}" stroke="var(--line)" stroke-width="1" />`;
    svg += `<text x="4" y="${y + 4}" font-size="10" fill="var(--text-faint)" font-family="var(--font-mono)">${v}</text>`;
  });

  series.forEach(s => {
    let d = "";
    let hasPoints = false;
    rows.forEach((row, i) => {
      const val = row[s.key];
      if (val === null || val === undefined) return; // e.g. avg_posture_score can be null if no scored samples that hour
      const x = padding + i * xStep;
      const y = yFor(val);
      d += (hasPoints ? "L" : "M") + x.toFixed(1) + "," + y.toFixed(1) + " ";
      hasPoints = true;
    });
    if (hasPoints) {
      svg += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />`;
    }
  });

  svg += "</svg>";
  container.innerHTML = svg;
}

function fmtDateShort(isoDate) {
  const d = new Date(isoDate + "T00:00:00");
  if (isNaN(d.getTime())) return isoDate;
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

function renderTrendsDaily(data) {
  const container = document.getElementById("trendsDailyChart");
  const empty = document.getElementById("trendsDailyEmpty");
  const coverage = document.getElementById("trendsCoverage");
  if (!container) return;
  container.innerHTML = "";

  const rows = (data && data.days) || [];

  if (coverage) {
    if (data && data.total_days_recorded > 0 && data.date_range) {
      coverage.textContent = rows.length + " of " + data.total_days_recorded +
        " recorded day(s) shown · " + fmtDateShort(data.date_range.first) +
        " – " + fmtDateShort(data.date_range.last);
    } else {
      coverage.textContent = "No days recorded yet.";
    }
  }

  if (!rows.length) {
    empty.style.display = "block";
    return;
  }
  empty.style.display = "none";

  const width = container.clientWidth || 900;
  const height = 200;
  const padding = 28;
  const bottomPadding = 34;
  const n = rows.length;
  const xStep = n > 1 ? (width - padding * 2) / (n - 1) : 0;
  const plotHeight = height - padding - bottomPadding;

  const series = [
    { key: "avg_posture_score", color: "var(--teal)" },
    { key: "avg_engagement_score", color: "var(--amber)" },
  ];

  const yFor = (v) => height - bottomPadding - (v / 100) * plotHeight;

  let svg = `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}">`;

  [0, 50, 100].forEach(v => {
    const y = yFor(v);
    svg += `<line x1="${padding}" y1="${y}" x2="${width - padding}" y2="${y}" stroke="var(--line)" stroke-width="1" />`;
    svg += `<text x="4" y="${y + 4}" font-size="10" fill="var(--text-faint)" font-family="var(--font-mono)">${v}</text>`;
  });

  // A null average for a day (e.g. every row that day had a null score)
  // breaks the line there rather than interpolating across it or drawing a
  // false zero - that day genuinely has no value for this series.
  series.forEach(s => {
    let d = "";
    let drawing = false;
    rows.forEach((row, i) => {
      const val = row[s.key];
      const x = padding + i * xStep;
      if (val === null || val === undefined) { drawing = false; return; }
      const y = yFor(val);
      d += (drawing ? "L" : "M") + x.toFixed(1) + "," + y.toFixed(1) + " ";
      drawing = true;
    });
    if (d) {
      svg += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />`;
    }
  });

  const axisY = height - bottomPadding;
  svg += `<line x1="${padding}" y1="${axisY}" x2="${width - padding}" y2="${axisY}" stroke="var(--line)" stroke-width="1" />`;
  rows.forEach((row, i) => {
    const x = padding + i * xStep;
    svg += `<text x="${x}" y="${axisY + 16}" font-size="9.5" fill="var(--text-faint)" font-family="var(--font-mono)" text-anchor="middle">${fmtDateShort(row.date)}</text>`;
  });

  svg += `<text x="${width - padding}" y="14" font-size="10" fill="var(--teal)" font-family="var(--font-mono)" text-anchor="end">— posture</text>`;
  svg += `<text x="${width - padding}" y="26" font-size="10" fill="var(--amber)" font-family="var(--font-mono)" text-anchor="end">— engagement</text>`;

  svg += "</svg>";
  container.innerHTML = svg;
}

async function pollTrends() {
  try {
    const res = await authedFetch("/api/trends");
    renderTrendsDaily(await res.json());
  } catch (err) {
    // Leave last-known render in place; nothing to update this cycle.
  }
}

function fmtTimestamp(iso) {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function renderSessionHistory(rows) {
  const container = document.getElementById("sessionHistoryChart");
  const empty = document.getElementById("sessionHistoryEmpty");
  empty.textContent = "No completed sessions recorded yet";
  container.innerHTML = "";

  if (!rows || rows.length === 0) {
    container.style.display = "none";
    empty.style.display = "block";
    return;
  }
  container.style.display = "block";
  empty.style.display = "none";

  const width = container.clientWidth || 900;
  const height = 220;
  const padding = 32;
  const bottomPadding = 46; // room for rotated timestamp labels
  const n = rows.length;
  const xStep = n > 1 ? (width - padding * 2) / (n - 1) : 0;
  const plotHeight = height - padding - bottomPadding;

  const series = [
    { key: "avg_posture_score", label: "posture", color: "var(--teal)" },
    { key: "avg_engagement_score", label: "engagement", color: "var(--amber)" },
  ];

  const yFor = (v) => height - bottomPadding - (v / 100) * plotHeight;

  let svg = `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}">`;

  [0, 50, 100].forEach(v => {
    const y = yFor(v);
    svg += `<line x1="${padding}" y1="${y}" x2="${width - padding}" y2="${y}" stroke="var(--line)" stroke-width="1" />`;
    svg += `<text x="4" y="${y + 4}" font-size="10" fill="var(--text-faint)" font-family="var(--font-mono)">${v}</text>`;
  });

  series.forEach(s => {
    let d = "";
    let hasPoints = false;
    rows.forEach((row, i) => {
      const val = row[s.key];
      if (val === null || val === undefined) return; // e.g. avg_posture_score can be null if no scored samples that period
      const x = padding + i * xStep;
      const y = yFor(val);
      d += (hasPoints ? "L" : "M") + x.toFixed(1) + "," + y.toFixed(1) + " ";
      hasPoints = true;
    });
    if (hasPoints) {
      svg += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />`;
    }
  });

  // X-axis: labels every row's timestamp would collide once there are more
  // than a handful of sessions, so space them out but always include the
  // last (most recent) point.
  const maxLabels = Math.min(n, 6);
  const labelStep = n > 1 ? Math.max(1, Math.round((n - 1) / (maxLabels - 1))) : 1;
  const labelIndexes = new Set();
  for (let i = 0; i < n; i += labelStep) labelIndexes.add(i);
  labelIndexes.add(n - 1);

  const axisY = height - bottomPadding;
  svg += `<line x1="${padding}" y1="${axisY}" x2="${width - padding}" y2="${axisY}" stroke="var(--line)" stroke-width="1" />`;
  labelIndexes.forEach(i => {
    const x = padding + i * xStep;
    const ts = fmtTimestamp(rows[i].period_start);
    svg += `<text x="${x}" y="${axisY + 16}" font-size="9.5" fill="var(--text-faint)" font-family="var(--font-mono)" text-anchor="end" transform="rotate(-30 ${x} ${axisY + 16})">${ts}</text>`;
  });

  svg += `<text x="${width - padding}" y="14" font-size="10" fill="var(--teal)" font-family="var(--font-mono)" text-anchor="end">— posture</text>`;
  svg += `<text x="${width - padding}" y="26" font-size="10" fill="var(--amber)" font-family="var(--font-mono)" text-anchor="end">— engagement</text>`;

  svg += "</svg>";
  container.innerHTML = svg;
}

function setupSessionHistoryButton() {
  const btn = document.getElementById("sessionHistoryBtn");
  const panel = document.getElementById("sessionHistoryPanel");
  const empty = document.getElementById("sessionHistoryEmpty");

  btn.addEventListener("click", async () => {
    btn.disabled = true;
    panel.style.display = "block";
    try {
      const res = await authedFetch("/api/history");
      const rows = await res.json();
      renderSessionHistory(rows);
    } catch (err) {
      document.getElementById("sessionHistoryChart").innerHTML = "";
      document.getElementById("sessionHistoryChart").style.display = "none";
      empty.textContent = "Couldn't load session history - try again in a moment.";
      empty.style.display = "block";
    } finally {
      btn.disabled = false;
    }
  });
}

async function pollHistory() {
  try {
    const [historyRes, todayRes] = await Promise.all([
      authedFetch("/api/history"), authedFetch("/api/today"),
    ]);
    renderTrend(await historyRes.json());
    renderTimeline(await todayRes.json());
  } catch (err) {
    // Leave last-known render in place; the live socket surfaces connectivity issues.
  }
}

async function primeLiveFromRest() {
  // First paint, before any WebSocket message has arrived.
  try {
    const res = await authedFetch("/api/live");
    applyLiveSample(await res.json());
  } catch (err) {
    // Socket connection will populate this shortly.
  }
}

function connectSocket() {
  // `io` comes from the Socket.IO client library - see index.html for how
  // it's loaded (App Lab-bundled if available, CDN fallback otherwise).
  //
  // The Brick can't reject a socket at connect time (its on_connect callback
  // only receives the sid), so authentication is a message sent immediately
  // after connecting. Until the server acks it, this socket is in no push
  // list and receives nothing - no stats, no frames.
  const socket = io();

  socket.on("connect", () => {
    document.getElementById("statusText").textContent = "authenticating…";
    socket.emit("auth", { token: getToken() });
  });

  socket.on("auth_response", (data) => {
    if (data && data.ok) {
      document.getElementById("statusText").textContent = "connected";
    } else {
      document.getElementById("statusPill").className = "status-pill away";
      document.getElementById("statusText").textContent = "not authorised";
      handleSessionLost();
    }
  });

  socket.on("disconnect", () => {
    document.getElementById("statusPill").className = "status-pill away";
    document.getElementById("statusText").textContent = "disconnected";
  });

  socket.on("live_sample", (data) => applyLiveSample(data));
  socket.on("pomodoro", (data) => applyPomodoro(data));
  return socket;
}

function setupSummaryButton() {
  const btn = document.getElementById("summaryBtn");
  const result = document.getElementById("summaryResult");

  btn.addEventListener("click", async () => {
    btn.disabled = true;
    btn.textContent = "Generating… this can take up to 2 minutes on-device";
    result.style.display = "block";
    result.textContent = "Thinking it over…";

    try {
      const res = await authedFetch("/api/generate_summary", { method: "POST" });
      const data = await res.json();
      if (data.status === "ok") {
        result.textContent = data.summary;
      } else {
        result.textContent = data.message || "Couldn't generate a summary right now.";
      }
    } catch (err) {
      result.textContent = "Couldn't reach the device - try again in a moment.";
    } finally {
      btn.disabled = false;
      btn.textContent = "Generate my daily summary";
    }
  });
}

/* ---- Focus tab: pomodoro ----
 * Pure display + control. Python owns the state machine; this renders the
 * once-a-second "pomodoro" socket push and POSTs commands back. The settings
 * inputs are only refilled while they're not being edited, so a push landing
 * mid-typing doesn't yank the value out from under you.
 */
let pomoSettingsTouched = false;

function pomoFmtTime(seconds) {
  const s = Math.max(0, Math.round(seconds || 0));
  const mm = Math.floor(s / 60);
  const ss = s % 60;
  return String(mm).padStart(2, "0") + ":" + String(ss).padStart(2, "0");
}

const POMO_PHASE_TEXT = {
  idle: "Idle",
  focus: "Focus",
  short_break: "Short break",
  long_break: "Long break",
};

function applyPomodoro(data) {
  if (!data) return;

  const isBreak = data.phase === "short_break" || data.phase === "long_break";
  const isFocus = data.phase === "focus";

  const timeEl = document.getElementById("pomoTime");
  if (timeEl) timeEl.textContent = data.phase === "idle" ? "--:--" : pomoFmtTime(data.remaining_s);

  const subEl = document.getElementById("pomoSub");
  if (subEl) {
    if (data.phase === "idle") subEl.textContent = "not started";
    else if (data.state === "paused") subEl.textContent = "paused";
    else subEl.textContent = "of " + Math.round((data.total_s || 0) / 60) + " min";
  }

  // Ring sweep = elapsed share of the phase.
  const ring = document.getElementById("pomoRing");
  if (ring) {
    const total = data.total_s || 0;
    const pct = total > 0 ? ((total - data.remaining_s) / total) * 100 : 0;
    ring.style.setProperty("--pomo-pct", Math.max(0, Math.min(100, pct)).toFixed(2));
    ring.style.setProperty("--pomo-color", isBreak ? "var(--absent)" : "var(--focused)");
  }

  const pill = document.getElementById("pomoPhasePill");
  const pillText = document.getElementById("pomoPhaseText");
  if (pill) {
    pill.className = "status-pill" + (isFocus ? " pomo-focus" : (isBreak ? " pomo-break" : ""));
  }
  if (pillText) {
    const base = POMO_PHASE_TEXT[data.phase] || "Idle";
    pillText.textContent = data.state === "paused" ? base + " · paused" : base;
  }

  const startBtn = document.getElementById("pomoStartBtn");
  if (startBtn) startBtn.textContent = data.running ? "Pause" : "Start";

  const completed = document.getElementById("pomoCompleted");
  if (completed) completed.textContent = data.completed_today ?? 0;

  const quality = document.getElementById("pomoQuality");
  if (quality) {
    quality.textContent = (data.last_quality_pct === null || data.last_quality_pct === undefined)
      ? "--" : data.last_quality_pct + "%";
  }

  const st = data.settings || {};
  const hint = document.getElementById("pomoCycleHint");
  if (hint && st.sessions_before_long_break) {
    hint.textContent = "Long break after every " + st.sessions_before_long_break +
      " sessions · " + (data.cycle_count || 0) + " done this run.";
  }

  if (!pomoSettingsTouched) {
    const set = (id, value) => {
      const el = document.getElementById(id);
      if (el && value !== undefined && value !== null) el.value = value;
    };
    set("pomoFocusMin", st.focus_minutes);
    set("pomoShortMin", st.short_break_minutes);
    set("pomoLongMin", st.long_break_minutes);
    set("pomoSessionsBeforeLong", st.sessions_before_long_break);
    const auto = document.getElementById("pomoAutoStart");
    if (auto && st.auto_start_next !== undefined) auto.checked = !!st.auto_start_next;
  }
}

async function pomoCommand(path) {
  try {
    const res = await authedFetch("/api/pomodoro/" + path, { method: "POST" });
    if (res.ok) applyPomodoro(await res.json());
  } catch (err) {
    // authedFetch already handles a dead session; nothing else to do.
  }
}

function setupPomodoro() {
  const startBtn = document.getElementById("pomoStartBtn");
  if (!startBtn) return;

  // Start doubles as Pause - the server decides which, so a stale label
  // can't put the two sides out of step.
  startBtn.addEventListener("click", () => {
    pomoCommand(startBtn.textContent === "Pause" ? "pause" : "start");
  });
  document.getElementById("pomoSkipBtn").addEventListener("click", () => pomoCommand("skip"));
  document.getElementById("pomoResetBtn").addEventListener("click", () => pomoCommand("reset"));

  const form = document.getElementById("pomoSettingsForm");
  form.querySelectorAll("input").forEach((input) => {
    input.addEventListener("focus", () => { pomoSettingsTouched = true; });
  });

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const err = document.getElementById("pomoSettingsError");
    const ok = document.getElementById("pomoSettingsOk");
    const btn = document.getElementById("pomoSettingsSubmit");
    err.hidden = true;
    ok.hidden = true;
    btn.disabled = true;

    const body = {
      focus_minutes: Number(document.getElementById("pomoFocusMin").value),
      short_break_minutes: Number(document.getElementById("pomoShortMin").value),
      long_break_minutes: Number(document.getElementById("pomoLongMin").value),
      sessions_before_long_break: Number(document.getElementById("pomoSessionsBeforeLong").value),
      auto_start_next: document.getElementById("pomoAutoStart").checked,
    };

    try {
      const res = await authedFetch("/api/pomodoro/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) return showError(err, await errorDetail(res, "Couldn't save settings."));
      pomoSettingsTouched = false;
      applyPomodoro(await res.json());
      ok.hidden = false;
    } catch (e) {
      if (e.message !== "unauthenticated") {
        showError(err, "Couldn't reach the device - try again in a moment.");
      }
    } finally {
      btn.disabled = false;
    }
  });
}

async function primePomodoro() {
  try {
    const res = await authedFetch("/api/pomodoro");
    if (res.ok) applyPomodoro(await res.json());
  } catch (err) {
    // The socket push will populate this shortly.
  }
}

/* ---- Auth gate + bootstrap ----
 * Nothing below talks to the API until a token is in hand. startDashboard()
 * is the old bootstrap sequence, unchanged apart from being callable; the
 * timers it owns are tracked so signing out can stop them instead of leaving
 * them hammering a dead session.
 */
let dashboardTimers = [];
let dashboardSocket = null;
let dashboardStarted = false;

function startDashboard() {
  if (dashboardStarted) return;
  dashboardStarted = true;

  document.getElementById("authGate").hidden = true;
  document.getElementById("appShell").hidden = false;

  primeLiveFromRest();
  pollHistory();
  pollTrends();
  primePomodoro();
  dashboardSocket = connectSocket();
  dashboardTimers.push(setInterval(pollHistory, HISTORY_POLL_MS));
  dashboardTimers.push(setInterval(pollTrends, HISTORY_POLL_MS));
}

function stopDashboard() {
  dashboardStarted = false;
  dashboardTimers.forEach(clearInterval);
  dashboardTimers = [];
  if (dashboardSocket) {
    dashboardSocket.disconnect();
    dashboardSocket = null;
  }
  document.getElementById("appShell").hidden = true;
  document.getElementById("authGate").hidden = false;
}

function showGate(which) {
  document.getElementById("authGate").hidden = false;
  document.getElementById("appShell").hidden = true;
  document.getElementById("setupForm").hidden = which !== "setup";
  document.getElementById("loginForm").hidden = which !== "login";
  if (which === "login") document.getElementById("loginPassword").focus();
  else document.getElementById("setupPassword").focus();
}

// Called from authedFetch on a 401 and from a rejected socket handshake.
function handleSessionLost() {
  clearToken();
  stopDashboard();
  showGate("login");
}

function setupAuthForms() {
  const setupForm = document.getElementById("setupForm");
  const loginForm = document.getElementById("loginForm");
  const changeForm = document.getElementById("changeForm");

  setupForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const err = document.getElementById("setupError");
    const btn = document.getElementById("setupSubmit");
    const pw = document.getElementById("setupPassword").value;
    const confirm = document.getElementById("setupConfirm").value;
    err.hidden = true;

    if (pw !== confirm) return showError(err, "The two passwords don't match.");
    btn.disabled = true;
    try {
      const res = await postJson("/api/auth/setup", { password: pw });
      if (!res.ok) return showError(err, await errorDetail(res, "Couldn't set the password."));
      const data = await res.json();
      setToken(data.token);
      setupForm.reset();
      startDashboard();
    } catch (e) {
      showError(err, "Couldn't reach the device - try again in a moment.");
    } finally {
      btn.disabled = false;
    }
  });

  loginForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const err = document.getElementById("loginError");
    const btn = document.getElementById("loginSubmit");
    const pw = document.getElementById("loginPassword").value;
    err.hidden = true;
    btn.disabled = true;
    try {
      const res = await postJson("/api/auth/login", { password: pw });
      if (!res.ok) {
        // 429 carries the lockout message from the server verbatim.
        return showError(err, await errorDetail(res, "Couldn't sign in."));
      }
      const data = await res.json();
      setToken(data.token);
      loginForm.reset();
      startDashboard();
    } catch (e) {
      showError(err, "Couldn't reach the device - try again in a moment.");
    } finally {
      btn.disabled = false;
    }
  });

  changeForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const err = document.getElementById("changeError");
    const ok = document.getElementById("changeOk");
    const btn = document.getElementById("changeSubmit");
    const current = document.getElementById("currentPassword").value;
    const next = document.getElementById("newPassword").value;
    const confirm = document.getElementById("newConfirm").value;
    err.hidden = true;
    ok.hidden = true;

    if (next !== confirm) return showError(err, "The two new passwords don't match.");
    btn.disabled = true;
    try {
      const res = await authedFetch("/api/auth/change_password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ old_password: current, new_password: next }),
      });
      if (!res.ok) return showError(err, await errorDetail(res, "Couldn't change the password."));
      changeForm.reset();
      ok.hidden = false;
    } catch (e) {
      if (e.message !== "unauthenticated") {
        showError(err, "Couldn't reach the device - try again in a moment.");
      }
    } finally {
      btn.disabled = false;
    }
  });

  // Two entry points, one handler: the header icon (on every tab) and the
  // button on the Settings tab.
  const doLogout = async () => {
    try {
      await authedFetch("/api/auth/logout", { method: "POST" });
    } catch (e) {
      // Already unauthenticated server-side; clearing locally is enough.
    }
    handleSessionLost();
  };
  ["logoutBtn", "logoutIcon"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("click", doLogout);
  });
}

async function boot() {
  // Theme first, so the login card is already in the right palette.
  setupTheme();
  applyTimeOfDayTheme();
  setupTabs();
  setupRangePills();
  setupSummaryButton();
  setupSessionHistoryButton();
  setupPomodoro();
  setupAuthForms();
  setInterval(applyTimeOfDayTheme, 5 * 60 * 1000); // re-check every 5 min in case the tab stays open across a boundary

  let passwordSet = true;
  try {
    const res = await fetch("/api/auth/status");
    const data = await res.json();
    passwordSet = !!data.password_set;
  } catch (err) {
    // Can't reach the board - show login; it'll surface the error on submit.
  }

  if (!passwordSet) return showGate("setup");
  if (!getToken()) return showGate("login");

  // We have a token, but it may be expired or revoked. Probe one cheap gated
  // route: authedFetch sends us to the login screen if it's no longer valid.
  try {
    await authedFetch("/api/live");
    startDashboard();
  } catch (err) {
    if (err.message !== "unauthenticated") showGate("login");
  }
}

boot();
