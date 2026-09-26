const MIN_DB = -90;
const VOLUME_SEND_INTERVAL_MS = 250;
const RECONNECT_DELAY_MS = 3000;

let snap = null;
let reconnecting = false;
let dismissedError = null;
let localError = null; // { key, message }
let drawerOpen = false;
let drawerInputs = null;
let playStopPending = false;
let autoOnPending = false;

const speakerNodes = new Map();
const missingNodes = new Map();
const dragging = new Map();
let speakerOrder = null;
let lastDefaultSpeakers = null;

function sameSet(a, b) {
  if (a.size !== b.size) return false;
  for (const x of a) if (!b.has(x)) return false;
  return true;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v === true) node.setAttribute(k, "");
    else if (v !== false && v != null) node.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c) node.append(c);
  return node;
}

function positionAfter(parent, node, afterNode) {
  if (afterNode.nextSibling === node) return;
  if (afterNode.nextSibling) parent.insertBefore(node, afterNode.nextSibling);
  else parent.append(node);
}

function sortInitialOrder(speakers) {
  return speakers
    .slice()
    .sort((a, b) => {
      if (a.is_default !== b.is_default) return a.is_default ? -1 : 1;
      return a.name.toLowerCase().localeCompare(b.name.toLowerCase());
    })
    .map(sp => sp.id);
}

function statusText(s) {
  if (!s.owntone_up) return "OwnTone unavailable";
  if (!s.input_available) return "Turntable not connected";
  if (s.state === "playing") return s.countdown_s !== null ? `Stopping in ${formatCountdown(s.countdown_s)}` : "Playing";
  if (s.state === "held") return "Held until this record ends";
  if (s.state === "off") return "Auto-on off";
  return "Waiting for a record";
}

function formatCountdown(seconds) {
  const m = Math.floor(seconds / 60);
  const s = String(seconds % 60).padStart(2, "0");
  return `${m}:${s}`;
}

function pct(db) {
  return Math.max(0, Math.min(100, (db - MIN_DB) / -MIN_DB * 100));
}

function names(ids) {
  const list = ids.map(id => (snap.speakers.find(sp => sp.id === id) || {}).name).filter(Boolean);
  if (list.length === 0) return "";
  if (list.length <= 2) return list.join(" and ");
  return `${list.slice(0, -1).join(", ")} and ${list[list.length - 1]}`;
}

function describe(s) {
  if (!s.owntone_up) {
    return { lamp: "bad", eyebrow: "OwnTone", body: "Tonearm keeps listening to the turntable and starts playback as soon as OwnTone answers again." };
  }
  if (!s.input_available) {
    return { lamp: "bad", eyebrow: "Turntable", body: "Check the USB cable and that the turntable is switched on." };
  }
  if (s.state === "playing" && s.countdown_s !== null) {
    return { lamp: "on", eyebrow: "Tonearm up", body: "Flip the record or drop the needle to keep playing." };
  }
  if (s.state === "playing") {
    const playingTo = s.speakers.filter(x => x.selected).map(x => x.id);
    const to = names(playingTo);
    return { lamp: "on", eyebrow: "Now spinning", body: to ? `On ${to}.` : "" };
  }
  if (s.state === "held") {
    return { lamp: "", eyebrow: "Stopped by you", body: "Auto-on comes back after the tonearm lifts. Press Play to resume now." };
  }
  if (s.state === "off") {
    return { lamp: "", eyebrow: "Auto-on off", body: "Dropping the needle does nothing. Press Play to start by hand." };
  }
  const defaults = s.default_speakers.filter(id => !s.missing_default_speakers.includes(id));
  const to = names(defaults);
  return {
    lamp: "", eyebrow: "Armed",
    body: to ? `Drop the needle to play on ${to}.` : "No default speakers — star a speaker to play it when the needle drops.",
  };
}

