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
  if (b.dataset.tab === "chat") { chatSeen(); loadChat(); }
  ({ playtimes: loadPlaytimes, requests: loadRequests, web: loadWeb, games: loadGames, voice: loadInvites }[b.dataset.tab] || (() => {}))();
}));

function fmtMin(m) { return m >= 60 ? `${Math.floor(m / 60)}h${m % 60 ? ` ${m % 60}m` : ""}` : `${m}m`; }

async function refresh() {
  const o = await api("/api/parent/overview");
  showApp();
  state.kids = o.kids;
  const pending = o.kids.reduce((n, k) => n + k.pending, 0);
  $("#reqCount").textContent = pending || "";
  $("#kids").innerHTML = o.kids.map((k) => {
    const t = k.time, total = t.allowance + t.granted, left = total ? Math.max(0, Math.min(100, (t.minutes_left / total) * 100)) : 0;
    const status = k.online ? (k.running ? `Playing <b>${esc(k.running)}</b>` : "On the home screen") : "Offline";
    const reason = { time: "out of time", bedtime: "bedtime", paused: "paused", outside_hours: "not play time" }[t.lock_reason] || t.lock_reason;
    const lock = t.locked ? `<span class="tagline bad">Locked: ${esc(reason)}</span>` : (t.warn ? `<span class="warn">Under 10 minutes left.</span>` : "");
    const apps = (k.apps || []).map((a) => {
      const sys = { kidspc: "Kids PC", webfilter: "Web filter" }[a.app];
      const retry = !sys && a.state === "failed" ? `<button class="ghost small" data-installapp="${esc(a.app)}" data-kid="${k.id}">Retry</button>` : "";
      // A downloaded build waits for a restart or sign-in unless a parent sends it in now.
      const install = a.app === "kidspc" && k.online && / ready/.test(a.detail) ? `<button class="ghost small" data-update="${k.id}">Install now</button>` : "";
      const cls = a.state === "failed" ? "failed" : a.state === "installed" ? "" : "pending";
      return `<span class="chip ${cls}" title="${esc(a.detail)}">${esc(sys || a.app)} ${esc(sys ? a.detail : a.state)}${retry}${install}</span>`;
    }).join("");
    const hasPc = (k.apps || []).some((a) => a.app === "kidspc");
    const how = hasPc ? `<span class="chip">Updates go in ${k.auto_update ? "right away" : "at the next restart"}<button class="ghost small" data-autoupdate="${k.id}" data-on="${k.auto_update ? 1 : 0}">Change</button></span>` : "";
    // "On build X" is only half the story: say when something newer is still being built or on its way.
    const pc = (k.apps || []).find((a) => a.app === "kidspc"), B = o.builds || {}, b = B.building;
    const late = b && b.waiting_minutes >= 15;
    let newer = "";
    if (pc && b) newer = `${late ? "A newer build should have arrived by now" : "A newer build is being made"}${b.title ? `: ${esc(b.title)}` : ""}. ${late
      ? `It has been ${b.waiting_minutes} minutes, so its build may have failed on GitHub.`
      : "It reaches this PC about 5 minutes after a merge."}`;
    else if (pc && k.online && B.published && !pc.detail.includes(B.published.slice(0, 8)))
      newer = `A newer build${B.published_title ? ` (${esc(B.published_title)})` : ""} is on its way to this PC.`;
    if (newer) newer = `<div class="muted">${newer}</div>`;
    return `<div class="card kid ${k.online ? "online" : ""}">
      <div class="kidhead">
        <div class="avatar">${esc(k.name.slice(0, 1))}</div>
        <div class="kidname"><h2>${esc(k.name)}</h2><div class="status"><span class="dot ${k.online ? "on" : ""}"></span>${status}</div></div>
        <div class="timeleft"><div class="big">${fmtMin(t.minutes_left)}</div><div class="muted">left today</div></div>
      </div>
      <div class="fuel ${t.locked || left <= 12 ? "empty" : left <= 34 ? "low" : ""}"><div style="width:${left}%"></div></div>
      <div class="muted">Played ${fmtMin(t.used)} of ${fmtMin(total)} today. ${lock}${t.next_play ? ` Next play time ${esc(nextPlay(t.next_play))}.` : ""}</div>
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
      ${apps ? `<div class="chips">${apps}${how}</div>${newer}` : ""}
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
  document.querySelectorAll("[data-update]").forEach((b) => (b.onclick = async () => {
    const name = kidName(b.dataset.update);
    if (!await sheet({ title: `Install the update on ${name}'s PC now?`, text: "He is signed out for about a minute, even in the middle of a game, and the PC restarts.", ok: "Install now", kind: "bad" })) return;
    await api(`/api/parent/kids/${b.dataset.update}/commands`, { method: "POST", body: { kind: "install_app", arg: "kidspc" } }); b.textContent = "Sent";
  }));
  document.querySelectorAll("[data-autoupdate]").forEach((b) => (b.onclick = async () => {
    const on = b.dataset.on !== "1", name = kidName(b.dataset.autoupdate);
    const text = on
      ? "A new build goes in the moment it arrives. He is signed out for about a minute unless a game is running. Good for a test PC."
      : "A new build waits for the next restart or sign-in, so it never interrupts him.";
    if (!await sheet({ title: on ? `Install updates on ${name}'s PC right away?` : `Wait for a restart on ${name}'s PC?`, text, ok: on ? "Right away" : "At the next restart" })) return;
    await api(`/api/parent/kids/${b.dataset.autoupdate}/auto-update`, { method: "PUT", body: { on } }); refresh();
  }));
  document.querySelectorAll("[data-installapp]").forEach((b) => (b.onclick = async () => {
    await api(`/api/parent/kids/${b.dataset.kid}/commands`, { method: "POST", body: { kind: "install_app", arg: b.dataset.installapp } }); b.textContent = "Retrying…";
  }));
  document.querySelectorAll("[data-activity]").forEach((d) => (d.ontoggle = async () => {
    if (!d.open) return;
    const a = await api(`/api/parent/kids/${d.dataset.activity}/activity`);
    const label = { stopped_time_up: "Stopped (time up)", game_blocked: "Blocked unapproved", app_installed: "Installed", app_install_failed: "Install failed", browser_blocked: "Closed other browser", web_filter: "Web filter set", offline_play: "Played offline", tool_blocked: "Closed a Windows tool he shouldn't have" };
    const rows = [
      ...a.events.map((e) => ({ at: e.at, text: `${label[e.kind] || e.kind}: ${e.detail}` })),
      ...a.commands.map((c) => ({ at: c.created_at.replace(" ", "T") + "Z", text: `${c.created_by} sent ${c.kind}${c.arg ? ` "${c.arg}"` : ""} (${c.state}${c.result ? ": " + c.result : ""})` })),
    ].sort((x, y) => (x.at < y.at ? 1 : -1)).slice(0, 30);
    d.querySelector("ul").innerHTML = rows.map((r) => `<li>${esc(r.text)}<div class="muted">${new Date(r.at).toLocaleString()}</div></li>`).join("") || `<li class="muted">Nothing yet.</li>`;
  }));
  fillKidSelects();
  loadFamily().catch(() => {});
}

