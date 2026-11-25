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
    const inp = typeof input === "object" && input ? input : { placeholder: input || "", value: "" };
    box.hidden = input === null; box.value = inp.value || ""; box.placeholder = inp.placeholder || "";
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

// Pages. On a phone the bottom bar has room for Home, Requests, Activity and Messages; "More" opens the rest.
// On a phone, Messages shows the chats too (on a laptop they are the bar docked bottom right).
const LOADERS = {
  home: () => refresh().catch(() => {}), requests: () => loadRequests().catch(oops), activity: () => loadFeed().catch(oops),
  screentime: () => loadScreenTime().catch(oops), web: () => loadWeb(), games: () => loadGames(),
  messages: () => { loadInvites(); if (!wide.matches) { chatSeen(); loadChat().catch(() => {}); loadConvs().catch(() => {}); } },
  connections: () => loadConnections(), family: () => loadPcs(),
};
function go(tab) {
  document.querySelectorAll("nav button[data-tab]").forEach((x) => x.classList.toggle("on", x.dataset.tab === tab));
  document.querySelectorAll("[data-panel]").forEach((p) => (p.hidden = !(p.dataset.panel === tab || (tab === "messages" && p.dataset.panel === "chat" && !wide.matches))));
  document.body.classList.remove("more-open"); $("#moreBtn").setAttribute("aria-expanded", "false");
  $("#moreBtn").classList.toggle("on", !!document.querySelector(`nav button.extra.on`));
  window.scrollTo(0, 0);
  (LOADERS[tab] || (() => {}))();
}
document.querySelectorAll("nav button[data-tab]").forEach((b) => (b.onclick = () => go(b.dataset.tab)));
$("#moreBtn").onclick = () => $("#moreBtn").setAttribute("aria-expanded", document.body.classList.toggle("more-open"));

function fmtMin(m) { return m >= 60 ? `${Math.floor(m / 60)}h${m % 60 ? ` ${m % 60}m` : ""}` : `${m}m`; }

async function refresh() {
  const o = await api("/api/parent/overview");
  showApp();
  state.kids = o.kids; state.overview = o;
  const pending = o.kids.reduce((n, k) => n + k.pending, 0), stuck = o.kids.reduce((x, k) => x + ((k.installs || {}).needs || 0), 0);
  const held = o.held_pictures || 0;
  $("#reqCount").textContent = pending + stuck + held || "";
  // One strip for everything waiting on a parent. The details live in Requests only.
  $("#needs").innerHTML = pending + stuck + held ? `<button class="card needs" data-go="requests"><b>Needs you</b><span>${[
      pending ? `${pending} request${pending > 1 ? "s" : ""} from the kids` : "", held ? `${held} held picture${held > 1 ? "s" : ""}` : "", stuck ? `${stuck} stuck install${stuck > 1 ? "s" : ""}` : ""]
      .filter(Boolean).join(" · ")}</span><span class="go">Open Requests</span></button>` : "";
  const html = o.kids.map((k) => {
    const t = k.time, total = t.allowance + t.granted, left = total ? Math.max(0, Math.min(100, (t.minutes_left / total) * 100)) : 0;
    const status = k.online ? (k.running ? `Playing <b>${esc(k.running)}</b>` : "On the home screen") : "Offline";
    const reason = { time: "out of time", bedtime: "bedtime", paused: "paused", outside_hours: "not play time" }[t.lock_reason] || t.lock_reason;
    const lock = t.locked ? `<span class="tagline bad">Locked: ${esc(reason)}</span>` : (t.warn ? `<span class="warn">Under 10 minutes left.</span>` : "");
    return `<div class="card kid ${k.online ? "online" : ""}">
      <div class="kidhead">
        <div class="avatar">${esc(k.name.slice(0, 1))}</div>
        <div class="kidname"><h2>${esc(k.name)}</h2><div class="status"><span class="dot ${k.online ? "on" : ""}"></span>${status}</div></div>
        <div class="timeleft"><div class="big">${fmtMin(t.minutes_left)}</div><div class="muted">left today</div></div>
      </div>
      <div class="fuel ${t.locked || left <= 12 ? "empty" : left <= 34 ? "low" : ""}"><div style="width:${left}%"></div></div>
      <div class="muted">Played ${fmtMin(t.used)} of ${fmtMin(total)} today. ${lock}${t.next_play ? ` Next play time ${esc(nextPlay(t.next_play))}.` : ""}</div>
      ${(k.consoles || []).length ? `<div class="muted">Includes ${k.consoles.map((c) => `${fmtMin(c.minutes)} on ${esc(c.device)}${c.games.length ? ` (${c.games.slice(0, 3).map((g) => esc(g.name)).join(", ")})` : ""}`).join(" and ")}.</div>` : ""}
      ${k.flagged_today ? `<div class="alert"><span>${k.flagged_today} flagged search${k.flagged_today > 1 ? "es" : ""} today</span><button class="bad small" data-seesearch="${k.id}">See</button></div>` : ""}
      <div class="actions even">
        ${[15, 30, 60].map((m) => `<button class="ghost" data-grant="${k.id}" data-min="${m}">+${m}m</button>`).join("")}
      </div>
      <div class="actions even">
        ${t.lock_reason === "paused"
          ? `<button class="good" data-resume="${k.id}">Resume</button>`
          : `<button class="hot" data-pause="${k.id}" title="Locks the PC now, until you press Resume or midnight">Pause now</button>`}
        <button data-msg="${k.id}">Message</button>
      </div>
      <button class="ghost small linkish" data-feedkid="${k.id}">See ${esc(k.name)}'s activity</button>
    </div>`;
  }).join("") || `<div class="card"><h2>No kids yet</h2><p class="muted">Add one on Family &amp; Devices.</p></div>`;
  fillKidSelects();
  loadFamily().catch(() => {});
  // This runs every 10 seconds: leave the cards alone when nothing changed, so nothing jumps or closes.
  if (html === refresh.last) return;
  refresh.last = html;
  $("#kids").innerHTML = html;
  document.querySelectorAll("[data-go]").forEach((b) => (b.onclick = () => go(b.dataset.go)));
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
    const text = await sheet({ title: `Message for ${kidName(b.dataset.msg)}`, text: "It pops up on the screen, even over a game.", input: "Dinner in 5!", ok: "Send" });
    if (text) { await api(`/api/parent/kids/${b.dataset.msg}/commands`, { method: "POST", body: { kind: "message", arg: text } }); b.textContent = "Sent"; }
  }));
  document.querySelectorAll("[data-seesearch]").forEach((b) => (b.onclick = () => { $("#webKid").value = b.dataset.seesearch; go("web"); }));
  document.querySelectorAll("[data-feedkid]").forEach((b) => (b.onclick = () => { feedState.kid = b.dataset.feedkid; go("activity"); }));
}

// Home: what the kids are doing together. The switches for it are on Connections.
async function loadFamily() {
  const f = await api("/api/parent/family");
  if (f.kids.length < 2) { $("#family").innerHTML = ""; return; }
  const on = f.kids.filter((k) => k.online);
  const names = (ks) => ks.map((k) => esc(k.name)).join(" and ");
  const voice = f.kids.filter((k) => k.in_family_voice);
  const together = f.playing_together ? `${names(on.filter((k) => k.running))} are playing <b>${esc(on.find((k) => k.running).running)}</b> together.`
    : on.length ? on.map((k) => `${esc(k.name)}: ${k.locked ? "done for today" : k.running ? `playing <b>${esc(k.running)}</b>` : "home screen"}`).join(" · ")
    : "Nobody's PC is on.";
  const html = `<div class="card family"><h2>Together</h2><p>${together}</p>
    <div class="muted">${voice.length ? `In family voice: <b>${names(voice)}</b>` : "Nobody is in family voice."}</div></div>`;
  if ($("#family").innerHTML !== html) $("#family").innerHTML = html;
}
async function loadFamilySwitches() {
  const f = await api("/api/parent/family");
  const sw = (key, label, hint) => `<label class="switch"><input type="checkbox" data-family="${key}" ${f.settings[key] ? "checked" : ""}>
      <span><b>${label}</b><small class="muted">${hint}</small></span></label>`;
  $("#familySwitches").innerHTML = `<div class="card family">
    <h2>Between the kids at home</h2>
    ${sw("see", "See each other", "Each PC shows what the others are playing.")}
    ${sw("join", "Join each other's games", "One tap starts the same game, only if it's approved for both.")}
    ${sw("voice", "Family voice", "A voice room just for the kids at home, no invite link. Turning it off ends a call.")}
  </div>`;
  document.querySelectorAll("[data-family]").forEach((c) => (c.onchange = async () => {
    try { await api("/api/parent/family", { method: "PUT", body: { [c.dataset.family]: c.checked } }); } catch (err) { c.checked = !c.checked; oops(err); }
  }));
}
function loadConnections() {
  loadConsoles();
  if (typeof loadSkylight === "function") loadSkylight();
  loadChatSettings().catch(oops);
  loadFamilySwitches().catch(oops);
}

function fillKidSelects() {
  const opts = state.kids.map((k) => `<option value="${k.id}">${esc(k.name)}</option>`).join("");
  for (const sel of ["#webKid", "#inviteKid"]) if ($(sel).options.length !== state.kids.length) { const v = $(sel).value; $(sel).innerHTML = opts; if (v) $(sel).value = v; }
  if ($("#gameKids").querySelectorAll("input").length !== state.kids.length) {
    $("#gameKids").querySelectorAll("label").forEach((l) => l.remove());
    $("#gameKids").insertAdjacentHTML("beforeend", state.kids.map((k) => `<label><input type="checkbox" name="kid" value="${k.id}" checked>${esc(k.name)}</label>`).join(""));
  }
}

const kidName = (id) => (state.kids.find((k) => k.id === id) || {}).name || id;

// Visit times arrive as ISO text from the PC; show them like every other time on the dashboard.
const when = (at) => { const d = new Date(at); return isNaN(d) ? at : d.toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" }); };
const localTime = (utc) => new Date(utc.replace(" ", "T") + (utc.endsWith("Z") || utc.includes("+") ? "" : "Z"))
  .toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });

async function loadStuck() {
  const d = await api("/api/parent/installs");
  const rows = d.pcs.flatMap((p) => p.games.filter((g) => ["needs", "bad"].includes(g.plain.tone)).map((g) =>
    `<div class="card"><div class="who">${esc(p.name)}'s PC</div><ul class="list">${installRow(g, p)}</ul></div>`));
  $("#stuck").innerHTML = rows.join("") || `<p class="muted">Nothing stuck. Approved games are installing by themselves.</p>`;
  wireInstalls($("#stuck"), () => { loadStuck(); refresh().catch(() => {}); });
}
// Pictures and videos the picture check held (screening.py): look, then let through or remove.
async function loadHeld() {
  const d = await api("/api/parent/chat-held");
  state.held = d.held.length;
  $("#heldBox").innerHTML = d.held.length ? `<h2 class="sub">Held pictures</h2>
    <p class="hint">The picture check on your server thought these might show nudity, so the kids haven't seen them. Look, then let through or remove.</p>
    <div class="heldgrid">${d.held.map((m) => `<div class="card held"><div class="who">${esc(m.sender)}${m.friend ? " (friend)" : ""} in ${esc(convTitle(m.conv))}</div>
      <details><summary>Show it</summary>${m.video ? clipHtml(m, picUrl(m.id), vidUrl(m.id)) : `<img src="${picUrl(m.id)}" alt="Held picture" loading="lazy">`}</details>
      ${m.text ? `<div>${esc(m.text)}</div>` : ""}<div class="muted">${esc(new Date(m.at).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" }))}</div>
      <div class="actions even"><button class="good" data-release="${m.id}">Let it through</button><button class="bad" data-remove="${m.id}">Remove</button></div></div>`).join("")}</div>` : "";
  $("#heldBox").querySelectorAll("[data-release]").forEach((b) => (b.onclick = async () => {
    try { await api(`/api/parent/chat/${b.dataset.release}/release`, { method: "POST" }); } catch (err) { return oops(err); }
    loadHeld(); refresh().catch(() => {});
  }));
  wireRemove($("#heldBox"), () => { loadHeld(); refresh().catch(() => {}); });
}
const convTitle = (id) => id === "family" ? "the family chat" : id === "kids" ? "Kids at home" : ((chat.convs.find((c) => c.id === id) || {}).title || "a friend's chat");
async function loadRequests() {
  loadStuck().catch(() => { $("#stuck").innerHTML = ""; });
  loadHeld().catch(() => { $("#heldBox").innerHTML = ""; });
  const [reqs, games] = await Promise.all([api("/api/parent/requests"), api("/api/parent/games")]);
  state.shownReqs = reqs.map((r) => r.id).join(",");
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
  }).join("") : `<p class="muted">No requests waiting. When a kid asks for more time, a game or a site, it shows here.</p>`;
  // Fill each game request with matches from every store and preselect the best one.
  reqs.filter((r) => r.kind === "game").forEach(async (r) => {
    const group = $(`[data-steamfor="${r.id}"]`), sel = $(`[data-gamefor="${r.id}"]`), img = $(`[data-artfor="${r.id}"]`);
    const inCat = games.find((g) => g.title.toLowerCase() === r.detail.toLowerCase());
    if (inCat) sel.value = inCat.id;
    let hits = [];
    const picked = r.ref || "";   // the exact game and store he picked on his screen, e.g. "battlenet:WoWC"
    try {
      if (picked.startsWith("steam:")) {   // a Steam pick goes first even if his words match other games better
        const a = await api(`/api/parent/steam/app/${picked.slice(6)}`);
        if (a.appid) hits.push(...(await api(`/api/parent/games/search?q=${encodeURIComponent(a.name)}`)).results
          .filter((h) => h.platforms.some((p) => `steam:${p.launch_ref}` === picked)).slice(0, 1));
      }
      const more = (await api(`/api/parent/games/search?q=${encodeURIComponent(r.detail)}`)).results;
      hits = hits.concat(more.filter((h) => !hits.some((x) => x.title === h.title)));
    } catch {}
    // The platform to add: the one he picked when this game has it, otherwise the game's default store.
    const platOf = (h) => h.platforms.find((p) => p.addable && `${p.launcher}:${p.launch_ref}` === picked) || h.platforms[h.default];
    hits = hits.filter((h) => h.default !== null);
    const refOf = (h) => { const p = platOf(h); return `${p.launcher}:${p.launch_ref}`; };
    hits.sort((a, b) => (refOf(b) === picked) - (refOf(a) === picked));   // his pick first
    const byRef = Object.fromEntries(hits.map((h) => [refOf(h), h]));
    const info = $(`[data-infofor="${r.id}"]`);
    group.label = hits.length ? "Add from the stores" : "No store matches";
    group.innerHTML = hits.map((h) => `<option value="${esc(refOf(h))}" data-img="${esc(h.image_url)}">${esc(h.title)} · ${esc(platOf(h).label)}${h.esrb ? ` (${esc(h.esrb)})` : ""}${refOf(h) === picked ? " ← the one he picked" : ""}</option>`).join("");
    const showArt = () => {
      const o = sel.selectedOptions[0]; const src = o?.dataset.img || games.find((g) => String(g.id) === sel.value)?.image_url;
      img.hidden = !src; if (src) img.src = src;
      const h = byRef[sel.value];
      info.innerHTML = h ? gameFacts(h, platOf(h)) : "";
    };
    if (picked && byRef[picked]) sel.value = picked;
    else if (!sel.value && hits.length) sel.value = refOf(hits[0]);
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
      if (pick.value.includes(":")) body.ref = pick.value; else body.game_id = +pick.value;
    }
    try { await api(`/api/parent/requests/${b.dataset.decide}`, { method: "POST", body }); } catch (err) { return oops(err); }
    loadRequests(); refresh();
  }));
}