const PLAY_ICON = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 1.5v13l11-6.5z"/></svg>';
const STOP_ICON = '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="2.5" y="2.5" width="11" height="11" rx="1.5"/></svg>';
const STAR_ICON = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 2.2l2.4 5 5.4.6-4 3.7 1.1 5.3L10 14.1l-4.9 2.7 1.1-5.3-4-3.7 5.4-.6z"/></svg>';
const GEAR_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19.14 12.94a7.07 7.07 0 0 0 .06-.94 7.07 7.07 0 0 0-.06-.94l2.03-1.58a.49.49 0 0 0 .12-.62l-1.92-3.32a.49.49 0 0 0-.59-.22l-2.39.96a7 7 0 0 0-1.62-.94l-.36-2.54a.48.48 0 0 0-.48-.4h-3.84a.48.48 0 0 0-.48.4l-.36 2.54a7 7 0 0 0-1.62.94l-2.39-.96a.49.49 0 0 0-.59.22L2.71 8.86a.48.48 0 0 0 .12.62l2.03 1.58a7.3 7.3 0 0 0 0 1.88l-2.03 1.58a.49.49 0 0 0-.12.62l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.04.7 1.62.94l.36 2.54c.05.23.24.4.48.4h3.84c.24 0 .44-.17.48-.4l.36-2.54a7 7 0 0 0 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32a.49.49 0 0 0-.12-.62zM12 15.6A3.6 3.6 0 1 1 12 8.4a3.6 3.6 0 0 1 0 7.2z"/></svg>';