// Family: what the brothers are doing together, and the parents' switches for it.
async function loadFamily() {
  const f = await api("/api/parent/family");
  const on = f.kids.filter((k) => k.online);
  const names = (ks) => ks.map((k) => esc(k.name)).join(" and ");
  const voice = f.kids.filter((k) => k.in_family_voice);
  const together = f.playing_together ? `${names(on.filter((k) => k.running))} are playing <b>${esc(on.find((k) => k.running).running)}</b> together.`
    : on.length ? on.map((k) => `${esc(k.name)}: ${k.locked ? "done for today" : k.running ? `playing <b>${esc(k.running)}</b>` : "home screen"}`).join(" · ")
    : "Nobody's PC is on.";
  const sw = (key, label, hint) => `<label class="switch"><input type="checkbox" data-family="${key}" ${f.settings[key] ? "checked" : ""}>
      <span><b>${label}</b><small class="muted">${hint}</small></span></label>`;
  $("#family").innerHTML = `<div class="card family">
    <h2>Family</h2>
    <p>${together}</p>
    <div class="muted">${voice.length ? `In family voice: <b>${names(voice)}</b>` : "Nobody is in family voice."}</div>
    ${sw("see", "See each other", "Each PC shows what his brother is playing.")}
    ${sw("join", "Join each other's games", "One tap starts the same game, only if it's approved for both.")}
    ${sw("voice", "Family voice", "A voice room just for the brothers, no invite link. Turning it off ends a call.")}
  </div>`;
  document.querySelectorAll("[data-family]").forEach((c) => (c.onchange = async () => {
    try { await api("/api/parent/family", { method: "PUT", body: { [c.dataset.family]: c.checked } }); } catch (err) { c.checked = !c.checked; oops(err); }
  }));
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
  // Fill each game request with matches from every store and preselect the best one.
  reqs.filter((r) => r.kind === "game").forEach(async (r) => {
    const group = $(`[data-steamfor="${r.id}"]`), sel = $(`[data-gamefor="${r.id}"]`), img = $(`[data-artfor="${r.id}"]`);
    const inCat = games.find((g) => g.title.toLowerCase() === r.detail.toLowerCase());
    if (inCat) sel.value = inCat.id;
    let hits = [];
    const picked = (r.ref || "").startsWith("steam:") ? r.ref : "";
    try {
      if (picked) {   // the exact Steam game he picked on his screen goes first
        const a = await api(`/api/parent/steam/app/${picked.slice(6)}`);
        if (a.appid) hits.push(...(await api(`/api/parent/games/search?q=${encodeURIComponent(a.name)}`)).results
          .filter((h) => h.platforms.some((p) => `steam:${p.launch_ref}` === picked)).slice(0, 1));
      }
      const more = (await api(`/api/parent/games/search?q=${encodeURIComponent(r.detail)}`)).results;
      hits = hits.concat(more.filter((h) => !hits.some((x) => x.title === h.title)));
    } catch {}
    hits = hits.filter((h) => h.default !== null);
    const refOf = (h) => `${h.platforms[h.default].launcher}:${h.platforms[h.default].launch_ref}`;
    const byRef = Object.fromEntries(hits.map((h) => [refOf(h), h]));
    const info = $(`[data-infofor="${r.id}"]`);
    group.label = hits.length ? "Add from the stores" : "No store matches";
    group.innerHTML = hits.map((h) => `<option value="${esc(refOf(h))}" data-img="${esc(h.image_url)}">${esc(h.title)} · ${esc(h.platforms[h.default].label)}${h.esrb ? ` (${esc(h.esrb)})` : ""}${refOf(h) === picked ? " ← the one he picked" : ""}</option>`).join("");
    const showArt = () => {
      const o = sel.selectedOptions[0]; const src = o?.dataset.img || games.find((g) => String(g.id) === sel.value)?.image_url;
      img.hidden = !src; if (src) img.src = src;
      const h = byRef[sel.value];
      info.innerHTML = h ? gameFacts(h, h.platforms[h.default]) : "";
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
function renderGameHits() {
  $("#gameResults").innerHTML = gameHits.map((h, i) => {
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
    note.textContent = !gameHits.length ? "No games found. Try a shorter name." : r.xbox === "unavailable" ? "Couldn't reach the Xbox catalogue just now, so Game Pass isn't shown." : "Tap a store to choose where he gets it.";
  } catch (err) { note.textContent = err.message; }
  renderGameHits();
};

const STORE = { steam: "Steam", battlenet: "Battle.net", gamepass: "PC Game Pass", direct: "Installed by hand" };
async function loadGames() {
  const games = await api("/api/parent/games");
  $("#games").innerHTML = games.map((g) => `<li>${g.image_url ? `<img class="art" src="${esc(g.image_url)}" alt="" loading="lazy">` : ""}<b>${esc(g.title)}</b> ${g.rating ? `<span class="tagline rating">${esc(g.rating)}</span>` : ""} <span class="muted">${STORE[g.launcher] || g.launcher}</span>
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
setInterval(() => { if (!$("#app").hidden && !$('[data-panel="now"]').hidden) refresh().catch(() => {}); }, 10000);

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

async function loadPlaytimes() {
  pt.data = await api("/api/parent/playtimes");
  if (!pt.kid || !pt.data.kids.some((k) => k.id === pt.kid)) pt.kid = pt.data.kids[0]?.id;
  renderPlaytimes();
}

function renderPlaytimes() {
  const kids = pt.data.kids, kid = kids.find((k) => k.id === pt.kid);
  $("#ptKids").hidden = kids.length < 2;
  $("#ptKids").innerHTML = kids.map((k) => `<button role="tab" aria-selected="${k.id === pt.kid}" class="${k.id === pt.kid ? "on" : ""}" data-ptkid="${k.id}">${esc(k.name)}</button>`).join("");
  document.querySelectorAll("[data-ptkid]").forEach((b) => (b.onclick = () => { pt.kid = b.dataset.ptkid; renderPlaytimes(); }));
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
  if (!box.querySelector(".winrow")) box.innerHTML = `<p class="hint">None: he can play any time from 6 AM until bedtime.</p>`;
  box.querySelectorAll("[data-delwin]").forEach((b) => (b.onclick = () => { b.closest(".winrow").remove(); wireWindows(); }));
}

function editPlaytime(day) {
  const kid = pt.data.kids.find((k) => k.id === pt.kid), d = kid.days[day], f = $("#ptForm");
  pt.day = day;
  $("#ptTitle").textContent = `${kid.name}'s ${DAY_NAMES[day]}`;
  f.daily_minutes.value = d.daily_minutes;
  $("#ptQuick").innerHTML = [0, 30, 60, 90, 120, 180].map((m) => `<button type="button" class="ghost small" data-quick="${m}">${m ? fmtMin(m) : "No play"}</button>`).join("");
  $("#ptQuick").querySelectorAll("[data-quick]").forEach((b) => (b.onclick = () => (f.daily_minutes.value = b.dataset.quick)));
  f.bedtime.value = d.bedtime || "20:30"; f.nobed.checked = !d.bedtime; f.bedtime.disabled = !d.bedtime;
  f.nobed.onchange = () => (f.bedtime.disabled = f.nobed.checked);
  $("#ptWindows").innerHTML = d.windows.map(windowRow).join(""); wireWindows();
  $("#ptDaysPick").innerHTML = DAY_NAMES.map((n, i) => `<label class="pick"><input type="checkbox" name="day" value="${i}" ${i === day ? "checked" : ""}><span>${n.slice(0, 3)}</span></label>`).join("");
  $("#ptKidsPick").innerHTML = pt.data.kids.length < 2 ? "" : pt.data.kids.map((k) => `<label class="pick"><input type="checkbox" name="kid" value="${k.id}" ${k.id === kid.id ? "checked" : ""}><span>${esc(k.name)}</span></label>`).join("");
  $("#ptErr").textContent = "";
  $("#ptSheet").showModal();
}
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
    for (const k of kids.length ? kids : [pt.kid]) await api(`/api/parent/playtimes/${k}`, { method: "PUT", body });
  } catch (err) { $("#ptErr").textContent = err.message; return; }
  $("#ptSheet").close();
  loadPlaytimes().catch(oops); refresh().catch(() => {});
};

// Family chat ---------------------------------------------------------------
const chat = { last: 0, seen: 0, started: false };
// On a phone chat is a tab. On a laptop (same 900px as app.css) it is a bar docked in the bottom right corner
// that opens over whatever tab is showing: body.chat-open while it is open, body.chat-unread for the red dot.
const wide = window.matchMedia("(min-width: 900px)");
function chatOpen() {
  if ($("#app").hidden) return false;
  return wide.matches ? document.body.classList.contains("chat-open") : !$('[data-panel="chat"]').hidden;
}
function chatSeen() { $("#chatDot").textContent = ""; document.body.classList.remove("chat-unread"); chat.seen = chat.last; }
{
  const bar = $('[data-panel="chat"] > .title');
  bar.tabIndex = 0; bar.setAttribute("role", "button");
  const toggle = () => {
    if (!wide.matches) return;
    const open = document.body.classList.toggle("chat-open");
    bar.setAttribute("aria-expanded", open);
    if (!open) return;
    chatSeen(); $("#chatLog").scrollTop = $("#chatLog").scrollHeight; $("#chatForm").text.focus(); loadChat().catch(() => {});
  };
  bar.onclick = toggle;
  bar.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } };
  // Window dragged from phone width to laptop width while on the Chat tab: that tab is gone, so go to Now.
  wide.addEventListener("change", () => { if (wide.matches && $('nav button[data-tab="chat"]').classList.contains("on")) $('nav button[data-tab="now"]').click(); });
}
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
  if (unread) { $("#chatDot").textContent = "\u25CF"; document.body.classList.add("chat-unread"); }
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