// Activity: one feed of what the kids did, in plain words (portal.py builds the lines) --------------
const feedState = { kid: "", group: "", days: 7 };
const GROUP_WORDS = { "": "Everything", played: "Played", blocked: "Blocked", installs: "Installs", chats: "Chats and calls", requests: "Requests", time: "Time", parents: "Parents" };
async function loadFeed() {
  const q = new URLSearchParams({ kid: feedState.kid, days: feedState.days, group: feedState.group });
  const [f, all] = await Promise.all([api(`/api/parent/feed?${q}`), feedState.group ? api(`/api/parent/feed?${new URLSearchParams({ kid: feedState.kid, days: feedState.days })}`) : null]);
  const counts = (all || f).counts, total = Object.values(counts).reduce((a, b) => a + b, 0);
  $("#feedWho").innerHTML = [["", "Everyone"], ...state.kids.map((k) => [k.id, k.name])]
    .map(([id, name]) => `<button type="button" class="${feedState.kid === id ? "on" : ""}" data-fwho="${esc(id)}">${esc(name)}</button>`).join("");
  $("#feedGroups").innerHTML = Object.entries(GROUP_WORDS).map(([g, w]) => {
    const n = g ? counts[g] : total;
    return `<button type="button" class="chip ${feedState.group === g ? "on" : ""} g-${g || "all"}" data-fgroup="${g}" ${n || g === "" ? "" : "disabled"}>${w}${n ? ` ${n}` : ""}</button>`;
  }).join("");
  const byDay = [];
  for (const i of f.items) {
    const d = new Date(i.at), key = d.toDateString();
    let day = byDay.find((x) => x.key === key);
    if (!day) byDay.push(day = { key, label: ago(i.at).replace(/^./, (c) => c.toUpperCase()) + (ago(i.at).match(/ago$|day$/) ? "" : `, ${d.toLocaleDateString([], { month: "short", day: "numeric" })}`), items: [] });
    day.items.push(i);
  }
  const t = (iso) => new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  $("#feed").innerHTML = byDay.map((d) => `<h3 class="feedday">${esc(d.label)}</h3><ul class="list feed">${d.items.map((i) => `<li class="g-${i.group}">
      <span class="gdot" aria-hidden="true"></span>
      <div><div>${esc(i.text)}${i.minutes ? ` <span class="pill">${fmtMin(i.minutes)}</span>` : ""}</div>
      <div class="muted">${esc(t(i.at))}${i.until && i.minutes > 1 ? ` to ${esc(t(i.until))}` : ""}${!feedState.kid && state.kids.length > 1 && !i.text.includes(kidName(i.kid_id)) ? ` · ${esc(kidName(i.kid_id))}` : ""}</div></div></li>`).join("")}</ul>`).join("")
    || `<div class="card"><p class="muted">Nothing ${feedState.days == 1 ? "today" : `in the last ${feedState.days} days`}${feedState.group ? " of this kind" : ""} yet.</p></div>`;
  document.querySelectorAll("[data-fwho]").forEach((b) => (b.onclick = () => { feedState.kid = b.dataset.fwho; loadFeed().catch(oops); }));
  document.querySelectorAll("[data-fgroup]").forEach((b) => (b.onclick = () => { feedState.group = b.dataset.fgroup; loadFeed().catch(oops); }));
}
$("#feedDays").onchange = (e) => { feedState.days = +e.target.value; loadFeed().catch(oops); };

async function loadWeb() {
  const kid = $("#webKid").value, src = $("#webSource").value, days = $("#webDays").value;
  const [visits, sites, searches] = await Promise.all([
    api(`/api/parent/visits/${kid}?limit=200&days=${days}${src ? `&source=${src}` : ""}`), api("/api/parent/sites"), api(`/api/parent/searches/${kid}?limit=100&days=${days}`),
  ]);
  const none = { 1: "today", 7: "in the last 7 days", 0: "kept" }[days];
  $("#searches").innerHTML = searches.map((q) => `<li>${q.flagged ? '<span class="tagline bad">Flagged</span> ' : ""}${esc(q.term)}
    <div class="muted">${esc(new Date(q.at).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" }))}</div></li>`).join("") || `<li class="muted">No searches ${none}.</li>`;
  $("#visits").innerHTML = visits.map((v) => `<li>${v.blocked ? '<span class="tagline bad">Blocked</span> ' : ""}<b>${esc(v.title || v.domain)}</b>
    <div class="muted">${esc(v.url || v.domain)}</div><div class="muted">${esc(when(v.at))} · ${v.source === "chrome" ? "Chrome" : "network"}</div></li>`).join("") || `<li class="muted">No pages ${none}.</li>`;
  $("#webMode").textContent = sites.mode === "allowlist" ? "(allow-list mode: only these load)" : "(filtered mode: list is informational)";
  $("#sites").innerHTML = sites.sites.map((d) => `<li class="split">${esc(d)} <button class="ghost small" data-delsite="${esc(d)}">Remove</button></li>`).join("");
  document.querySelectorAll("[data-delsite]").forEach((b) => (b.onclick = async () => { await api(`/api/parent/sites/${encodeURIComponent(b.dataset.delsite)}`, { method: "DELETE" }); loadWeb(); }));
}
$("#webKid").onchange = $("#webSource").onchange = $("#webDays").onchange = loadWeb;
document.querySelectorAll("[data-clearweb]").forEach((b) => (b.onclick = async () => {
  const kid = $("#webKid").value, what = b.dataset.clearweb === "searches" ? "searches" : "pages and sites";
  if (!await sheet({ title: `Clear ${kidName(kid)}'s ${what}?`, text: "This deletes all of them from the hub, not just the ones showing.", ok: "Clear", kind: "bad" })) return;
  try { await api(`/api/parent/history/${b.dataset.clearweb}?kid_id=${encodeURIComponent(kid)}`, { method: "DELETE" }); } catch (err) { return oops(err); }
  loadWeb();
}));
$("#siteForm").onsubmit = async (e) => { e.preventDefault(); await api("/api/parent/sites", { method: "POST", body: { domain: new FormData(e.target).get("domain") } }); e.target.reset(); loadWeb(); };

// What a parent needs to decide on a game: rating, where it's on, and how to get it.
function gameFacts(h, p) {
  const d = h.details || {};
  const others = h.platforms.filter((x) => x !== p).map((x) => x.label);
  const how = p.platform === "steam"
    ? (d.free ? "Free on Steam" : `${esc(d.price || "Paid")} · <a href="https://store.steampowered.com/app/${esc(p.launch_ref)}" target="_blank" rel="noopener">Buy on Steam</a> (gift it to his account, or buy it on yours and share it through Steam Family)`)
    : p.platform === "battlenet" ? "Through Battle.net: he signs in to Blizzard's app on his PC, and the game downloads there"
    : "Included with PC Game Pass, through the Xbox app";
  return [h.esrb ? `<b>Rated ${esc(h.esrb)}</b>${d.esrb_descriptors ? `: ${esc(d.esrb_descriptors)}` : ""}` : "No ESRB rating listed",
    h.adult ? `<span class="tagline bad">Adult content</span>` : "", d.content_notes ? esc(d.content_notes) : "", esc(d.description || ""),
    how, others.length ? `Also on ${esc(others.join(", "))}` : "",
  ].filter(Boolean).join("<br>");
}

let gameHits = [];
// ESRB order for the "Rated up to" menu. RP (rating pending) and unrated games count as unknown: shown, marked.
const ESRB_RANK = { EC: 0, E: 1, "E10+": 2, T: 3, M: 4, AO: 5 };
const overCap = (h) => { const max = $("#gameMax").value; return !!max && (h.adult || (h.esrb in ESRB_RANK && ESRB_RANK[h.esrb] > ESRB_RANK[max])); };
function gameNote() {
  const note = $("#gameSearchNote"), hidden = gameHits.filter(overCap).length, shown = gameHits.length - hidden;
  if (!gameHits.length) return;
  note.textContent = (shown ? "Tap a store to choose where he gets it." : "Every match is rated above your limit.")
    + (hidden ? ` ${hidden} game${hidden > 1 ? "s" : ""} rated above ${$("#gameMax").selectedOptions[0].textContent.replace(/^Up to |^Rated | only$/g, "")} hidden.` : "");
}
const SEARCH_NOTE = "Searches Steam, Battle.net and PC Game Pass together. Pick a game and the hub works out the rest.";
function renderGameHits() {
  $("#gameResults").innerHTML = gameHits.map((h, i) => {
    if (overCap(h)) return "";
    const p = h.platforms[h.pick ?? h.default ?? 0];
    const kids = state.kids.map((k) => `<label><input type="checkbox" data-hitkid="${i}" value="${k.id}" checked> ${esc(k.name)}</label>`).join("");
    return `<li>${h.image_url ? `<img class="art" src="${esc(h.image_url)}" alt="" loading="lazy">` : ""}
      <b>${esc(h.title)}</b> ${h.esrb ? `<span class="tagline rating">${esc(h.esrb)}</span>` : ""} ${h.adult ? `<span class="tagline bad">Adult</span>` : ""}
      <div class="chips">${h.platforms.map((x, j) => `<button type="button" class="chip${x === p && x.addable ? " on" : ""}" data-hit="${i}" data-plat="${j}" ${x.addable ? "" : "disabled"}>${esc(x.label)}</button>`).join("")}</div>
      <div class="muted">${p.addable ? gameFacts(h, p) : esc(p.note)}</div>
      ${p.addable ? `<div class="actions">${kids}<button class="small" data-addhit="${i}">${p.game_id ? "Approve" : "Add"}</button></div>` : ""}</li>`;
  }).join("");
  document.querySelectorAll("[data-plat]").forEach((b) => (b.onclick = () => { gameHits[b.dataset.hit].pick = +b.dataset.plat; renderGameHits(); }));
  document.querySelectorAll("[data-addhit]").forEach((b) => (b.onclick = async () => {
    const h = gameHits[b.dataset.addhit], p = h.platforms[h.pick ?? h.default];
    const kids = [...document.querySelectorAll(`[data-hitkid="${b.dataset.addhit}"]:checked`)].map((c) => c.value);
    try { await api("/api/parent/games/from-search", { method: "POST", body: { ref: `${p.launcher}:${p.launch_ref}`, approved_for: kids } }); } catch (err) { return oops(err); }
    b.textContent = "Added"; b.disabled = true; loadGames();
  }));
}
$("#gameSearch").onsubmit = async (e) => {
  e.preventDefault();
  const q = new FormData(e.target).get("q").trim(), note = $("#gameSearchNote");
  note.textContent = "Searching…"; gameHits = []; renderGameHits();
  try {
    const r = await api(`/api/parent/games/search?q=${encodeURIComponent(q)}`);
    gameHits = r.results;
    if (!gameHits.length) note.textContent = "No games found. Try a shorter name.";
    else { gameNote(); if (r.xbox === "unavailable") note.textContent += " Couldn't reach the Xbox catalogue just now, so Game Pass isn't shown."; }
  } catch (err) { note.textContent = err.message; }
  renderGameHits();
};
// Clearing the box (the x, or deleting the words) clears the results too, ready for a new search.
$("#gameSearch").q.oninput = (e) => { if (!e.target.value.trim()) { gameHits = []; renderGameHits(); $("#gameSearchNote").textContent = SEARCH_NOTE; } };
// The rating menu filters what is already showing; no new search needed.
$("#gameMax").onchange = () => { renderGameHits(); gameNote(); };

