// Parent dashboard. Plain JS, no build step.
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let state = { kids: [] };

async function api(path, opts = {}) {
  const r = await fetch(path, {
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (r.status === 401) { showLogin(); throw new Error("login"); }
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.status === 204 ? null : r.json();
}

// One sheet for every question (instead of the browser's prompt/confirm/alert, which look broken on a phone).
// Resolves to null on cancel; otherwise true, or the typed text when `input` is given.
function sheet({ title, text = "", input = null, ok = "OK", cancel = "Cancel", kind = "" }) {
  return new Promise((resolve) => {
    const d = $("#sheet"), box = $("#sheetInput");
    $("#sheetTitle").textContent = title;
    $("#sheetText").textContent = text;
    box.hidden = input === null; box.value = ""; box.placeholder = input || "";
    $("#sheetOk").textContent = ok; $("#sheetOk").className = kind;
    $("#sheetCancel").hidden = !cancel; $("#sheetCancel").textContent = cancel || "";
    d.returnValue = "cancel";
    d.onclose = () => resolve(d.returnValue === "ok" ? (input === null ? true : box.value.trim()) : null);
    d.showModal();
    if (input !== null) box.focus();
  });
}
const oops = (err) => sheet({ title: "That didn't work", text: err.message || String(err), cancel: "" });

function showLogin() { $("#login").hidden = false; $("#app").hidden = true; }
function showApp() { $("#login").hidden = true; $("#app").hidden = false; }

$("#loginForm").onsubmit = async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    await api("/api/parent/login", { method: "POST", body: { name: f.get("name"), password: f.get("password") } });
    showApp(); refresh();
  } catch (err) { $("#loginErr").textContent = err.message; }
};

document.querySelectorAll("nav button").forEach((b) => (b.onclick = () => {
  document.querySelectorAll("nav button").forEach((x) => x.classList.toggle("on", x === b));
  document.querySelectorAll("[data-panel]").forEach((p) => (p.hidden = p.dataset.panel !== b.dataset.tab));
  if (b.dataset.tab === "chat") { $("#chatDot").textContent = ""; chat.seen = chat.last; loadChat(); }
  ({ requests: loadRequests, web: loadWeb, games: loadGames, voice: loadInvites }[b.dataset.tab] || (() => {}))();
}));