async function apiCall(path, method, body) {
  const key = `${method} ${path}`;
  try {
    const opts = { method };
    if (body !== undefined) {
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    if (!res.ok) {
      let message = `${method} ${path} failed`;
      try {
        const data = await res.json();
        if (data && data.error) message = data.error;
      } catch {}
      localError = { key, message };
      updateBanner();
    } else if (localError && localError.key === key) {
      localError = null;
      updateBanner();
    }
    return res;
  } catch (e) {
    localError = { key, message: `Network error: ${e.message}` };
    updateBanner();
    return null;
  }
}

function putSpeaker(id, fields) {
  return apiCall(`/api/speakers/${encodeURIComponent(id)}`, "PUT", fields);
}

const togglingDefault = new Set();

function toggleDefault(id) {
  if (togglingDefault.has(id)) return;
  togglingDefault.add(id);
  const list = snap.default_speakers.slice();
  const idx = list.indexOf(id);
  const adding = idx < 0;
  if (idx >= 0) list.splice(idx, 1);
  else list.push(id);
  const sp = snap.speakers.find(x => x.id === id);
  const shouldTurnOn = adding && sp && sp.available && !sp.selected && !sp.needs_pin;
  return apiCall("/api/default-speakers", "PUT", { ids: list }).then(res => {
    if (shouldTurnOn && res && res.ok) return putSpeaker(id, { selected: true });
    return res;
  }).finally(() => togglingDefault.delete(id));
}

async function onPlayStop() {
  if (playStopPending || !snap) return;
  playStopPending = true;
  playBtnEl.disabled = true;
  try {
    if (snap.state === "playing") {
      await apiCall("/api/stop", "POST");
    } else {
      const ids = snap.speakers.filter(sp => sp.selected).map(sp => sp.id);
      await apiCall("/api/play", "POST", ids.length ? { speakers: ids } : undefined);
    }
  } finally {
    playStopPending = false;
    playBtnEl.disabled = false;
  }
}

async function onAutoOnChange() {
  if (autoOnPending) return;
  const desired = autoOnEl.checked;
  autoOnPending = true;
  autoOnEl.disabled = true;
  try {
    await apiCall("/api/auto-on", "PUT", { enabled: desired });
  } finally {
    autoOnPending = false;
    autoOnEl.disabled = false;
  }
}

function createSpeakerCard(id) {
  const toggle = el("input", { type: "checkbox", role: "switch" });
  toggle.addEventListener("change", () => putSpeaker(id, { selected: toggle.checked }));

  const star = el("button", { class: "star", type: "button" });
  star.innerHTML = STAR_ICON;
  star.addEventListener("click", () => toggleDefault(id));

  const vol = el("input", { type: "range", min: "0", max: "100" });
  const out = el("output");
  let lastSentAt = -Infinity;
  let lastSentValue = null;
  let dragTimer = null;
  let dragEndPending = false;

  const sendVolumeThrottled = () => {
    lastSentAt = Date.now();
    lastSentValue = Number(vol.value);
    putSpeaker(id, { volume: lastSentValue });
  };
  const onVolInput = () => {
    dragging.set(id, Number(vol.value));
    out.textContent = vol.value;
    const wait = VOLUME_SEND_INTERVAL_MS - (Date.now() - lastSentAt);
    if (wait <= 0) sendVolumeThrottled();
    else if (!dragTimer) dragTimer = setTimeout(() => { dragTimer = null; sendVolumeThrottled(); }, wait);
  };
  const endDrag = () => {
    if (dragTimer) { clearTimeout(dragTimer); dragTimer = null; }
    const value = Number(vol.value);
    if (value === lastSentValue) {
      if (!dragEndPending) dragging.delete(id);
      return;
    }
    lastSentAt = Date.now();
    lastSentValue = value;
    dragEndPending = true;
    putSpeaker(id, { volume: value }).finally(() => {
      dragEndPending = false;
      dragging.delete(id);
    });
  };
  vol.addEventListener("input", onVolInput);
  vol.addEventListener("change", endDrag);
  vol.addEventListener("pointerup", endDrag);
  vol.addEventListener("pointercancel", endDrag);
  vol.addEventListener("blur", endDrag);

  const rank = el("span", { class: "rank", "aria-hidden": "true" });
  const nameB = el("b");
  const nameSmall = el("small");
  const pinContainer = el("div");

  const card = el("article", { class: "spk" },
    el("div", { class: "spk-top" },
      rank,
      el("div", { class: "spk-name" }, nameB, nameSmall),
      star,
      el("span", { class: "switch" }, toggle, el("span"))),
    el("div", { class: "vol" }, vol, out),
    pinContainer);

  const nodes = { card, toggle, star, vol, out, rank, nameB, nameSmall, pinContainer, pinForm: null };
  speakerNodes.set(id, nodes);
  return nodes;
}

function updateSpeakerCard(sp) {
  const nodes = speakerNodes.get(sp.id) || createSpeakerCard(sp.id);
  const unavailable = sp.available === false;

  nodes.card.className = `spk ${sp.selected ? "on" : "off"}${unavailable ? " missing" : ""}`;
  nodes.nameB.textContent = sp.name;
  nodes.nameSmall.textContent = unavailable ? "Unavailable" : sp.type + (sp.is_default ? " · default" : "");

  nodes.toggle.checked = sp.selected;
  nodes.toggle.id = `on-${sp.id}`;
  nodes.toggle.setAttribute("aria-label", `Play on ${sp.name}`);
  nodes.toggle.disabled = unavailable;

  nodes.star.setAttribute("aria-pressed", String(sp.is_default));
  nodes.star.setAttribute("aria-label", `${sp.name} is a default speaker`);
  nodes.star.disabled = unavailable;

  nodes.vol.id = `vol-${sp.id}`;
  nodes.vol.setAttribute("aria-label", `${sp.name} volume`);
  nodes.out.setAttribute("for", `vol-${sp.id}`);
  nodes.vol.disabled = unavailable;
  if (!dragging.has(sp.id)) {
    nodes.vol.value = String(sp.volume);
    nodes.out.textContent = String(sp.volume);
  }

  if (sp.needs_pin) {
    if (!nodes.pinForm) {
      const input = el("input", {
        inputmode: "numeric", pattern: "[0-9]{4}", maxlength: "4", placeholder: "0000",
        required: true, autocomplete: "one-time-code",
        "aria-label": `PIN for ${sp.name}`,
      });
      const form = el("form", {
        onsubmit: (e) => { e.preventDefault(); putSpeaker(sp.id, { pin: input.value }); },
      }, input, el("button", { type: "submit", text: "Pair" }));
      nodes.pinContainer.className = "pin";
      nodes.pinContainer.append(
        el("p", { text: "Enter the PIN shown on the device" }),
        form);
      nodes.pinForm = form;
    }
  } else if (nodes.pinForm) {
    nodes.pinContainer.className = "";
    nodes.pinContainer.replaceChildren();
    nodes.pinForm = null;
  }

  return nodes.card;
}

function updateMissingCard(id) {
  let node = missingNodes.get(id);
  if (!node) {
    const star = el("button", {
      class: "star", type: "button", "aria-pressed": "true",
      "aria-label": "Remove from default speakers",
    });
    star.innerHTML = STAR_ICON;
    star.addEventListener("click", () => toggleDefault(id));
    node = el("article", { class: "spk missing" },
      el("div", { class: "spk-top" },
        el("div", { class: "spk-name" }, el("b", { text: "Default speaker offline" }), el("small", { text: id })),
        star));
    missingNodes.set(id, node);
  }
  return node;
}

let speakersSection, speakersHeadEl, speakersCountEl, speakersNoticeEl;

function buildSpeakersSkeleton() {
  speakersCountEl = el("span");
  speakersHeadEl = el("div", { class: "speakers-head" }, el("h2", { text: "The Line-up" }), speakersCountEl);
  speakersSection = el("section", { class: "speakers", "aria-label": "Speakers" }, speakersHeadEl);
  return speakersSection;
}

function updateSpeakers(s) {
  const hasSpeakers = s.owntone_up && s.speakers.length > 0;
  const availableSpeakers = s.speakers.filter(x => x.available !== false);
  speakersCountEl.textContent = hasSpeakers
    ? `${availableSpeakers.filter(x => x.selected).length} of ${availableSpeakers.length} on · ★ plays on needle drop`
    : "";

  const noticeText = !s.owntone_up
    ? "Speakers appear here when OwnTone is reachable."
    : (s.speakers.length === 0 ? "No speakers found on the network." : null);

  if (noticeText) {
    if (!speakersNoticeEl) {
      speakersNoticeEl = el("div", { class: "notice" });
      speakersSection.append(speakersNoticeEl);
    }
    speakersNoticeEl.textContent = noticeText;
  } else if (speakersNoticeEl) {
    speakersNoticeEl.remove();
    speakersNoticeEl = null;
  }

  if (!s.owntone_up) {
    for (const [id, nodes] of speakerNodes) { nodes.card.remove(); speakerNodes.delete(id); dragging.delete(id); }
    for (const [id, node] of missingNodes) { node.remove(); missingNodes.delete(id); }
    return;
  }

  const idSet = new Set(s.speakers.map(sp => sp.id));
  for (const [id, nodes] of speakerNodes) {
    if (!idSet.has(id)) { nodes.card.remove(); speakerNodes.delete(id); dragging.delete(id); }
  }

  const defaultSet = new Set(s.default_speakers);
  const defaultSetChanged = lastDefaultSpeakers !== null && !sameSet(defaultSet, lastDefaultSpeakers);
  lastDefaultSpeakers = defaultSet;

  if (!speakerOrder || defaultSetChanged) {
    speakerOrder = sortInitialOrder(s.speakers);
  } else {
    speakerOrder = speakerOrder.filter(id => idSet.has(id));
    for (const sp of s.speakers) {
      if (!speakerOrder.includes(sp.id)) speakerOrder.push(sp.id);
    }
  }

  const byId = new Map(s.speakers.map(sp => [sp.id, sp]));
  let prevNode = speakersHeadEl;
  for (const [i, id] of speakerOrder.entries()) {
    const sp = byId.get(id);
    const cardEl = updateSpeakerCard(sp);
    const rankText = `#${i + 1}`;
    const rankEl = speakerNodes.get(id).rank;
    if (rankEl.textContent !== rankText) rankEl.textContent = rankText;
    positionAfter(speakersSection, cardEl, prevNode);
    prevNode = cardEl;
  }
  for (const id of s.missing_default_speakers) {
    const cardEl = updateMissingCard(id);
    positionAfter(speakersSection, cardEl, prevNode);
    prevNode = cardEl;
  }

  const missingSet = new Set(s.missing_default_speakers);
  for (const [id, node] of missingNodes) {
    if (!missingSet.has(id)) { node.remove(); missingNodes.delete(id); }
  }
}

let lampEl, eyebrowTextNode, h1El, bodyEl, meterFillEl, meterReadoutEl, meterTrackEl, playBtnEl, autoOnEl;
let lastPlayBtnPlaying = null;

function buildDeckSkeleton() {
  lampEl = el("span", { class: "lamp" });
  eyebrowTextNode = document.createTextNode("");
  const eyebrow = el("div", { class: "eyebrow" }, lampEl, eyebrowTextNode);
  h1El = el("h1");
  bodyEl = el("p");
  const status = el("div", { class: "status", "aria-live": "polite" }, eyebrow, h1El, bodyEl);

  meterFillEl = el("div", { class: "meter-fill" });
  meterReadoutEl = el("span", { class: "readout" });
  meterTrackEl = el("div", {
    class: "meter-track", role: "meter", "aria-label": "Input level",
    "aria-valuemin": MIN_DB, "aria-valuemax": 0,
  }, meterFillEl);
  const meterHead = el("div", { class: "meter-head" }, el("span", { class: "eyebrow", text: "Input level" }), meterReadoutEl);
  const scale = el("div", { class: "meter-scale", "aria-hidden": "true" },
    el("span", { class: "edge-l", style: "left:0", text: "−90" }),
    el("span", { style: `left:${pct(-40)}%`, text: "−40" }),
    el("span", { style: `left:${pct(-20)}%`, text: "−20" }),
    el("span", { class: "edge-r", style: "left:100%", text: "0 dBFS" }));
  const meterEl = el("div", { class: "meter" }, meterHead, meterTrackEl, scale);

  playBtnEl = el("button", { class: "play", type: "button", onclick: onPlayStop });
  playBtnEl.innerHTML = PLAY_ICON + "Play";

  autoOnEl = el("input", { type: "checkbox", id: "autoOn", role: "switch" });
  autoOnEl.addEventListener("change", onAutoOnChange);

  const transport = el("div", { class: "transport" },
    playBtnEl,
    el("div", { class: "row" },
      el("label", { for: "autoOn" }, "Auto-on", el("span", { class: "hint", text: "Play when the needle drops" })),
      el("span", { class: "switch" }, autoOnEl, el("span"))));

  return el("section", { class: "deck", "aria-label": "Turntable" }, status, meterEl, transport);
}

function updateDeck(s) {
  const d = describe(s);
  lampEl.className = `lamp ${d.lamp}`;
  eyebrowTextNode.textContent = d.eyebrow;
  h1El.textContent = reconnecting ? "Reconnecting…" : statusText(s);
  bodyEl.textContent = d.body;

  const level = s.input_available ? `${s.level_db.toFixed(1)} dBFS` : "no signal";
  meterReadoutEl.textContent = level;
  meterFillEl.style.width = `${s.input_available ? pct(s.level_db) : 0}%`;
  meterTrackEl.setAttribute("aria-valuenow", String(Math.max(MIN_DB, Math.min(0, s.level_db))));

  const playing = s.state === "playing";
  if (playing !== lastPlayBtnPlaying) {
    playBtnEl.className = playing ? "play stop" : "play";
    playBtnEl.innerHTML = playing ? STOP_ICON + "Stop" : PLAY_ICON + "Play";
    lastPlayBtnPlaying = playing;
  }
  playBtnEl.disabled = playStopPending;

  if (!autoOnPending) autoOnEl.checked = s.auto_on;
  autoOnEl.disabled = autoOnPending;
}

let appEl, deckEl, bannerEl, bannerTextEl, bannerShownText = null;

function onDismissBanner() {
  if (localError) localError = null;
  else if (snap) dismissedError = snap.error;
  updateBanner();
}

function updateBanner() {
  const text = !snap ? null : (localError ? localError.message : (snap.error && snap.error !== dismissedError ? snap.error : null));
  if (text == null) {
    if (bannerEl) { bannerEl.remove(); bannerEl = null; bannerShownText = null; }
    return;
  }
  if (!bannerEl) {
    bannerTextEl = el("span");
    bannerEl = el("div", { class: "banner", role: "alert" },
      el("div", {}, el("b", { text: "Problem. " }), bannerTextEl),
      el("button", { type: "button", "aria-label": "Dismiss", text: "✕", onclick: onDismissBanner }));
    appEl.insertBefore(bannerEl, deckEl);
  }
  if (text !== bannerShownText) {
    bannerTextEl.textContent = text;
    bannerShownText = text;
  }
}

let gearBtn, drawerScrimEl, selectEl, closeBtn, drawerRoot;

function buildDrawerSkeleton() {
  selectEl = el("select", { id: "inputSelect" });
  selectEl.addEventListener("change", () => apiCall("/api/input", "PUT", { id: selectEl.value }));
  closeBtn = el("button", { class: "icon-btn", type: "button", onclick: closeDrawer, text: "Close" });
  drawerScrimEl = el("div", {
    class: "drawer-scrim",
    onclick: (e) => { if (e.target === e.currentTarget) closeDrawer(); },
  },
    el("aside", { class: "drawer", role: "dialog", "aria-modal": "true", "aria-label": "Settings", tabindex: "-1" },
      el("div", { class: "row" }, el("h2", { text: "Settings" }), closeBtn),
      el("div", { class: "field" },
        el("label", { for: "inputSelect", text: "Turntable input" }),
        selectEl,
        el("p", { text: "The USB audio device the turntable is plugged into." }))));
}

function onDrawerKeydown(e) {
  if (e.key === "Escape") { e.preventDefault(); closeDrawer(); }
}

function populateDrawerOptions(inputs) {
  drawerInputs = inputs;
  const currentInput = snap ? snap.input : null;
  selectEl.replaceChildren();
  const currentKnown = inputs.some(i => i.id === currentInput);
  if (!currentKnown) {
    selectEl.append(el("option", { value: "", text: "Choose input…", disabled: true, selected: true }));
  }
  for (const input of inputs) {
    const shared = inputs.filter(i => i.name === input.name).length > 1;
    const text = shared ? `${input.name} (${input.id})` : input.name;
    selectEl.append(el("option", { value: input.id, text, selected: input.id === currentInput }));
  }
}

function updateDrawerSelection() {
  if (!drawerOpen || !drawerInputs) return;
  const currentInput = snap ? snap.input : null;
  for (const opt of selectEl.options) {
    opt.selected = opt.value === currentInput && opt.value !== "";
  }
}

async function loadDrawerInputs() {
  const res = await apiCall("/api/inputs", "GET");
  let inputs = [];
  if (res && res.ok) {
    const data = await res.json();
    inputs = data.inputs || [];
  }
  populateDrawerOptions(inputs);
  if (drawerOpen && document.activeElement === closeBtn && inputs.length) {
    selectEl.focus();
  }
}

function openDrawer() {
  if (drawerOpen) return;
  drawerOpen = true;
  appEl.setAttribute("inert", "");
  drawerRoot.append(drawerScrimEl);
  document.addEventListener("keydown", onDrawerKeydown);
  (selectEl.options.length ? selectEl : closeBtn).focus();
  loadDrawerInputs();
}

function closeDrawer() {
  if (!drawerOpen) return;
  drawerOpen = false;
  drawerScrimEl.remove();
  appEl.removeAttribute("inert");
  document.removeEventListener("keydown", onDrawerKeydown);
  gearBtn.focus();
}

function buildSkeleton() {
  appEl = document.getElementById("app");
  drawerRoot = document.getElementById("drawerRoot");

  gearBtn = el("button", {
    class: "icon-btn gear", type: "button", onclick: openDrawer,
    "aria-label": "Settings", title: "Settings",
  });
  gearBtn.innerHTML = GEAR_ICON;
  const header = el("header", { class: "brand" },
    el("div", { class: "wordmark" },
      el("span", { class: "name", text: "Tonearm" }),
      el("span", { class: "tagline", text: "★ Top of the pops, every room ★" })),
    gearBtn);

  deckEl = buildDeckSkeleton();
  const speakers = buildSpeakersSkeleton();

  appEl.append(header, deckEl, speakers);
  buildDrawerSkeleton();
}

function update() {
  if (!snap) {
    h1El.textContent = reconnecting ? "Reconnecting…" : "Connecting…";
    playBtnEl.disabled = true;
    autoOnEl.disabled = true;
    return;
  }
  updateDeck(snap);
  updateBanner();
  updateSpeakers(snap);
  if (drawerOpen) updateDrawerSelection();
}

let es = null;
let reconnectTimer = null;

function connect() {
  es = new EventSource("/api/events");
  es.onmessage = (ev) => {
    reconnecting = false;
    snap = JSON.parse(ev.data);
    update();
  };
  es.onerror = () => {
    reconnecting = true;
    update();
    if (es.readyState === EventSource.CLOSED) {
      es.close();
      if (!reconnectTimer) {
        reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, RECONNECT_DELAY_MS);
      }
    }
  };
}

buildSkeleton();
update();
connect();