// PCs: like an IT office's view of its computers, in plain words. Every approved game on every PC, how far
// along it is, and the one thing to do when it's stuck. The hub writes the words (installs.py).
const APP_NAME = { steam: "Steam", battlenet: "Battle.net", chrome: "Chrome" };
let pcTimer = 0;
// The hub itself: which version it runs and whether it keeps itself up to date (deploy/hub-autoupdate.sh).
function hubCard(h) {
  if (!h) return "";
  const when = (t) => t ? new Date(t).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" }) : "";
  const v = h.version === "dev" ? "" : ` ${h.version.slice(0, 7)}`;
  let line, tone = "";
  if (!h.self_update) line = `On version${v}. It doesn't update itself yet: run the setup line on the TrueNAS once to turn that on.`;
  else if (h.result === "rolled_back" || h.result === "build_failed" || h.result === "down") {
    tone = "attn";
    line = `${h.failed_title ? `"${esc(h.failed_title)}" didn't go in. ` : ""}${esc(h.message || "")}`;
  } else {
    line = `Updates itself within about 2 minutes of a merge. On${v}${h.title ? `: ${esc(h.title)}` : ""}${h.updated_at ? `, since ${esc(when(h.updated_at))}` : ""}.`;
    if (h.result === "fetch_failed") { tone = "attn"; line += ` ${esc(h.message)}`; }
  }
  const behind = h.behind && !tone ? `<div class="muted">On its way to the hub: ${esc(h.behind.title || h.behind.version.slice(0, 7))}.</div>` : "";
  return `<div class="card pc hub ${tone}"><div class="pchead"><div class="avatar">H</div>
    <div><h2>The hub</h2><div class="status">${line}</div></div></div>${behind}</div>`;
}
function gb(n) { return n == null ? "" : n >= 100 ? `${Math.round(n)} GB free` : `${n.toFixed(1)} GB free`; }
// One approved game on one PC: where it stands, and the one thing to do when it's stuck (installs.py writes the words).
function installRow(g, p) {
  const t = g.plain, pct = t.percent ?? null;
  const btn = t.fix === "retry" ? `<button class="small" data-pcretry="${g.game_id}" data-kid="${p.id}">Try again</button>`
    : t.fix === "install" ? `<button class="small" data-pcinstall="${g.game_id}" data-kid="${p.id}">Install</button>`
    : t.fix === "signin" ? `<button class="small" data-pchow="${esc(g.launcher)}" data-kid="${p.id}">How</button><button class="ghost small" data-pcretry="${g.game_id}" data-kid="${p.id}">Try again</button>` : "";
  return `<li class="inst t-${t.tone}">
    ${g.image_url ? `<img src="${esc(g.image_url)}" alt="" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('span'), { className: 'noart' }))">` : `<span class="noart" aria-hidden="true"></span>`}
    <div class="what"><b>${esc(g.title)}</b> <span class="pill t-${t.tone}">${esc(t.label)}</span>
      ${pct !== null ? `<div class="bar"><div style="width:${pct}%"></div></div>` : t.tone === "busy" ? `<div class="bar busy"><div></div></div>` : ""}
      <div class="muted">${esc(t.text)}</div>${t.since ? `<div class="since">${esc(t.since)}</div>` : ""}
      ${g.store_log && t.tone !== "ok" ? `<details class="storelog"><summary>What ${esc(APP_NAME[g.launcher] || "the store")} said</summary><code>${esc(g.store_log)}</code></details>` : ""}</div>
    <div class="do">${btn}</div></li>`;
}
function wireInstalls(root, reload) {
  root.querySelectorAll("[data-pcretry]").forEach((b) => (b.onclick = async () => {
    b.disabled = true; b.textContent = "Starting…";
    try { await api(`/api/parent/installs/${b.dataset.kid}/${b.dataset.pcretry}/retry`, { method: "POST" }); } catch (err) { oops(err); }
    reload();
  }));
  root.querySelectorAll("[data-pcinstall]").forEach((b) => (b.onclick = async () => {
    b.disabled = true; b.textContent = "Sending…";
    try { await api(`/api/parent/installs/${b.dataset.kid}/${b.dataset.pcinstall}`, { method: "PUT", body: { install: true } }); } catch (err) { oops(err); }
    reload();
  }));
  root.querySelectorAll("[data-pchow]").forEach((b) => (b.onclick = () => {
    const store = APP_NAME[b.dataset.pchow] || "the store", name = kidName(b.dataset.kid);
    const text = b.dataset.pchow === "battlenet"
      ? `This is the one step that needs someone at ${name}'s PC, once. Sign in to the parent account on the PC, open Battle.net, sign in with a Blizzard account and tick "Keep me logged in". Then come back here and press Try again. Every game after that installs by itself.`
      : `This is the one step that needs someone at ${name}'s PC, once. On ${name}'s screen, Steam asks for a sign-in: use ${name}'s Steam account (a Steam Family child account is best) and tick "Remember me". Then press Try again here. Every game after that installs by itself.`;
    sheet({ title: `Sign in to ${store} once`, text, cancel: "" });
  }));
}

// Family & Devices: each kid, his devices and their software, the games installing, and the parents' remote controls.
// Like an IT office's view of its computers, in plain words.
async function loadPcs() {
  clearTimeout(pcTimer);
  if (document.querySelector('[data-panel="family"]').hidden) return;
  let d, o, devs;
  try { [d, o, devs] = await Promise.all([api("/api/parent/installs"), api("/api/parent/overview"), api("/api/parent/devices")]); }
  catch (err) { if (err.message !== "login") $("#pcs").innerHTML = `<p class="muted">${esc(err.message)}</p>`; return; }
  state.kids = o.kids; fillKidSelects();
  const open = [...document.querySelectorAll("#pcs details[open]")].map((x) => x.dataset.k);
  $("#pcSummary").textContent = d.summary;
  $("#pcs").innerHTML = d.pcs.map((p) => {
    const k = o.kids.find((x) => x.id === p.id) || { apps: [] }, dv = (devs[p.id] || {}).devices || [];
    const seen = p.online ? "On now" : p.seen_at ? `Off. Last on ${new Date(p.seen_at).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}` : "No device set up yet";
    const apps = (k.apps || []).map((a) => {
      const sys = { kidspc: "Kids PC", webfilter: "Web filter" }[a.app];
      const retry = !sys && a.state === "failed" ? `<button class="ghost small" data-installapp="${esc(a.app)}" data-kid="${p.id}">Retry</button>` : "";
      const install = a.app === "kidspc" && k.online && / ready/.test(a.detail) ? `<button class="ghost small" data-update="${p.id}">Install now</button>` : "";
      const cls = a.state === "failed" ? "failed" : a.state === "installed" ? "" : "pending";
      return `<span class="chip ${cls}" title="${esc(a.detail)}"><b>${esc(sys || APP_NAME[a.app] || a.app)}</b>&nbsp;${esc(sys ? a.detail : a.state === "installed" ? "ready" : a.state)}${retry}${install}</span>`;
    }).join("");
    const hasPc = (k.apps || []).some((a) => a.app === "kidspc");
    const how = hasPc ? `<span class="chip">Updates go in ${k.auto_update ? "right away" : "at the next restart"}<button class="ghost small" data-autoupdate="${p.id}" data-on="${k.auto_update ? 1 : 0}">Change</button></span>` : "";
    const pc = (k.apps || []).find((a) => a.app === "kidspc"), B = o.builds || {}, b = B.building;
    let newer = "";
    if (pc && b) newer = `${b.waiting_minutes >= 15 ? "A newer build should have arrived by now" : "A newer build is being made"}${b.title ? `: ${esc(b.title)}` : ""}.`;
    else if (pc && k.online && B.published && !pc.detail.includes(B.published.slice(0, 8))) newer = `A newer build${B.published_title ? ` (${esc(B.published_title)})` : ""} is on its way.`;
    const busy = p.games.filter((g) => g.plain.tone !== "ok"), done = p.games.filter((g) => g.plain.tone === "ok");
    const devices = dv.map((x) => `<li class="split"><span><b>${esc(x.name || x.computer)}</b>
        <span class="muted">${x.name ? `${esc(x.computer)} · ` : ""}${x.last_seen ? `last on ${esc(ago(x.last_seen))}` : `set up ${esc(ago(x.set_up_at))}`}</span></span>
        <button class="ghost small" data-namedev="${esc(x.computer)}" data-kid="${p.id}" data-name="${esc(x.name)}">${x.name ? "Rename" : "Name it"}</button></li>`).join("")
      || (p.seen_at ? `<li class="muted">${esc(p.name)}'s PC will show here, with its name, next time it checks in.</li>` : "");
    return `<div class="card pc ${p.online ? "online" : ""}">
      <div class="pchead"><div class="avatar">${esc(p.name.slice(0, 1))}</div>
        <div><h2>${esc(p.name)}</h2><div class="status"><span class="dot ${p.online ? "on" : ""}"></span>${esc(seen)}${p.disk_free_gb != null ? ` · ${gb(p.disk_free_gb)}` : ""}</div></div></div>
      <h3>Devices</h3>${devices ? `<ul class="list devices">${devices}</ul>` : ""}
      ${apps ? `<h3>Kept on ${dv.length > 1 ? "each" : "the"} PC by the hub</h3><p class="hint">Nothing to set up here: the hub installs these by itself and keeps them working. Retry shows if one fails.</p>` : ""}
      ${apps || how ? `<div class="chips">${apps}${how}</div>` : ""}${newer ? `<div class="muted">${newer}</div>` : ""}
      <div class="actions even">
        <button class="ghost small" data-setup="${p.id}">+ Add a device</button>
        ${k.online ? `<button class="ghost small" data-cmd="restart" data-kid="${p.id}">Restart</button><button class="ghost small" data-cmd="shutdown" data-kid="${p.id}">Shut down</button>` : ""}
      </div>
      ${busy.length ? `<h3>Games installing or stuck</h3><ul class="list">${busy.map((g) => installRow(g, p)).join("")}</ul>` : ""}
      <details data-k="${p.id}" ${open.includes(p.id) ? "open" : ""}><summary class="muted">${done.length} game${done.length === 1 ? "" : "s"} installed</summary>
        <ul class="list">${done.map((g) => installRow(g, p)).join("") || `<li class="muted">None yet. Approve one in Games or Requests and it installs by itself.</li>`}</ul></details>
    </div>`;
  }).join("");
  $("#hubCard").innerHTML = hubCard(d.hub);
  const root = $("#pcs");
  wireInstalls(root, loadPcs);
  root.querySelectorAll("[data-cmd]").forEach((b) => (b.onclick = async () => {
    const verb = b.dataset.cmd === "restart" ? "Restart" : "Shut down";
    if (!await sheet({ title: `${verb} ${kidName(b.dataset.kid)}'s PC?`, text: "A 1-minute warning shows on the screen first. The kids also have their own Power button for this.", ok: verb, kind: "bad" })) return;
    await api(`/api/parent/kids/${b.dataset.kid}/commands`, { method: "POST", body: { kind: b.dataset.cmd } }); b.textContent = "Sent";
  }));
  root.querySelectorAll("[data-update]").forEach((b) => (b.onclick = async () => {
    const name = kidName(b.dataset.update);
    if (!await sheet({ title: `Install the update on ${name}'s PC now?`, text: "The PC signs out for about a minute, even in the middle of a game, and restarts.", ok: "Install now", kind: "bad" })) return;
    await api(`/api/parent/kids/${b.dataset.update}/commands`, { method: "POST", body: { kind: "install_app", arg: "kidspc" } }); b.textContent = "Sent";
  }));
  root.querySelectorAll("[data-autoupdate]").forEach((b) => (b.onclick = async () => {
    const on = b.dataset.on !== "1", name = kidName(b.dataset.autoupdate);
    const text = on ? "A new build goes in the moment it arrives, signing out for about a minute unless a game is running. Good for a test PC."
      : "A new build waits for the next restart or sign-in, so it never interrupts.";
    if (!await sheet({ title: on ? `Install updates on ${name}'s PC right away?` : `Wait for a restart on ${name}'s PC?`, text, ok: on ? "Right away" : "At the next restart" })) return;
    await api(`/api/parent/kids/${b.dataset.autoupdate}/auto-update`, { method: "PUT", body: { on } }); loadPcs();
  }));
  root.querySelectorAll("[data-installapp]").forEach((b) => (b.onclick = async () => {
    await api(`/api/parent/kids/${b.dataset.kid}/commands`, { method: "POST", body: { kind: "install_app", arg: b.dataset.installapp } }); b.textContent = "Retrying…";
  }));
  root.querySelectorAll("[data-namedev]").forEach((b) => (b.onclick = async () => {
    const name = await sheet({ title: `Name this device`, text: `Windows calls it ${b.dataset.namedev}. A name like "${kidName(b.dataset.kid)}'s laptop" is easier to spot.`,
      input: { value: b.dataset.name, placeholder: `${kidName(b.dataset.kid)}'s PC` }, ok: "Save" });
    if (name == null) return;
    try { await api(`/api/parent/devices/${b.dataset.kid}/name`, { method: "PUT", body: { computer: b.dataset.namedev, name: String(name) } }); } catch (err) { return oops(err); }
    loadPcs();
  }));
  root.querySelectorAll("[data-setup]").forEach((b) => (b.onclick = () => setupDevice(b.dataset.setup).catch(oops)));
  pcTimer = setTimeout(loadPcs, 10000);   // progress moves while the page is open
}