function fmtMin(m) { return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m`; }

async function refresh() {
  const o = await api("/api/parent/overview");
  showApp();
  state.kids = o.kids;
  const pending = o.kids.reduce((n, k) => n + k.pending, 0);
  $("#reqCount").textContent = pending || "";
  $("#kids").innerHTML = o.kids.map((k) => {
    const t = k.time, total = t.allowance + t.granted, left = total ? Math.max(0, Math.min(100, (t.minutes_left / total) * 100)) : 0;
    const status = k.online ? (k.running ? `Playing <b>${esc(k.running)}</b>` : "On the home screen") : "Offline";
    const reason = { time: "out of time", bedtime: "bedtime", paused: "paused" }[t.lock_reason] || t.lock_reason;
    const lock = t.locked ? `<span class="tagline bad">Locked: ${esc(reason)}</span>` : (t.warn ? `<span class="warn">Under 10 minutes left.</span>` : "");
    const apps = (k.apps || []).map((a) => {
      const sys = { kidspc: "Kids PC", webfilter: "Web filter" }[a.app];
      const retry = !sys && a.state === "failed" ? `<button class="ghost small" data-installapp="${esc(a.app)}" data-kid="${k.id}">Retry</button>` : "";
      const cls = a.state === "failed" ? "failed" : a.state === "installed" ? "" : "pending";
      return `<span class="chip ${cls}" title="${esc(a.detail)}">${esc(sys || a.app)} ${esc(sys ? a.detail : a.state)}${retry}</span>`;
    }).join("");
    return `<div class="card kid ${k.online ? "online" : ""}">
      <div class="kidhead">
        <div class="avatar">${esc(k.name.slice(0, 1))}</div>
        <div class="kidname"><h2>${esc(k.name)}</h2><div class="status"><span class="dot ${k.online ? "on" : ""}"></span>${status}</div></div>
        <div class="timeleft"><div class="big">${fmtMin(t.minutes_left)}</div><div class="muted">left today</div></div>
      </div>
      <div class="fuel ${t.locked || left <= 12 ? "empty" : left <= 34 ? "low" : ""}"><div style="width:${left}%"></div></div>
      <div class="muted">Played ${fmtMin(t.used)} of ${fmtMin(total)} today. ${lock}</div>
      ${k.flagged_today ? `<div class="alert"><span>${k.flagged_today} flagged search${k.flagged_today > 1 ? "es" : ""} today</span><button class="bad small" data-seesearch="${k.id}">See</button></div>` : ""}
      <div class="actions even">
        ${[15, 30, 60].map((m) => `<button class="ghost" data-grant="${k.id}" data-min="${m}">+${m}m</button>`).join("")}
      </div>
      <div class="actions even">
        ${t.lock_reason === "paused"
          ? `<button class="good" data-resume="${k.id}">Resume</button>`
          : `<button class="hot" data-pause="${k.id}">Pause now</button>`}
        <button data-msg="${k.id}">Message</button>
      </div>
      <div class="actions even">
        <button class="ghost small" data-cmd="restart" data-kid="${k.id}">Restart PC</button>
        <button class="ghost small" data-cmd="shutdown" data-kid="${k.id}">Shut down</button>
      </div>
      ${apps ? `<div class="chips">${apps}</div>` : ""}
      <details data-activity="${k.id}"><summary class="muted">Activity</summary><ul class="list"></ul></details>
    </div>`;
  }).join("");
  document.querySelectorAll("[data-grant]").forEach((b) => (b.onclick = async () => {
    await api(`/api/parent/kids/${b.dataset.grant}/grant`, { method: "POST", body: { minutes: +b.dataset.min } });
    refresh();
  }));
  document.querySelectorAll("[data-pause]").forEach((b) => (b.onclick = async () => {
    await api(`/api/parent/kids/${b.dataset.pause}/pause`, { method: "POST", body: { minutes: 0 } }); refresh();
  }));
  document.querySelectorAll("[data-resume]").forEach((b) => (b.onclick = async () => {
    await api(`/api/parent/kids/${b.dataset.resume}/pause`, { method: "DELETE" }); refresh();
  }));
  document.querySelectorAll("[data-msg]").forEach((b) => (b.onclick = async () => {
    const text = await sheet({ title: `Message for ${kidName(b.dataset.msg)}`, text: "It pops up on his screen, even over a game.", input: "Dinner in 5!", ok: "Send" });
    if (text) { await api(`/api/parent/kids/${b.dataset.msg}/commands`, { method: "POST", body: { kind: "message", arg: text } }); b.textContent = "Sent"; }
  }));
  document.querySelectorAll("[data-cmd]").forEach((b) => (b.onclick = async () => {
    const verb = b.dataset.cmd === "restart" ? "Restart" : "Shut down";
    if (!await sheet({ title: `${verb} ${kidName(b.dataset.kid)}'s PC?`, text: "He gets a 1-minute warning.", ok: verb, kind: "bad" })) return;
    await api(`/api/parent/kids/${b.dataset.kid}/commands`, { method: "POST", body: { kind: b.dataset.cmd } }); b.textContent = "Sent";
  }));
  document.querySelectorAll("[data-seesearch]").forEach((b) => (b.onclick = () => {
    $("#webKid").value = b.dataset.seesearch; document.querySelector('nav button[data-tab="web"]').click();
  }));
  document.querySelectorAll("[data-installapp]").forEach((b) => (b.onclick = async () => {
    await api(`/api/parent/kids/${b.dataset.kid}/commands`, { method: "POST", body: { kind: "install_app", arg: b.dataset.installapp } }); b.textContent = "Retrying…";
  }));
  document.querySelectorAll("[data-activity]").forEach((d) => (d.ontoggle = async () => {
    if (!d.open) return;
    const a = await api(`/api/parent/kids/${d.dataset.activity}/activity`);
    const label = { stopped_time_up: "Stopped (time up)", game_blocked: "Blocked unapproved", app_installed: "Installed", app_install_failed: "Install failed", browser_blocked: "Closed other browser", web_filter: "Web filter set" };
    const rows = [
      ...a.events.map((e) => ({ at: e.at, text: `${label[e.kind] || e.kind}: ${e.detail}` })),
      ...a.commands.map((c) => ({ at: c.created_at.replace(" ", "T") + "Z", text: `${c.created_by} sent ${c.kind}${c.arg ? ` "${c.arg}"` : ""} (${c.state}${c.result ? ": " + c.result : ""})` })),
    ].sort((x, y) => (x.at < y.at ? 1 : -1)).slice(0, 30);
    d.querySelector("ul").innerHTML = rows.map((r) => `<li>${esc(r.text)}<div class="muted">${new Date(r.at).toLocaleString()}</div></li>`).join("") || `<li class="muted">Nothing yet.</li>`;
  }));
  fillKidSelects();
}