// Add a device: a one-time code, and the one line to paste on the new PC, with the steps spelled out.
async function setupDevice(kid) {
  const name = kidName(kid);
  const c = await api(`/api/parent/kids/${kid}/setup-code`, { method: "POST" });
  const when = new Date(c.expires_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const text = `This is done once per computer, on the new PC itself (a Windows desktop or laptop).\n\n` +
    `1. Sign in to Windows on that PC with its administrator account: the account made when Windows was first set up. This is not your Kids PC Hub sign-in.\n` +
    `2. Right-click the Start button and choose Terminal (Admin). Say Yes when Windows asks.\n` +
    `3. Paste the whole line below, starting with "irm", and press Enter.\n\n${c.command}\n\n` +
    `It installs everything, makes ${name}'s Windows account, and restarts into ${name}'s screen, in about 10 minutes. The line works until ${when}.`;
  if (await sheet({ title: `Add a device for ${name}`, text, ok: "Copy the line", cancel: "Close" })) await copyText(c.command);
}
$("#kidForm").onsubmit = async (e) => {
  e.preventDefault();
  const name = new FormData(e.target).get("name").trim();
  let k;
  try { k = await api("/api/parent/kids", { method: "POST", body: { name } }); } catch (err) { return oops(err); }
  e.target.reset();
  await refresh().catch(() => {});
  await loadPcs();
  if (await sheet({ title: `${k.name} is added`, text: `Next, set up ${k.name}'s computer. You can also do it later from this page with "Add a device".`, ok: "Set up a device now", cancel: "Later" }))
    setupDevice(k.id).catch(oops);
};

const STORE = { steam: "Steam", battlenet: "Battle.net", gamepass: "PC Game Pass", direct: "Installed by hand" };
// Where an approved game stands on one kid's PC, in a few words (the PCs tab has the detail and the fix).
const INSTALL_WORDS = { installed: "On his PC", queued: "Installing soon", downloading: "Installing", installing: "Installing",
  needs_signin: "Needs a sign-in", not_owned: "Not bought yet", no_space: "PC is full", failed: "Didn't install" };
function ago(iso) {
  if (!iso) return "";
  const days = Math.round((new Date().setHours(0, 0, 0, 0) - new Date(iso).setHours(0, 0, 0, 0)) / 864e5);
  return days <= 0 ? "today" : days === 1 ? "yesterday" : days < 7 ? new Date(iso).toLocaleDateString([], { weekday: "long" }) : `${days} days ago`;
}
// No store art (Battle.net, by hand): the game's first letter on a colour picked from its name.
function cover(g, cls = "cover") {
  if (g.image_url) return `<img class="${cls}" src="${esc(g.image_url)}" alt="" loading="lazy" data-l="${esc((g.title || "?")[0])}"
    onerror="this.replaceWith(Object.assign(document.createElement('div'), { className: this.className + ' noart', textContent: this.dataset.l }))">`;
  const hue = [...(g.title || "")].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 7);
  return `<div class="${cls} noart" style="--h:${hue}">${esc((g.title || "?")[0])}</div>`;
}
const games = { all: [], who: "", filter: "" };
async function loadGames() {
  games.all = await api("/api/parent/games");
  renderGames();
  loadIdeas().catch(() => {});
}
function renderGames() {
  $("#gameWho").innerHTML = [["", "Everyone"], ...state.kids.map((k) => [k.id, k.name])]
    .map(([id, name]) => `<button type="button" class="${games.who === id ? "on" : ""}" data-who="${esc(id)}">${esc(name)}</button>`).join("");
  document.querySelectorAll("[data-who]").forEach((b) => (b.onclick = () => { games.who = b.dataset.who; renderGames(); }));
  const f = games.filter.toLowerCase();
  let list = games.all.filter((g) => (!games.who || g.approved_for.includes(games.who)) && (!f || g.title.toLowerCase().includes(f)));
  if (games.who) list = [...list].sort((a, b) => (b.kids[games.who].last_played || "").localeCompare(a.kids[games.who].last_played || "") || a.title.localeCompare(b.title));
  const kidRow = (g, k) => {
    const on = g.approved_for.includes(k.id), st = g.kids[k.id] || {};
    const words = !on ? "Not approved" : [INSTALL_WORDS[st.install] || "", st.minutes ? `${fmtMin(st.minutes)} played` : "Not played yet",
      st.last_played ? `last ${ago(st.last_played)}` : ""].filter(Boolean).join(" · ");
    const tone = !on ? "off" : ["needs_signin", "not_owned", "no_space", "failed"].includes(st.install) ? "attn" : "";
    return `<li class="${tone}"><label><input type="checkbox" data-appr="${g.id}" data-kid="${k.id}" ${on ? "checked" : ""}> ${esc(k.name)}</label><span>${esc(words)}</span></li>`;
  };
  $("#games").innerHTML = list.map((g) => `<li class="game">${cover(g)}
      <div class="gbody"><b class="gtitle">${esc(g.title)}</b>
        <div class="meta">${g.rating ? `<span class="tagline rating">${esc(g.rating)}</span>` : ""} ${esc(STORE[g.launcher] || g.launcher)}</div>
        <ul class="perkid">${state.kids.map((k) => kidRow(g, k)).join("")}</ul>
        <button class="ghost small" data-delgame="${g.id}" data-title="${esc(g.title)}">Remove from catalogue</button></div></li>`).join("")
    || `<li class="muted">${games.all.length ? "No games match." : "No games yet. Search for one, or pick from the ideas below."}</li>`;
  document.querySelectorAll("[data-appr]").forEach((c) => (c.onchange = async () => {
    try { await api(`/api/parent/games/${c.dataset.appr}/kids/${c.dataset.kid}`, { method: "PUT", body: { approved: c.checked } }); } catch (err) { oops(err); }
    loadGames();
  }));
  document.querySelectorAll("[data-delgame]").forEach((b) => (b.onclick = async () => {
    if (!await sheet({ title: `Remove ${b.dataset.title}?`, text: "It comes off the catalogue for every kid, and they can't start it from their home screen any more. It stays installed on their PCs.", ok: "Remove", kind: "bad" })) return;
    try { await api(`/api/parent/games/${b.dataset.delgame}`, { method: "DELETE" }); } catch (err) { return oops(err); }
    loadGames();
  }));
}
$("#gameFilter").oninput = (e) => { games.filter = e.target.value.trim(); renderGames(); };