function fillKidSelects() {
  const opts = state.kids.map((k) => `<option value="${k.id}">${esc(k.name)}</option>`).join("");
  for (const sel of ["#webKid", "#inviteKid"]) if (!$(sel).options.length) $(sel).innerHTML = opts;
  if (!$("#gameKids").querySelector("input"))
    $("#gameKids").insertAdjacentHTML("beforeend", state.kids.map((k) => `<label><input type="checkbox" name="kid" value="${k.id}" checked>${esc(k.name)}</label>`).join(""));
}

const kidName = (id) => (state.kids.find((k) => k.id === id) || {}).name || id;

// Visit times arrive as ISO text from the PC; show them like every other time on the dashboard.
const when = (at) => { const d = new Date(at); return isNaN(d) ? at : d.toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" }); };
const localTime = (utc) => new Date(utc.replace(" ", "T") + (utc.endsWith("Z") || utc.includes("+") ? "" : "Z"))
  .toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });

async function loadRequests() {
  const [reqs, games] = await Promise.all([api("/api/parent/requests"), api("/api/parent/games")]);
  $("#requests").innerHTML = reqs.length ? reqs.map((r) => {
    const what = r.kind === "time" ? `${esc(r.detail)} more minutes` : r.kind === "game" ? `Game: ${esc(r.detail)}` : `Site: ${esc(r.detail)}`;
    const gamePick = r.kind === "game"
      ? `<select data-gamefor="${r.id}"><option value="">Choose the game…</option>
           <optgroup label="Already in the catalogue">${games.map((g) => `<option value="${g.id}">${esc(g.title)}</option>`).join("")}</optgroup>
           <optgroup label="Searching Steam…" data-steamfor="${r.id}"></optgroup></select>
         <img class="art" data-artfor="${r.id}" hidden alt=""><div class="muted" data-infofor="${r.id}"></div>` : "";
    return `<div class="card req"><div class="who">${esc(kidName(r.kid_id))} asks for</div><div class="what">${what}</div>
      ${r.note ? `<div class="note">"${esc(r.note)}"</div>` : ""}
      <div class="muted">${esc(localTime(r.created_at))}</div>${gamePick}
      <div class="actions even"><button class="good" data-decide="${r.id}" data-ok="1">Approve</button><button class="bad" data-decide="${r.id}" data-ok="0">Decline</button></div></div>`;
  }).join("") : `<p class="muted">No requests waiting.</p>`;
  // Fill each game request with Steam matches and preselect the best one.
  reqs.filter((r) => r.kind === "game").forEach(async (r) => {
    const group = $(`[data-steamfor="${r.id}"]`), sel = $(`[data-gamefor="${r.id}"]`), img = $(`[data-artfor="${r.id}"]`);
    const inCat = games.find((g) => g.title.toLowerCase() === r.detail.toLowerCase());
    if (inCat) sel.value = inCat.id;
    let hits = [];
    const picked = (r.ref || "").startsWith("steam:") ? r.ref.slice(6) : "";
    try {
      if (picked) { const a = await api(`/api/parent/steam/app/${picked}`); if (a.appid) hits = [a]; }
      const more = await api(`/api/parent/steam/search?q=${encodeURIComponent(r.detail)}`);
      hits = hits.concat(more.filter((h) => h.appid !== picked));
    } catch {}
    const info = $(`[data-infofor="${r.id}"]`);
    group.label = hits.length ? "Add from Steam" : "No Steam matches";
    const byId = Object.fromEntries(hits.map((h) => [h.appid, h]));
    group.innerHTML = hits.map((h) => `<option value="steam:${h.appid}" data-img="${esc(h.image_url)}">${esc(h.name)}${h.esrb ? ` (${esc(h.esrb)})` : ""}${h.appid === picked ? " ← the one he picked" : ""}</option>`).join("");
    const showArt = () => {
      const o = sel.selectedOptions[0]; const src = o?.dataset.img || games.find((g) => String(g.id) === sel.value)?.image_url;
      img.hidden = !src; if (src) img.src = src;
      const h = sel.value.startsWith("steam:") ? byId[sel.value.slice(6)] : null;
      info.innerHTML = h ? [h.esrb ? `<b>Rated ${esc(h.esrb)}</b>${h.esrb_descriptors ? `: ${esc(h.esrb_descriptors)}` : ""}` : "No ESRB rating on Steam",
        h.adult ? `<span class="tagline bad">Adult content</span>` : "", h.content_notes ? esc(h.content_notes) : "", esc(h.description || ""),
        h.free ? "Free" : `${esc(h.price || "Paid")} · <a href="https://store.steampowered.com/app/${esc(h.appid)}" target="_blank" rel="noopener">Buy on Steam</a> (gift it to his account, or buy it on yours and share it through Steam Family)`,
      ].filter(Boolean).join("<br>") : "";
    };
    if (picked && byId[picked]) sel.value = `steam:${picked}`;
    else if (!sel.value && hits.length) sel.value = `steam:${hits[0].appid}`;
    sel.onchange = showArt; showArt();
  });
  document.querySelectorAll("[data-decide]").forEach((b) => (b.onclick = async () => {
    const pick = $(`[data-gamefor="${b.dataset.decide}"]`);
    const body = { approve: b.dataset.ok === "1" };
    if (!body.approve) {
      const reason = await sheet({ title: `Tell ${kidName(reqs.find((r) => String(r.id) === b.dataset.decide)?.kid_id)} why?`, text: "Optional. It shows on his screen.", input: "Maybe when you're older", ok: "Decline", kind: "bad" });
      if (reason === null) return;
      body.reason = reason;
    }
    if (pick && body.approve) {
      if (!pick.value) return sheet({ title: "Choose the game first", cancel: "" });
      if (pick.value.startsWith("steam:")) body.steam_appid = pick.value.slice(6); else body.game_id = +pick.value;
    }
    try { await api(`/api/parent/requests/${b.dataset.decide}`, { method: "POST", body }); } catch (err) { return oops(err); }
    loadRequests(); refresh();
  }));
}