const CAP_WORDS = { off: "Don't show ideas", E: "Rated E only", "E10+": "Up to E10+", T: "Up to T", M: "Up to M" };
async function loadIdeas() {
  const d = await api("/api/parent/games/ideas");
  $("#ideaCaps").innerHTML = state.kids.map((k) => `<label>${esc(k.name)} sees
    <select data-cap="${esc(k.id)}">${d.choices.map((c) => `<option value="${c}" ${d.caps[k.id] === c ? "selected" : ""}>${CAP_WORDS[c] || c}</option>`).join("")}</select></label>`).join("");
  document.querySelectorAll("[data-cap]").forEach((s) => (s.onchange = async () => {
    try { await api(`/api/parent/kids/${s.dataset.cap}/ideas`, { method: "PUT", body: { rating: s.value } }); } catch (err) { oops(err); }
  }));
  const shelves = [];
  for (const i of d.ideas) { let sh = shelves.find((x) => x.title === i.shelf); if (!sh) shelves.push(sh = { title: i.shelf, ideas: [] }); sh.ideas.push(i); }
  $("#ideas").innerHTML = shelves.map((sh) => `<h3>${esc(sh.title)}</h3><ul class="ideagrid">${sh.ideas.map((i) => `<li>${cover(i, "icover")}
      <b>${esc(i.title)}</b><div class="meta">${i.esrb ? `<span class="tagline rating">${esc(i.esrb)}</span>` : ""} ${esc(i.store)}</div>
      <div class="who">${state.kids.map((k) => i.approved_for.includes(k.id)
        ? `<span class="yes">${esc(k.name)}</span>`
        : `<button type="button" class="small ghost" data-idea="${esc(i.ref)}" data-kid="${esc(k.id)}">+ ${esc(k.name)}</button>`).join("")}</div></li>`).join("")}</ul>`).join("");
  document.querySelectorAll("[data-idea]").forEach((b) => (b.onclick = async () => {
    b.disabled = true; b.textContent = "Adding…";
    try { await api("/api/parent/games/from-search", { method: "POST", body: { ref: b.dataset.idea, approved_for: [b.dataset.kid] } }); } catch (err) { b.disabled = false; return oops(err); }
    loadGames();
  }));
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

// Messages and calls: approved friends (standing links), what each has done lately, and invites -----------
const msgState = { kid: "" };
async function loadInvites() {
  const [inv, fr, keep, calls, chats] = await Promise.all([api("/api/parent/invites"), api("/api/parent/friends"), api("/api/parent/retention").catch(() => null),
    api(`/api/parent/calls?days=30${msgState.kid ? `&kid=${msgState.kid}` : ""}`).catch(() => []), api("/api/parent/chats")]);
  const K = msgState.kid, mineK = (ids) => !K || (ids || []).includes(K);
  $("#msgWho").innerHTML = [["", "Everyone"], ...state.kids.map((k) => [k.id, k.name])]
    .map(([id, name]) => `<button type="button" class="${K === id ? "on" : ""}" data-mwho="${esc(id)}">${esc(name)}</button>`).join("");
  $("#msgWho").querySelectorAll("[data-mwho]").forEach((b) => (b.onclick = () => { msgState.kid = b.dataset.mwho; loadInvites().catch(oops); }));

  // Chats: newest first, each with who is in it and what you can do.
  const live = fr;   // approved friends, for "Add someone"
  const convs = chats.convs.filter((c) => mineK(c.kid_ids || (c.kid_id ? [c.kid_id] : []))).filter((c) => c.count || c.active)
    .sort((a, b) => (b.last_at || "").localeCompare(a.last_at || ""));
  $("#chatList").innerHTML = convs.map((c) => {
    const people = c.kind === "family" ? "Mom, Dad and the kids" : c.kind === "kids" ? "Just the kids" : (c.people || []).join(", ");
    const lastLine = c.last ? `${esc(c.last.sender)}: ${esc((c.last.text || "").slice(0, 80))}` : "No messages yet";
    const group = c.kind === "friend" || c.kind === "group";
    return `<li><div class="split"><span><b>${esc(c.title)}</b> ${c.kind === "group" ? `<span class="tagline">Group</span>` : ""}${c.active ? "" : ` <span class="tagline bad">Ended</span>`}</span>
        <span class="muted">${c.last_at ? esc(ago(c.last_at)) : ""}</span></div>
      <div class="muted">${c.named || c.kind === "family" ? `${esc(people)} · ` : ""}${c.count} message${c.count === 1 ? "" : "s"}${c.pictures ? ` · ${c.pictures} picture${c.pictures > 1 ? "s" : ""}` : ""}</div>
      <div class="muted lastmsg">${lastLine}</div>
      <div class="actions"><button class="ghost small" data-readconv="${esc(c.id)}">Read chat</button>
        ${group && c.active ? `<button class="ghost small" data-addto="${esc(c.id)}">Add someone</button>` : ""}
        ${(group && c.active) || c.kind === "kids" ? `<button class="ghost small" data-rename="${esc(c.id)}" data-name="${esc(c.named ? c.title : "")}">Rename</button>` : ""}</div>
      ${(c.members || []).length ? `<div class="chips">${c.members.map((m) => `<span class="chip">${esc(m.name)} <button class="ghost small" data-unmember="${esc(c.id)}|${m.kind}|${esc(m.who)}" title="Take ${esc(m.name)} out of this chat">×</button></span>`).join("")}</div>` : ""}</li>`;
  }).join("") || `<li class="muted">No chats yet.</li>`;

  // Voice calls: who was on, who joined, how long.
  const t = (iso) => new Date(iso).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  $("#calls").innerHTML = calls.slice(0, 20).map((c) => `<li><div class="split"><span><b>${esc(kidName(c.kid_id))}</b> <span class="muted">${esc(c.room)}</span></span><span class="pill">${fmtMin(c.minutes)}</span></div>
      <div>${c.with.length ? `With ${esc(c.with.join(", "))}` : `<span class="muted">Nobody else joined</span>`}</div><div class="muted">${esc(t(c.started_at))}</div></li>`).join("")
    || `<li class="muted">No calls in the last 30 days${K ? ` for ${esc(kidName(K))}` : ""}.</li>`;

  // Approved friends
  const past = inv.filter((i) => i.revoked && mineK([i.kid_id])), links = Object.fromEntries(inv.map((i) => [i.code, i.link]));
  const days = keep ? keep.days.invites : 7;
  const last = (f) => [f.last_call ? `last call ${ago(f.last_call)}` : "no calls yet", f.messages ? `${f.messages} message${f.messages > 1 ? "s" : ""}, last ${ago(f.last_message)}` : "no messages yet"].join(" · ");
  $("#invites").innerHTML = fr.filter((f) => mineK([f.kid_id])).map((f) => `<li><div class="split"><span><b>${esc(f.friend_name)}</b> <span class="muted">with ${esc(kidName(f.kid_id))}</span></span>
      <span class="muted">since ${esc(ago(f.added))}</span></div>
    <div class="muted">${esc(last(f))}</div>
    <div class="actions"><button class="ghost small" data-readconv="${esc(f.conv)}">Read chat</button><button class="small" data-copy="${esc(links[f.code] || "")}">Copy link</button>
      <button class="ghost small" data-revoke="${esc(f.code)}" data-name="${esc(f.friend_name)}">Revoke</button></div></li>`).join("")
    || `<li class="muted">No approved friends${K ? ` for ${esc(kidName(K))}` : ""} yet. Invite one below.</li>`;
  if (past.length) $("#invites").insertAdjacentHTML("beforeend", `<li class="past"><details>
      <summary>Revoked (${past.length})</summary>
      <p class="muted">These links no longer work. ${days ? `They are deleted ${days === 1 ? "a day" : `${days} days`} after you revoke them.` : "They are kept until you clear them."}</p>
      <ul class="list">${past.map((i) => `<li><b>${esc(i.friend_name)}</b> with ${esc(kidName(i.kid_id))} <span class="tagline bad">Revoked</span>
        <div class="muted">${i.revoked_at ? `Revoked ${esc(localTime(i.revoked_at))}` : ""}</div></li>`).join("")}</ul>
      <button class="ghost small" id="clearInvites">Clear revoked links</button></details></li>`);
  if (K) $("#inviteKid").value = K;

  const panel = document.querySelector('[data-panel="messages"]');
  panel.querySelectorAll("[data-copy]").forEach((b) => (b.onclick = async () => { b.textContent = (await copyText(b.dataset.copy)) ? "Copied" : "Couldn't copy: press and hold the link"; }));
  panel.querySelectorAll("[data-readconv]").forEach((b) => (b.onclick = () => {
    if (wide.matches) { if (!document.body.classList.contains("chat-open")) $('[data-panel="chat"] > .title').click(); }
    else window.scrollTo(0, 0);
    switchConv(b.dataset.readconv);
  }));
  panel.querySelectorAll("[data-addto]").forEach((b) => (b.onclick = () => addToChat(chats.convs.find((c) => c.id === b.dataset.addto), live).catch(oops)));
  panel.querySelectorAll("[data-rename]").forEach((b) => (b.onclick = async () => {
    const name = await sheet({ title: "Name this chat", text: "Everyone in it sees the new name. Leave it empty to go back to the list of names.",
      input: { value: b.dataset.name, placeholder: "Minecraft crew" }, ok: "Save" });
    if (name == null) return;
    try { await api(`/api/parent/chats/${encodeURIComponent(b.dataset.rename)}/name`, { method: "PUT", body: { name } }); } catch (err) { return oops(err); }
    loadInvites(); loadConvs().catch(() => {});
  }));
  panel.querySelectorAll("[data-unmember]").forEach((b) => (b.onclick = async () => {
    const [conv, kind, who] = b.dataset.unmember.split("|");
    if (!await sheet({ title: "Take them out of this chat?", text: "They stop seeing it straight away. Their own chats aren't touched.", ok: "Take out", kind: "bad" })) return;
    await api(`/api/parent/chats/${encodeURIComponent(conv)}/members/${kind}/${encodeURIComponent(who)}`, { method: "DELETE" }); loadInvites();
  }));
  panel.querySelectorAll("[data-revoke]").forEach((b) => (b.onclick = async () => {
    if (!await sheet({ title: `Revoke ${b.dataset.name}'s link?`, text: "The link stops working straight away, they are dropped from a call they are in, and their chat ends. You can still read it.", ok: "Revoke", kind: "bad" })) return;
    await api(`/api/parent/invites/${b.dataset.revoke}`, { method: "DELETE" }); loadInvites();
  }));
  const clr = $("#clearInvites");
  if (clr) clr.onclick = async () => { await api("/api/parent/history/invites", { method: "DELETE" }); loadInvites(); };
}

// Add a kid at home or another approved friend to a friend's chat.
async function addToChat(c, friends) {
  if (!c) return;
  const inIt = new Set([...(c.kid_ids || []), ...(c.members || []).map((m) => m.who)]);
  const own = friends.find((f) => f.conv === c.id);
  const choices = [...state.kids.filter((k) => !inIt.has(k.id)).map((k) => ({ kind: "kid", who: k.id, label: k.name })),
    ...friends.filter((f) => f.conv !== c.id && !inIt.has(f.code) && (!own || f.code !== own.code)).map((f) => ({ kind: "friend", who: f.code, label: `${f.friend_name} (${kidName(f.kid_id)}'s friend)` }))];
  if (!choices.length) return sheet({ title: "Nobody else to add", text: "Everyone at home and every approved friend is already in this chat. Invite a friend first to add them.", cancel: "" });
  const box = $("#addPick");
  box.innerHTML = choices.map((x, i) => `<label class="pick"><input type="radio" name="addwho" value="${i}" ${i ? "" : "checked"}><span>${esc(x.label)}</span></label>`).join("");
  box.hidden = false;
  const ok = await sheet({ title: `Add someone to ${c.title}`, text: "They see what is said from now on, not what was said before. You can take them out again.", ok: "Add" });
  box.hidden = true;
  if (!ok) return;
  const x = choices[+box.querySelector("input:checked").value];
  try { await api(`/api/parent/chats/${encodeURIComponent(c.id)}/members`, { method: "POST", body: { kind: x.kind, who: x.who } }); } catch (err) { return oops(err); }
  loadInvites(); loadConvs().catch(() => {});
}
$("#inviteForm").onsubmit = async (e) => {
  e.preventDefault();
  const f = new FormData(e.target), names = [...new Set(f.get("friend_name").split(/[,\n]/).map((n) => n.trim()).filter(Boolean))].slice(0, 10);
  const made = [];
  try { for (const n of names) made.push(await api("/api/parent/invites", { method: "POST", body: { kid_id: f.get("kid_id"), friend_name: n } })); } catch (err) { oops(err); }
  e.target.reset(); fillKidSelects(); loadInvites();
  if (made.length > 1) sheet({ title: `${made.length} links made`, text: "Send each friend's parent their own link (Copy link on each). They all join the same call with " + kidName(f.get("kid_id")) + ".", cancel: "" });
};

// History and clean-up (Now tab) ---------------------------------------------
const keepLabel = (d) => (d === 0 ? "Forever" : d === 1 ? "1 day" : d === 365 ? "1 year" : `${d} days`);
async function loadRetention() {
  const r = await api("/api/parent/retention");
  $("#retention").innerHTML = r.kinds.map((k) => `<li class="split keep"><span>${esc(k.label)}</span><span class="inrow">
      <select data-keep="${k.kind}" aria-label="Keep ${esc(k.label)} for">${r.choices.map((d) => `<option value="${d}" ${d === k.days ? "selected" : ""}>${keepLabel(d)}</option>`).join("")}</select>
      <button class="ghost small" data-clearkind="${k.kind}" data-label="${esc(k.label)}">Clear</button></span></li>`).join("");
  document.querySelectorAll("[data-keep]").forEach((s) => (s.onchange = async () => {
    try { await api("/api/parent/retention", { method: "PUT", body: { days: { [s.dataset.keep]: +s.value } } }); } catch (err) { oops(err); }
    loadRetention();
  }));
  document.querySelectorAll("[data-clearkind]").forEach((b) => (b.onclick = async () => {
    if (!await sheet({ title: `Clear ${b.dataset.label.toLowerCase()}?`, text: "This deletes all of it now, for both kids. It can't be undone.", ok: "Clear", kind: "bad" })) return;
    try { const r = await api(`/api/parent/history/${b.dataset.clearkind}`, { method: "DELETE" }); b.textContent = `Cleared ${r.deleted}`; b.disabled = true; } catch (err) { oops(err); }
  }));
}
$("#historyCard").ontoggle = () => { if ($("#historyCard").open) loadRetention().catch(oops); };

$("#needs").onclick = (e) => { if (e.target.closest("[data-go]")) go("requests"); };
refresh().catch(() => {});
setInterval(() => { if (!$("#app").hidden && !$('[data-panel="home"]').hidden) refresh().catch(() => {}); }, 10000);
// Requests open on the screen: a new ask from a kid shows up by itself, without wiping a game a parent is choosing.
setInterval(async () => {
  if ($("#app").hidden || $('[data-panel="requests"]').hidden || document.hidden) return;
  try {
    const ids = (await api("/api/parent/requests")).map((r) => r.id).join(",");
    if (ids !== state.shownReqs) { loadRequests().catch(() => {}); refresh().catch(() => {}); }
  } catch {}
}, 10000);

// Playtimes -----------------------------------------------------------------
const DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const pt = { data: null, kid: null, day: 0 };

// "15:30" -> "3:30 PM" in the phone's own style.
const clock = (hhmm) => {
  if (hhmm === "24:00") return "midnight";
  const [h, m] = hhmm.split(":").map(Number);
  return new Date(2000, 0, 1, h, m).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
};
function nextPlay(iso) {
  const at = new Date(iso), days = Math.round((new Date(at).setHours(0, 0, 0, 0) - new Date().setHours(0, 0, 0, 0)) / 864e5);
  const t = at.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return days <= 0 ? `at ${t}` : days === 1 ? `tomorrow at ${t}` : `${at.toLocaleDateString([], { weekday: "long" })} at ${t}`;
}

// Screen time: the usual week, and changes for single dates shown on a calendar week ---------------
const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const parseDay = (s) => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };
const shortDate = (s) => parseDay(s).toLocaleDateString([], { month: "short", day: "numeric" });
function daysBetween(a, b) {
  const out = [], end = parseDay(b);
  for (let d = parseDay(a); d <= end && out.length < 62; d.setDate(d.getDate() + 1)) out.push(iso(d));
  return out;
}