async function loadWeb() {
  const kid = $("#webKid").value, src = $("#webSource").value;
  const [visits, sites, searches] = await Promise.all([
    api(`/api/parent/visits/${kid}?limit=200${src ? `&source=${src}` : ""}`), api("/api/parent/sites"), api(`/api/parent/searches/${kid}?limit=100`),
  ]);
  $("#searches").innerHTML = searches.map((q) => `<li>${q.flagged ? '<span class="tagline bad">Flagged</span> ' : ""}${esc(q.term)}
    <div class="muted">${esc(new Date(q.at).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" }))}</div></li>`).join("") || `<li class="muted">No searches yet.</li>`;
  $("#visits").innerHTML = visits.map((v) => `<li>${v.blocked ? '<span class="tagline bad">Blocked</span> ' : ""}<b>${esc(v.title || v.domain)}</b>
    <div class="muted">${esc(v.url || v.domain)}</div><div class="muted">${esc(when(v.at))} · ${v.source === "chrome" ? "Chrome" : "network"}</div></li>`).join("") || `<li class="muted">Nothing yet.</li>`;
  $("#webMode").textContent = sites.mode === "allowlist" ? "(allow-list mode: only these load)" : "(filtered mode: list is informational)";
  $("#sites").innerHTML = sites.sites.map((d) => `<li class="split">${esc(d)} <button class="ghost small" data-delsite="${esc(d)}">Remove</button></li>`).join("");
  document.querySelectorAll("[data-delsite]").forEach((b) => (b.onclick = async () => { await api(`/api/parent/sites/${encodeURIComponent(b.dataset.delsite)}`, { method: "DELETE" }); loadWeb(); }));
}
$("#webKid").onchange = $("#webSource").onchange = loadWeb;
$("#siteForm").onsubmit = async (e) => { e.preventDefault(); await api("/api/parent/sites", { method: "POST", body: { domain: new FormData(e.target).get("domain") } }); e.target.reset(); loadWeb(); };

async function loadGames() {
  const games = await api("/api/parent/games");
  $("#games").innerHTML = games.map((g) => `<li>${g.image_url ? `<img class="art" src="${esc(g.image_url)}" alt="" loading="lazy">` : ""}<b>${esc(g.title)}</b> <span class="muted">${g.launcher} ${esc(g.launch_ref)} ${esc(g.rating)}</span>
    <div class="actions">${state.kids.map((k) => `<label><input type="checkbox" data-appr="${g.id}" data-kid="${k.id}" ${g.approved_for.includes(k.id) ? "checked" : ""}> ${esc(k.name)}</label>`).join("")}
    <button class="ghost small" data-delgame="${g.id}">Remove</button></div></li>`).join("") || `<li class="muted">Catalogue is empty.</li>`;
  document.querySelectorAll("[data-appr]").forEach((c) => (c.onchange = () => api(`/api/parent/games/${c.dataset.appr}/kids/${c.dataset.kid}`, { method: "PUT", body: { approved: c.checked } })));
  document.querySelectorAll("[data-delgame]").forEach((b) => (b.onclick = async () => { await api(`/api/parent/games/${b.dataset.delgame}`, { method: "DELETE" }); loadGames(); }));
}
$("#gameForm").onsubmit = async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    await api("/api/parent/games", { method: "POST", body: {
      title: f.get("title"), launcher: f.get("launcher"), launch_ref: f.get("launch_ref"), rating: f.get("rating"), approved_for: f.getAll("kid"),
    } });
  } catch (err) { oops(err); return; }
  e.target.reset(); loadGames();
};
$("#gameForm [name=launcher]").onchange = (e) => {
  const steam = e.target.value === "steam";
  $("#gameForm [name=launch_ref]").closest("label").firstChild.textContent = steam ? "Steam store link or app id " : "Launch reference ";
  $("#gameForm [name=title]").placeholder = steam ? "Leave blank for Steam: filled in from the store" : "";
};