// Skylight: chore points from the family calendar, spent by the kids on extra time --------------
async function loadSkylight(pending = null) {
  let d;
  try { d = await api("/api/parent/skylight"); } catch { $("#skylight").innerHTML = ""; return; }
  const box = $("#skylight");
  if (!d.linked) {
    box.innerHTML = `<div class="card"><h2>Skylight chore points</h2>
      <p class="muted">Link your Skylight calendar and the kids see their chore points on their PC, and can spend them on extra time. Your password goes to Skylight only; the hub keeps a sign-in token, not the password.</p>
      ${pending ? `<form id="skyCode"><label>Skylight emailed you a 6-digit code <input name="code" inputmode="numeric" autocomplete="one-time-code" required></label>
        <button class="wide">Finish linking</button></form>`
      : `<form id="skySignIn"><label>Skylight email <input name="email" type="email" autocomplete="username" required></label>
        <label>Password <input name="password" type="password" autocomplete="current-password" required></label>
        <button class="wide">Link Skylight</button></form>`}</div>`;
    const done = (r) => (r.code_needed ? loadSkylight(r.code_needed) : loadSkylight());
    if ($("#skySignIn")) $("#skySignIn").onsubmit = async (e) => {
      e.preventDefault(); const f = new FormData(e.target), b = e.target.querySelector("button");
      b.disabled = true; b.textContent = "Linking…";
      try { done(await api("/api/parent/skylight/signin", { method: "POST", body: { email: f.get("email"), password: f.get("password") } })); }
      catch (err) { b.disabled = false; b.textContent = "Link Skylight"; oops(err); }
    };
    if ($("#skyCode")) $("#skyCode").onsubmit = async (e) => {
      e.preventDefault();
      try { done(await api("/api/parent/skylight/code", { method: "POST", body: { id: pending, code: new FormData(e.target).get("code") } })); } catch (err) { oops(err); }
    };
    return;
  }
  const opts = (sel) => `<option value="">Not linked</option>` + d.people.map((p) => `<option value="${esc(p.id)}" ${p.id === sel ? "selected" : ""}>${esc(p.name)}</option>`).join("");
  box.innerHTML = `<div class="card skylight"><h2 class="split">Skylight chore points <button class="ghost small" data-skyunlink="1">Unlink</button></h2>
    <p class="muted">Each kid sees his points next to his time, and can buy extra time with them straight away, up to the daily limit.</p>
    ${d.error ? `<div class="alert"><span>${esc(d.error)}</span></div>` : ""}
    ${d.frames.length > 1 ? `<label>Household <select id="skyFrame">${d.frames.map((f) => `<option value="${esc(f.id)}" ${f.id === d.frame_id ? "selected" : ""}>${esc(f.name)}</option>`).join("")}</select></label>` : ""}
    <ul class="list">${state.kids.map((k) => { const m = d.members[k.id], bal = m ? d.balances[m] : null;
      return `<li class="split"><span><b>${esc(k.name)}</b> <span class="muted">${bal != null ? `${bal} points` : m ? "points not read yet" : ""}${d.bought_today?.[k.id] ? ` · bought ${fmtMin(d.bought_today[k.id])} today` : ""}</span></span>
        <select data-skykid="${esc(k.id)}" aria-label="${esc(k.name)} in Skylight">${opts(m)}</select></li>`; }).join("")}</ul>
    <div class="row prices">
      <label><input id="skyPts" type="number" min="1" value="${d.price_points}"> points buy</label>
      <label><input id="skyMin" type="number" min="5" max="120" step="5" value="${d.price_minutes}"> minutes</label>
      <label>Up to <input id="skyCap" type="number" min="0" max="240" step="5" value="${d.daily_cap}"> min a day</label>
    </div></div>`;
  const save = async (body) => { try { await api("/api/parent/skylight", { method: "PUT", body }); } catch (err) { oops(err); } loadSkylight(); };
  box.querySelectorAll("[data-skykid]").forEach((s) => (s.onchange = () => save({ members: Object.fromEntries([...box.querySelectorAll("[data-skykid]")].map((x) => [x.dataset.skykid, x.value])) })));
  if ($("#skyFrame")) $("#skyFrame").onchange = (e) => save({ frame_id: e.target.value, members: {} });
  ["#skyPts", "#skyMin", "#skyCap"].forEach((id) => ($(id).onchange = () => save({ price_points: +$("#skyPts").value, price_minutes: +$("#skyMin").value, daily_cap: +$("#skyCap").value })));
  box.querySelector("[data-skyunlink]").onclick = async () => {
    if (!await sheet({ title: "Unlink Skylight?", text: "The kids stop seeing their points. Nothing changes in Skylight.", ok: "Unlink", kind: "bad" })) return;
    await api("/api/parent/skylight", { method: "DELETE" }); loadSkylight();
  };
}
async function loadPlaytimes() { return loadScreenTime(); }
async function loadScreenTime() {
  const [w, p] = await Promise.all([api(`/api/parent/screentime${pt.start ? `?start=${pt.start}` : ""}`), api("/api/parent/playtimes")]);
  pt.st = w; pt.data = p; pt.start = w.start;
  if (!pt.kid || !p.kids.some((k) => k.id === pt.kid)) pt.kid = p.kids[0]?.id;
  renderPlaytimes();
  renderWeek();
}
function dayFacts(d) {
  const wins = d.windows.length ? d.windows.map((x) => `${clock(x.start)}–${clock(x.end)}`).join(", ") : d.bedtime ? "Any time before bed" : "Any time";
  return d.daily_minutes ? `<span class="lim">${fmtMin(d.daily_minutes)}</span><span>${esc(wins)}</span><span>${d.bedtime ? `Bed ${esc(clock(d.bedtime))}` : "No bedtime"}</span>`
    : `<span class="lim">No play</span>`;
}
function renderWeek() {
  const k = pt.st.kids.find((x) => x.id === pt.kid);
  if (!k) { $("#stWeek").innerHTML = ""; return; }
  const mon = parseDay(pt.st.start), sun = parseDay(pt.st.start); sun.setDate(sun.getDate() + 6);
  $("#stWeekName").textContent = `${mon.toLocaleDateString([], { month: "short", day: "numeric" })} – ${sun.toLocaleDateString([], { month: "short", day: "numeric" })}`;
  $("#stToday").hidden = pt.st.start <= pt.st.today && pt.st.today <= iso(sun);
  $("#stWeek").innerHTML = k.week.map((d) => `<button class="card day ${d.changed ? "changed" : ""} ${d.date === pt.st.today ? "today" : ""} ${d.date < pt.st.today ? "past" : ""}" data-stday="${d.date}">
      <div class="dayhead"><b>${DAY_NAMES[d.weekday].slice(0, 3)} ${parseDay(d.date).getDate()}</b>${d.date === pt.st.today ? `<span class="tagline">Today</span>` : ""}</div>
      ${d.changed ? `<div class="tag">${esc(d.label || "Changed")}</div>` : ""}
      <div class="facts">${dayFacts(d)}</div></button>`).join("");
  document.querySelectorAll("[data-stday]").forEach((b) => (b.onclick = () => editDate(b.dataset.stday)));
  $("#stUpcoming").innerHTML = k.upcoming.length ? `<div class="card"><h2>Coming up</h2><ul class="list">${k.upcoming.map((u) =>
      `<li class="split"><span><b>${esc(u.label || "Changed")}</b> <span class="muted">${u.days > 1 ? `${shortDate(u.from)} to ${shortDate(u.to)}` : shortDate(u.from)} · ${u.daily_minutes ? `${fmtMin(u.daily_minutes)} a day` : "no play"}</span></span>
       <span class="inrow"><button class="ghost small" data-upedit="${u.from}">Change</button><button class="ghost small" data-upclear="${u.from}|${u.to}">Remove</button></span></li>`).join("")}</ul></div>` : "";
  document.querySelectorAll("[data-upedit]").forEach((b) => (b.onclick = () => { const u = k.upcoming.find((x) => x.from === b.dataset.upedit); editDate(u.from, u.to); }));
  document.querySelectorAll("[data-upclear]").forEach((b) => (b.onclick = async () => {
    const [a, z] = b.dataset.upclear.split("|");
    if (!await sheet({ title: "Back to the usual week?", text: `${k.name}'s ${a === z ? shortDate(a) : `${shortDate(a)} to ${shortDate(z)}`} goes back to the usual rules.`, ok: "Remove" })) return;
    try { await api(`/api/parent/screentime/${k.id}/dates/clear`, { method: "POST", body: { dates: daysBetween(a, z) } }); } catch (err) { return oops(err); }
    loadScreenTime().catch(oops); refresh().catch(() => {});
  }));
}
const moveWeek = (n) => { const d = parseDay(pt.st.start); d.setDate(d.getDate() + 7 * n); pt.start = iso(d); loadScreenTime().catch(oops); };
$("#stPrev").onclick = () => moveWeek(-1);
$("#stNext").onclick = () => moveWeek(1);
$("#stToday").onclick = () => { pt.start = ""; loadScreenTime().catch(oops); };
$("#stAdd").onclick = () => editDate(pt.st.today);
$("#stCopy").onclick = async () => {
  const name = kidName(pt.kid);
  if (!await sheet({ title: `Copy this week to next week for ${name}?`, text: `Next week's days become the same as this week's, as changes for those dates. The usual week stays as it is, and takes over again after.`, ok: "Copy" })) return;
  try { await api(`/api/parent/screentime/${pt.kid}/copy-week`, { method: "POST", body: { start: pt.st.start, weeks: 1 } }); } catch (err) { return oops(err); }
  moveWeek(1);
};