// navigator.clipboard only exists on https pages, and the dashboard is usually opened over plain http on the
// home network, so fall back to the old select-and-copy way.
async function copyText(text) {
  try { if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; } } catch { /* fall through */ }
  const t = document.createElement("textarea");
  t.value = text; t.readOnly = true; t.style.cssText = "position:fixed;top:0;left:0;opacity:0";
  (document.querySelector("dialog[open]") || document.body).appendChild(t);
  t.select(); t.setSelectionRange(0, text.length);
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { /* not allowed */ }
  t.remove();
  return ok;
}

async function loadInvites() {
  const inv = await api("/api/parent/invites");
  $("#invites").innerHTML = inv.map((i) => `<li><b>${esc(i.friend_name)}</b> with ${esc(kidName(i.kid_id))} ${i.revoked ? '<span class="tagline bad">Revoked</span>' : ""}
    <div class="muted">${esc(i.link)}</div>
    ${i.revoked ? "" : `<div class="actions"><button data-copy="${esc(i.link)}">Copy link</button><button class="ghost" data-revoke="${esc(i.code)}">Revoke</button></div>`}</li>`).join("") || `<li class="muted">No invites yet.</li>`;
  document.querySelectorAll("[data-copy]").forEach((b) => (b.onclick = async () => { b.textContent = (await copyText(b.dataset.copy)) ? "Copied" : "Couldn't copy: press and hold the link"; }));
  document.querySelectorAll("[data-revoke]").forEach((b) => (b.onclick = async () => { await api(`/api/parent/invites/${b.dataset.revoke}`, { method: "DELETE" }); loadInvites(); }));
}
$("#inviteForm").onsubmit = async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  await api("/api/parent/invites", { method: "POST", body: { kid_id: f.get("kid_id"), friend_name: f.get("friend_name") } });
  e.target.reset(); fillKidSelects(); loadInvites();
};

refresh().catch(() => {});
setInterval(() => { if (!$("#app").hidden && !$('[data-panel="now"]').hidden) refresh().catch(() => {}); }, 30000);

// Family chat ---------------------------------------------------------------
const chat = { last: 0, seen: 0, started: false };
function chatOpen() { return !$("#app").hidden && !$('[data-panel="chat"]').hidden; }
async function loadChat() {
  const msgs = await api(`/api/parent/chat?after=${chat.last}`);
  if (!msgs.length) return;
  const log = $("#chatLog");
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  log.insertAdjacentHTML("beforeend", msgs.map((m) => `<div class="msg ${m.mine ? "mine" : ""} ${m.kid_id ? "kid" : ""}">
    ${m.mine ? "" : `<div class="who">${esc(m.sender)}</div>`}<div class="bubble">${esc(m.text)}</div>
    <div class="when">${esc(new Date(m.at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }))}</div></div>`).join(""));
  if (atBottom || !chat.started) log.scrollTop = log.scrollHeight;
  if (!chat.started) chat.seen = msgs[msgs.length - 1].id;
  chat.started = true;
  chat.last = msgs[msgs.length - 1].id;
  if (chatOpen()) chat.seen = chat.last;
  const unread = msgs.filter((m) => m.id > chat.seen && !m.mine).length;
  if (unread) $("#chatDot").textContent = "\u25CF";
}
$("#chatForm").onsubmit = async (e) => {
  e.preventDefault();
  const input = e.target.text;
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  try { await api("/api/parent/chat", { method: "POST", body: { text } }); } catch (err) { input.value = text; oops(err); }
  loadChat().catch(() => {});
};
let chatTick = 0;  // every 4 s with the chat open, every 16 s otherwise (for the red dot)
setInterval(() => { if (!$("#app").hidden && (chatOpen() || ++chatTick % 4 === 0)) loadChat().catch(() => {}); }, 4000);