// Consoles: a linked Nintendo Switch shares the same daily time ------------------
async function loadConsoles() {
  let c;
  try { c = await api("/api/parent/consoles"); } catch { $("#consoles").innerHTML = ""; return; }   // turned off in config.yaml
  renderConsoles(c);
}
function renderConsoles(c) {
  const box = $("#consoles"), n = c.nintendo;
  const kidOpts = (sel, none) => `<option value="">${none}</option>` + c.kids.map((k) => `<option value="${k.id}" ${k.id === sel ? "selected" : ""}>${esc(k.name)}</option>`).join("");
  if (!n) {
    box.innerHTML = `<div class="card"><h2>Nintendo Switch</h2>
      <p class="muted">Link the Nintendo account you use in the Nintendo Switch Parental Controls app. Switch play then comes out of the same daily time as the PC, and the Switch stops the game when that time runs out.</p>
      <button data-nconnect="1">Connect Nintendo account</button></div>`;
  } else {
    const synced = n.synced_at ? new Date(n.synced_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "not yet";
    box.innerHTML = `<div class="card"><h2 class="split">Nintendo Switch <button class="ghost small" data-nsync="1">Check now</button></h2>
      <p class="muted">Linked as ${esc(n.label)}. Last checked ${esc(synced)}. Switch play reaches the hub about every 15 minutes.</p>
      ${n.error ? `<div class="alert"><span>${esc(n.error)}</span>${/again/.test(n.error) ? `<button class="bad small" data-nconnect="1">Connect</button>` : ""}</div>` : ""}
      ${c.devices.map((d) => `<div class="card day">
        <div class="dayhead"><b>${esc(d.name)}</b><span class="tagline">${esc(d.model)}</span></div>
        <label class="muted">Daily limit on this Switch
          <select data-nlimit="${esc(d.device_id)}">${kidOpts(d.limit_kid, "Leave it to Nintendo's app")}</select></label>
        ${d.limit_kid ? `<p><label class="check" style="white-space:normal"><input type="checkbox" data-nstop="${esc(d.device_id)}" ${d.stop_game ? "checked" : ""}> Stop the game when time is up, not just an alarm</label></p>
          <div class="muted">${d.extra_time ? "Bonus time from Nintendo's app is running, so the hub waits until it ends." : d.limit_now != null ? `Limit today: ${fmtMin(d.limit_now)} on this Switch.` : ""}</div>` : ""}
        <ul class="list">${d.players.map((p) => `<li class="split"><span>${esc(p.nickname)} <span class="muted">${fmtMin(p.minutes)} today</span></span>
          <select data-nplayer="${esc(p.player_id)}" data-dev="${esc(d.device_id)}" aria-label="Whose time is ${esc(p.nickname)}">${kidOpts(p.kid_id, "Not counted")}</select></li>`).join("")}</ul>
      </div>`).join("") || `<p class="muted">No Switch found yet. Set one up in the Parental Controls app first.</p>`}
      <button class="ghost small" data-nunlink="1">Unlink</button></div>`;
  }
  box.insertAdjacentHTML("beforeend", xboxCard(c, kidOpts));
  box.querySelectorAll("[data-xconnect]").forEach((b) => (b.onclick = () => connectXbox().catch(oops)));
  const saveMember = async (id) => {
    const kid = box.querySelector(`[data-xkid="${CSS.escape(id)}"]`).value, lock = box.querySelector(`[data-xlock="${CSS.escape(id)}"]`);
    renderConsoles(await api(`/api/parent/consoles/xbox/members/${encodeURIComponent(id)}`, { method: "PUT", body: { kid_id: kid, enforce: lock ? lock.checked : true } }));
    refresh();
  };
  box.querySelectorAll("[data-xkid]").forEach((s) => (s.onchange = () => saveMember(s.dataset.xkid).catch(oops)));
  box.querySelectorAll("[data-xlock]").forEach((s) => (s.onchange = () => saveMember(s.dataset.xlock).catch(oops)));
  box.querySelectorAll("[data-xunlink]").forEach((b) => (b.onclick = async () => {
    if (!(await sheet({ title: "Unlink Xbox?", text: "Xbox play stops counting toward the PC, and any Xbox block the hub set is lifted.", ok: "Unlink", kind: "bad" }))) return;
    await api("/api/parent/consoles/xbox", { method: "DELETE" }).catch(oops);
    loadConsoles();
  }));
  box.querySelectorAll("[data-nconnect]").forEach((b) => (b.onclick = () => connectNintendo().catch(oops)));
  box.querySelectorAll("[data-nsync]").forEach((b) => (b.onclick = async () => renderConsoles(await api("/api/parent/consoles/sync", { method: "POST" }).catch(oops) || c)));
  box.querySelectorAll("[data-nplayer]").forEach((s) => (s.onchange = async () => {
    renderConsoles(await api(`/api/parent/consoles/nintendo/devices/${encodeURIComponent(s.dataset.dev)}/players/${encodeURIComponent(s.dataset.nplayer)}`, { method: "PUT", body: { kid_id: s.value } }));
    refresh();
  }));
  const saveDev = async (id) => {
    const lim = box.querySelector(`[data-nlimit="${CSS.escape(id)}"]`).value, stop = box.querySelector(`[data-nstop="${CSS.escape(id)}"]`);
    renderConsoles(await api(`/api/parent/consoles/nintendo/devices/${encodeURIComponent(id)}`, { method: "PUT", body: { limit_kid: lim, stop_game: stop ? stop.checked : true } }));
  };
  box.querySelectorAll("[data-nlimit]").forEach((s) => (s.onchange = () => saveDev(s.dataset.nlimit).catch(oops)));
  box.querySelectorAll("[data-nstop]").forEach((s) => (s.onchange = () => saveDev(s.dataset.nstop).catch(oops)));
  box.querySelectorAll("[data-nunlink]").forEach((b) => (b.onclick = async () => {
    if (!(await sheet({ title: "Unlink Nintendo?", text: "Switch play stops counting toward the PC. Each Switch keeps today's limit; change it in Nintendo's app if you need to.", ok: "Unlink", kind: "bad" }))) return;
    await api("/api/parent/consoles/nintendo", { method: "DELETE" }).catch(oops);
    loadConsoles();
  }));
}
function xboxCard(c, kidOpts) {
  const x = c.xbox;
  if (!x) return `<div class="card"><h2>Xbox</h2>
    <p class="muted">Link the Microsoft account you use in the Family Safety app (the family organizer). Xbox play then comes out of the same daily time as the PC, and the hub locks his Xbox when that time runs out.</p>
    <button data-xconnect="1">Connect Microsoft account</button></div>`;
  const synced = x.synced_at ? new Date(x.synced_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "not yet";
  const rows = x.members.map((m) => `<li><div class="split"><span>${esc(m.nickname)} <span class="muted">${m.role === "Admin" ? "organizer" : `${fmtMin(m.minutes)} on Xbox today`}${m.blocked ? " · Xbox locked" : ""}</span></span>
      <select data-xkid="${esc(m.player_id)}" aria-label="Whose time is ${esc(m.nickname)}">${kidOpts(m.kid_id, "Not counted")}</select></div>
      ${m.kid_id ? `<label class="check" style="white-space:normal"><input type="checkbox" data-xlock="${esc(m.player_id)}" ${m.enforce ? "checked" : ""}> Lock his Xbox when his time is up</label>` : ""}</li>`).join("");
  return `<div class="card"><h2 class="split">Xbox <button class="ghost small" data-nsync="1">Check now</button></h2>
    <p class="muted">Linked ${esc(x.linked_by ? `by ${x.linked_by}` : "")}. Last checked ${esc(synced)}. Xbox time can take a few minutes to show up, so a lock may come a few minutes late.</p>
    ${x.error ? `<div class="alert"><span>${esc(x.error)}</span>${/again/.test(x.error) ? `<button class="bad small" data-xconnect="1">Connect</button>` : ""}</div>` : ""}
    <ul class="list">${rows || `<li class="muted">No one in this Microsoft family has Family Safety turned on yet.</li>`}</ul>
    <button class="ghost small" data-xunlink="1">Unlink</button></div>`;
}
async function connectXbox() {
  const { login_url } = await api("/api/parent/consoles/xbox/start", { method: "POST" });
  window.open(login_url, "_blank", "noopener");
  const link = await sheet({
    title: "Paste the Microsoft address",
    text: "Sign in on the page that just opened with the family organizer's Microsoft account. You'll end on a blank page: copy its whole address (it contains \"code=\") and paste it here.",
    input: "https://login.live.com/oauth20_desktop.srf?code=…", ok: "Link",
  });
  if (!link) return;
  renderConsoles(await api("/api/parent/consoles/xbox/finish", { method: "POST", body: { link } }));
}
async function connectNintendo() {
  const { login_url } = await api("/api/parent/consoles/nintendo/start", { method: "POST" });
  window.open(login_url, "_blank", "noopener");
  const link = await sheet({
    title: "Paste the Nintendo link",
    text: "Sign in on the page that just opened. On \"Linking an external account\", press and hold the red \"Select this person\" button, copy the link, and paste it here.",
    input: "npf54789befb391a838://auth#…", ok: "Link",
  });
  if (!link) return;
  renderConsoles(await api("/api/parent/consoles/nintendo/finish", { method: "POST", body: { link } }));
}

function renderPlaytimes() {
  const kids = pt.data.kids, kid = kids.find((k) => k.id === pt.kid);
  $("#ptKids").hidden = kids.length < 2;
  $("#ptKids").innerHTML = kids.map((k) => `<button role="tab" aria-selected="${k.id === pt.kid}" class="${k.id === pt.kid ? "on" : ""}" data-ptkid="${k.id}">${esc(k.name)}</button>`).join("");
  document.querySelectorAll("[data-ptkid]").forEach((b) => (b.onclick = () => { pt.kid = b.dataset.ptkid; renderPlaytimes(); renderWeek(); }));
  if (!kid) { $("#ptDays").innerHTML = `<p class="muted">No kids set up yet.</p>`; return; }
  $("#ptDays").innerHTML = kid.days.map((d) => {
    const wins = d.windows.length
      ? d.windows.map((w) => `<span class="win">${esc(clock(w.start))} – ${esc(clock(w.end))}</span>`).join("")
      : `<span class="win any">Any time${d.bedtime ? " before bedtime" : ""}</span>`;
    const limit = d.daily_minutes ? `${fmtMin(d.daily_minutes)} a day` : "No play";
    return `<button class="card day ${d.day === pt.data.today ? "today" : ""}" data-ptday="${d.day}" aria-label="Edit ${DAY_NAMES[d.day]}">
      <div class="dayhead"><b>${DAY_NAMES[d.day]}</b>${d.day === pt.data.today ? `<span class="tagline">Today</span>` : ""}<span class="edit">Edit</span></div>
      <div class="facts"><span>${limit}</span><span>${d.bedtime ? `Bedtime ${esc(clock(d.bedtime))}` : "No bedtime"}</span></div>
      ${d.daily_minutes ? `<div class="wins">${wins}</div>` : ""}
    </button>`;
  }).join("");
  document.querySelectorAll("[data-ptday]").forEach((b) => (b.onclick = () => editPlaytime(+b.dataset.ptday)));
}

function windowRow(w = { start: "15:30", end: "17:30" }) {
  return `<div class="winrow"><input type="time" data-w="start" value="${esc(w.start)}" required aria-label="From">
    <span>to</span><input type="time" data-w="end" value="${esc(w.end === "24:00" ? "00:00" : w.end)}" required aria-label="Until">
    <button type="button" class="ghost small" data-delwin aria-label="Remove this play time">✕</button></div>`;
}
function wireWindows() {
  const box = $("#ptWindows");
  if (!box.querySelector(".winrow")) box.innerHTML = `<p class="hint">None: they can play any time from 6 AM until bedtime.</p>`;
  box.querySelectorAll("[data-delwin]").forEach((b) => (b.onclick = () => { b.closest(".winrow").remove(); wireWindows(); }));
}

// One editor for both: a weekday of the usual week, or a run of dates (holiday, vacation).
// Closing the editor (Save or Cancel) leaves no day looking picked.
$("#ptSheet").addEventListener("close", () => { if (document.activeElement && document.activeElement.closest("#stWeek")) document.activeElement.blur(); });
function editDate(from, to = from) {
  const kid = pt.data.kids.find((k) => k.id === pt.kid), wk = pt.st.kids.find((k) => k.id === pt.kid);
  const d = wk.week.find((x) => x.date === from) || kid.days[parseDay(from).getDay() === 0 ? 6 : parseDay(from).getDay() - 1];
  fillEditor(kid, d, "date");
  const f = $("#ptForm");
  f.from.value = from; f.to.value = to; f.label.value = d.label || "";
  $("#ptUsual").hidden = !d.changed;
  $("#ptTitle").textContent = `${kid.name}: ${parseDay(from).toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" })}`;
  $("#ptSheet").showModal();
}
function editPlaytime(day) {
  const kid = pt.data.kids.find((k) => k.id === pt.kid);
  fillEditor(kid, kid.days[day], "week", day);
  $("#ptTitle").textContent = `${kid.name}'s usual ${DAY_NAMES[day]}`;
  $("#ptSheet").showModal();
}
function fillEditor(kid, d, mode, day = 0) {
  const f = $("#ptForm");
  pt.mode = mode; pt.day = day;
  $("#ptDateBox").hidden = mode !== "date"; $("#ptWeekBox").hidden = mode !== "week";
  f.from.required = f.to.required = mode === "date";
  f.daily_minutes.value = d.daily_minutes;
  $("#ptQuick").innerHTML = [0, 30, 60, 90, 120, 180].map((m) => `<button type="button" class="ghost small" data-quick="${m}">${m ? fmtMin(m) : "No play"}</button>`).join("");
  $("#ptQuick").querySelectorAll("[data-quick]").forEach((b) => (b.onclick = () => (f.daily_minutes.value = b.dataset.quick)));
  f.bedtime.value = d.bedtime || "20:30"; f.nobed.checked = !d.bedtime; f.bedtime.disabled = !d.bedtime;
  f.nobed.onchange = () => (f.bedtime.disabled = f.nobed.checked);
  $("#ptWindows").innerHTML = d.windows.map(windowRow).join(""); wireWindows();
  $("#ptDaysPick").innerHTML = DAY_NAMES.map((n, i) => `<label class="pick"><input type="checkbox" name="day" value="${i}" ${i === day ? "checked" : ""}><span>${n.slice(0, 3)}</span></label>`).join("");
  $("#ptKidsPick").innerHTML = pt.data.kids.length < 2 ? "" : pt.data.kids.map((k) => `<label class="pick"><input type="checkbox" name="kid" value="${k.id}" ${k.id === kid.id ? "checked" : ""}><span>${esc(k.name)}</span></label>`).join("");
  $("#ptErr").textContent = "";
}
$("#ptUsual").onclick = async () => {
  const f = $("#ptForm"), kids = [...f.querySelectorAll('[name=kid]:checked')].map((c) => c.value);
  try { for (const k of kids.length ? kids : [pt.kid]) await api(`/api/parent/screentime/${k}/dates/clear`, { method: "POST", body: { dates: daysBetween(f.from.value, f.to.value || f.from.value) } }); }
  catch (err) { $("#ptErr").textContent = err.message; return; }
  $("#ptSheet").close(); loadScreenTime().catch(oops); refresh().catch(() => {});
};
$("#ptAdd").onclick = () => {
  const box = $("#ptWindows"), last = [...box.querySelectorAll('[data-w="end"]')].pop();
  if (!box.querySelector(".winrow")) box.innerHTML = "";
  const start = last ? last.value : "15:30", [h, m] = start.split(":").map(Number);
  box.insertAdjacentHTML("beforeend", windowRow({ start, end: `${String(Math.min(23, h + 1)).padStart(2, "0")}:${String(m).padStart(2, "0")}` }));
  wireWindows();
};
$("#ptDayQuick").querySelectorAll("[data-pick]").forEach((b) => (b.onclick = () => {
  const want = b.dataset.pick.split(",");
  $("#ptDaysPick").querySelectorAll("input").forEach((c) => (c.checked = want.includes(c.value) || +c.value === pt.day));
}));
$("#ptSave").onclick = async (e) => {
  e.preventDefault();
  const f = $("#ptForm");
  if (!f.reportValidity()) return;
  const windows = [...$("#ptWindows").querySelectorAll(".winrow")].map((r) => {
    const end = r.querySelector('[data-w="end"]').value;
    return { start: r.querySelector('[data-w="start"]').value, end: end === "00:00" ? "24:00" : end };
  });
  const body = {
    days: [...f.querySelectorAll('[name=day]:checked')].map((c) => +c.value),
    daily_minutes: +f.daily_minutes.value, bedtime: f.nobed.checked ? "" : f.bedtime.value, windows,
  };
  if (!body.days.length) body.days = [pt.day];
  const sorted = [...windows].sort((a, b) => (a.start < b.start ? -1 : 1));
  const span = (w) => `${clock(w.start)} to ${clock(w.end)}`;
  const backwards = sorted.find((w) => w.end <= w.start);
  if (backwards) { $("#ptErr").textContent = `The play time ${span(backwards)} ends before it starts.`; return; }
  const clash = sorted.findIndex((w, i) => i && w.start < sorted[i - 1].end);
  if (clash > 0) { $("#ptErr").textContent = `Two play times overlap: ${span(sorted[clash - 1])} and ${span(sorted[clash])}.`; return; }
  const kids = [...f.querySelectorAll('[name=kid]:checked')].map((c) => c.value);
  try {
    if (pt.mode === "date") {
      if (f.to.value < f.from.value) { $("#ptErr").textContent = "The last date is before the first."; return; }
      const dates = daysBetween(f.from.value, f.to.value);
      if (dates.length >= 62) { $("#ptErr").textContent = "Pick up to two months at a time."; return; }
      for (const k of kids.length ? kids : [pt.kid]) await api(`/api/parent/screentime/${k}/dates`, { method: "PUT", body: { ...body, dates, label: f.label.value.trim() } });
    } else {
      for (const k of kids.length ? kids : [pt.kid]) await api(`/api/parent/playtimes/${k}`, { method: "PUT", body });
    }
  } catch (err) { $("#ptErr").textContent = err.message; return; }
  $("#ptSheet").close();
  loadScreenTime().catch(oops); refresh().catch(() => {});
};

// Chat ----------------------------------------------------------------------
// Conversations: the family chat (parents write here), the kids' own chat ("Kids at home"), each friend's chat (read only),
// and "Pictures & links", every picture and link sent in any of them. Parents can remove a message: the kids
// and the friend then see that a parent removed it, and the parents keep seeing what it was.
const chat = { conv: "family", last: 0, seen: {}, convs: [], started: false };
// On a phone chat is a tab. On a laptop (same 900px as app.css) it is a bar docked in the bottom right corner
// that opens over whatever tab is showing: body.chat-open while it is open, body.chat-unread for the red dot.
const wide = window.matchMedia("(min-width: 900px)");
function chatOpen() {
  if ($("#app").hidden) return false;
  return wide.matches ? document.body.classList.contains("chat-open") : !$('[data-panel="chat"]').hidden;
}
const pageTitle = document.title;
// A short two-note sound for a new chat message. Browsers only allow sound after the page has been tapped or
// clicked once, so a dashboard that was only ever looked at stays silent; the dots and the tab title still show.
function chatSound() {
  try {
    const ctx = chatSound.ctx || (chatSound.ctx = new (window.AudioContext || window.webkitAudioContext)());
    [[660, 0], [880, 0.16]].forEach(([hz, at]) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.frequency.value = hz; o.connect(g); g.connect(ctx.destination);
      g.gain.setValueAtTime(0.0001, ctx.currentTime + at);
      g.gain.exponentialRampToValueAtTime(0.15, ctx.currentTime + at + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + at + 0.3);
      o.start(ctx.currentTime + at); o.stop(ctx.currentTime + at + 0.32);
    });
  } catch (e) { /* no sound on this browser */ }
}
// The tab title says how many conversations have something new, so it shows with the dashboard in a background tab.
function chatTitle() {
  const n = chat.convs.filter((c) => c.last_id > (chat.seen[c.id] || 0) && !(chatOpen() && c.id === chat.conv)).length;
  document.title = n ? `(${n}) New message \u00B7 ${pageTitle}` : pageTitle;
}
function chatSeen() {
  const c = chat.convs.find((x) => x.id === chat.conv);
  if (c) chat.seen[c.id] = c.last_id;
  if (chat.conv === "family" || !chat.convs.length) chat.seen.family = Math.max(chat.seen.family || 0, chat.last);
  updateChatDot(); renderConvs(); chatTitle();
}
// The red dot is for the family chat, where messages are for the parents. The other chats get a dot on their button.
function updateChatDot() {
  const fam = chat.convs.find((c) => c.id === "family");
  const unread = fam && fam.last_id > (chat.seen.family || 0) && !(chatOpen() && chat.conv === "family");
  $("#chatDot").textContent = unread ? "●" : "";
  document.body.classList.toggle("chat-unread", !!unread);
}
{
  const bar = $('[data-panel="chat"] > .title');
  bar.tabIndex = 0; bar.setAttribute("role", "button");
  const toggle = () => {
    if (!wide.matches) return;
    const open = document.body.classList.toggle("chat-open");
    bar.setAttribute("aria-expanded", open);
    if (!open) return;
    chatSeen(); $("#chatLog").scrollTop = $("#chatLog").scrollHeight; if (chat.conv === "family") $("#chatForm").text.focus();
    loadChat().catch(() => {}); loadConvs().catch(() => {});
  };
  bar.onclick = toggle;
  bar.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } };
  // Window dragged from phone width to laptop width while on the Chat tab: that tab is gone, so go to Now.
  wide.addEventListener("change", () => { if ($('nav button[data-tab="messages"]').classList.contains("on")) go("messages"); });
}

async function loadConvs() {
  const r = await api("/api/parent/chats");
  const first = !chat.convs.length;
  chat.convs = r.convs;
  for (const c of r.convs) if (first || !(c.id in chat.seen)) chat.seen[c.id] = Math.max(chat.seen[c.id] || 0, c.last_id);   // old messages aren't new
  // A sound once per new message in a conversation that isn't open in front of the parent (any chat, not only Family).
  chat.told = chat.told || {};
  let news = false;
  for (const c of r.convs) {
    const told = c.id in chat.told ? chat.told[c.id] : chat.seen[c.id] || 0;
    if (c.last_id > told && !(chatOpen() && c.id === chat.conv && !document.hidden)) news = true;
    chat.told[c.id] = Math.max(told, c.last_id);
  }
  if (news && !first) chatSound();
  if (chatOpen()) { const c = r.convs.find((x) => x.id === chat.conv); if (c) chat.seen[c.id] = c.last_id; }
  renderConvs(); updateChatDot(); chatTitle();
}
function renderConvs() {
  const shown = chat.convs.filter((c) => c.kind === "family" || c.count || c.active);
  const shared = chat.convs.reduce((n, c) => n + c.pictures + c.links, 0);
  $("#chatConvs").innerHTML = shown.map((c) => {
    const isNew = c.last_id > (chat.seen[c.id] || 0) && c.id !== chat.conv;
    return `<button type="button" role="tab" data-conv="${esc(c.id)}" class="${c.id === chat.conv ? "on" : ""} ${isNew ? "dot" : ""} ${c.active ? "" : "off"}"
      aria-selected="${c.id === chat.conv}" title="${esc(c.who)}">${esc(c.kind === "family" ? "Family" : c.kind === "kids" ? (c.named ? c.title : "Kids at home") : c.title)}${c.flagged ? `<span class="n" title="Flagged words">!${c.flagged}</span>` : ""}</button>`;
  }).join("") + (shared ? `<button type="button" role="tab" data-conv="shared" class="${chat.conv === "shared" ? "on" : ""}">Pictures &amp; links</button>` : "");
  document.querySelectorAll("[data-conv]").forEach((b) => (b.onclick = () => switchConv(b.dataset.conv)));
}
function switchConv(conv) {
  if (conv === chat.conv) return;
  chat.conv = conv; chat.last = 0; chat.started = false;
  $("#chatLog").innerHTML = "";
  const c = chat.convs.find((x) => x.id === conv);
  const family = conv === "family";
  $("#chatForm").hidden = !family;
  $("#chatNote").hidden = family;
  $("#chatNote").textContent = conv === "shared" ? "Every picture and link sent in any chat, newest first. Tap a picture to see it full size."
    : c ? `${c.who}. You can read this chat; ${c.kind === "kids" ? "only the kids at home write in it" : c.kind === "group" ? `only ${(c.people || []).join(", ")} write in it` : "only the kid and the friend write in it"}. Remove a message to hide it from them.` : "";
  chatSeen();
  loadChat().catch(oops);
}

// Text with its links as links. Everything else is escaped.
function linked(text, links) {
  let out = "", at = 0;
  for (const l of links || []) {
    const i = text.indexOf(l, at);
    if (i < 0) continue;
    const href = /^https?:\/\//i.test(l) ? l : `https://${l}`;
    out += esc(text.slice(at, i)) + `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(l)}</a>`;
    at = i + l.length;
  }
  return out + esc(text.slice(at));
}
const picUrl = (id) => `/api/parent/chat/picture/${id}`;
const vidUrl = (id) => `/api/parent/chat/video/${id}`;
// A clip plays in place; its first frame shows until it is played.
const clipHtml = (m, pic, vid) => `<video controls playsinline preload="none" ${m.picture ? `poster="${pic}"` : ""} src="${vid}"></video>`;
function msgHtml(m) {
  const pic = m.video ? clipHtml(m, picUrl(m.id), vidUrl(m.id)) : m.picture ? `<a href="${picUrl(m.id)}" target="_blank"><img src="${picUrl(m.id)}" alt="Picture from ${esc(m.sender)}" loading="lazy"></a>` : "";
  const words = m.text ? (m.removed ? `<s>${linked(m.text, m.links)}</s>` : linked(m.text, m.links)) : "";
  const tools = [m.flagged ? `<span class="flag">Flagged words</span>` : "",
    m.held && !m.removed ? `<span class="flag">Held by the picture check: the kids can't see it</span><button type="button" data-release="${m.id}">Let it through</button>` : "",
    m.removed ? `<span>Removed by ${esc(m.removed_by)}. Hidden from the kids.</span>` : !m.mine ? `<button type="button" data-remove="${m.id}">Remove</button>` : ""].filter(Boolean).join("");
  return `<div class="msg ${m.mine ? "mine" : ""} ${m.kid_id ? "kid" : ""} ${m.friend ? "friend" : ""} ${m.removed ? "removed" : ""} ${m.flagged ? "flagged" : ""}" data-mid="${m.id}">
    ${m.mine ? "" : `<div class="who">${esc(m.sender)}${m.friend ? " (friend)" : ""}</div>`}<div class="bubble">${pic}${words}</div>
    <div class="when">${esc(new Date(m.at).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" }))}</div>${tools ? `<div class="tools">${tools}</div>` : ""}</div>`;
}
function wireRemove(root, after = reloadChat) {
  root.querySelectorAll("[data-remove]").forEach((b) => (b.onclick = async () => {
    if (!await sheet({ title: "Remove this message?", text: "The kids (and the friend) will see that a parent removed it. You will still see it here.", ok: "Remove", kind: "bad" })) return;
    try { await api(`/api/parent/chat/${b.dataset.remove}`, { method: "DELETE" }); } catch (err) { return oops(err); }
    after();
  }));
}
function reloadChat() { chat.last = 0; chat.started = false; $("#chatLog").innerHTML = ""; loadChat().catch(oops); }

async function loadChat() {
  const log = $("#chatLog");
  if (chat.conv === "shared") {
    const items = await api("/api/parent/chat-shared");
    const title = (m) => (chat.convs.find((c) => c.id === m.conv) || {}).title || "";
    log.innerHTML = `<div class="shared">${items.map((m) => `<div class="item">
      ${m.video ? clipHtml(m, picUrl(m.id), vidUrl(m.id)) : m.picture ? `<a href="${picUrl(m.id)}" target="_blank"><img src="${picUrl(m.id)}" alt="" loading="lazy"></a>` : ""}
      <b>${esc(m.sender)}</b> <span class="muted">in ${esc(m.conv === "family" ? "Family" : title(m))}</span><br>
      ${m.removed ? "<s>" : ""}${linked(m.text, m.links)}${m.removed ? "</s> <span class='muted'>(removed)</span>" : ""}
      <div class="muted">${esc(new Date(m.at).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }))}</div></div>`).join("")
      || `<p class="muted">No pictures or links yet.</p>`}</div>`;
    return;
  }
  const conv = chat.conv;
  const msgs = await api(`/api/parent/chats/${encodeURIComponent(conv)}?after=${chat.last}`);
  if (conv !== chat.conv) return;   // switched while this was on its way
  if (!msgs.length) {
    if (!chat.started && !log.children.length) log.innerHTML = `<p class="muted">Nothing here yet.</p>`;
    chat.started = true;
    return;
  }
  if (!chat.started) log.innerHTML = "";
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  log.insertAdjacentHTML("beforeend", msgs.map(msgHtml).join(""));
  wireRemove(log);
  log.querySelectorAll("[data-release]").forEach((b) => (b.onclick = async () => {
    try { await api(`/api/parent/chat/${b.dataset.release}/release`, { method: "POST" }); } catch (err) { return oops(err); }
    reloadChat();
  }));
  if (atBottom || !chat.started) log.scrollTop = log.scrollHeight;
  chat.started = true;
  chat.last = msgs[msgs.length - 1].id;
  const c = chat.convs.find((x) => x.id === conv);
  if (c) c.last_id = Math.max(c.last_id, chat.last);
  if (chatOpen()) chatSeen();
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
$("#chatPic").onchange = async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  const text = $("#chatForm").text.value.trim();
  if (file.type.startsWith("video/")) {
    if (file.size > 100 * 1024 * 1024) return oops(new Error("That video is too big. Videos can be up to 30 seconds."));
    try {
      const r = await fetch(`/api/parent/chat/video?text=${encodeURIComponent(text)}`, { method: "POST", credentials: "same-origin", body: file });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
      $("#chatForm").text.value = "";
    } catch (err) { return oops(err); }
    return loadChat().catch(() => {});
  }
  if (file.size > 15 * 1024 * 1024) return oops(new Error("That picture is too big. Try a smaller one."));
  const data = await new Promise((ok, bad) => { const r = new FileReader(); r.onload = () => ok(r.result); r.onerror = bad; r.readAsDataURL(file); });
  try { await api("/api/parent/chat/picture", { method: "POST", body: { data, text } }); $("#chatForm").text.value = ""; } catch (err) { return oops(err); }
  loadChat().catch(() => {});
};

// Chat switches (Connections).
async function loadChatSettings() {
  const [{ settings: c }, h] = await Promise.all([api("/api/parent/chats"), api("/api/parent/chat-held").catch(() => ({ check: "off: unknown" }))]);
  const check = h.check;
  const sw = (key, label, hint) => `<label class="switch"><input type="checkbox" data-chatset="${key}" ${c[key] ? "checked" : ""}>
      <span><b>${label}</b><small class="muted">${hint}</small></span></label>`;
  $("#chatSettings").innerHTML = `<div class="card chatset">
    <h2>Chat permissions</h2>
    <p class="muted">The family chat is always on. You can read every chat under Messages.</p>
    ${sw("kids", "Kids at home chat with each other", "A chat just for the kids in the family. You can read it, but don't write in it.")}
    ${sw("friends", "Chat with invited friends", "Each friend with a link can type with that kid, from the same page as voice.")}
    ${sw("pictures", "Pictures", "Photos and screenshots. The hub strips hidden data such as where a photo was taken.")}
    <p class="muted check">Picture check: ${check === "on" ? "on. Pictures and videos from the kids and friends are checked for nudity on your own server; anything it flags waits in Requests." : `off (${esc(check.replace(/^off: /, ""))}). Pictures arrive without a check.`}</p>
    ${sw("videos", "Videos", "Clips up to 30 seconds. The hub keeps a small copy without location data, on your own server.")}
    ${sw("links", "Links", "Web links in messages. On the kids' PCs a link opens in Chrome, under the same web rules.")}
  </div>`;
  document.querySelectorAll("[data-chatset]").forEach((x) => (x.onchange = async () => {
    try { await api("/api/parent/chat-settings", { method: "PUT", body: { [x.dataset.chatset]: x.checked } }); } catch (err) { x.checked = !x.checked; oops(err); }
  }));
}

let chatTick = 0;  // messages every 4 s with the chat open; the conversation list every 8 s (for the dots, the tab title and the sound)
setInterval(() => {
  if ($("#app").hidden) return;
  const open = chatOpen();
  if (open) loadChat().catch(() => {});
  if (++chatTick % 2 === 0) loadConvs().catch(() => {});
}, 4000);
