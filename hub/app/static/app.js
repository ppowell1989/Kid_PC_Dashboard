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
  ({ requests: loadRequests, web: loadWeb, games: loadGames, voice: loadInvites }[b.dataset.tab] || (() => {}))();
}));

function fmtMin(m) { return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m`; }

async function refresh() {
  const o = await api("/api/parent/overview");
  showApp();
  state.kids = o.kids;
  const pending = o.kids.reduce((n, k) => n + k.pending, 0);
  $("#reqCount").textContent = pending ? `(${pending})` : "";
  $("#kids").innerHTML = o.kids.map((k) => {
    const t = k.time, total = t.allowance + t.granted, pct = total ? Math.min(100, (t.used / total) * 100) : 0;
    const status = k.online ? (k.running ? `Playing <b>${esc(k.running)}</b>` : "On the home screen") : "Offline";
    const lock = t.locked ? `<span class="bad">Locked (${t.lock_reason})</span>` : (t.warn ? `<span class="warn">Under 10 minutes left</span>` : "");
    return `<div class="card">
      <h2><span class="dot ${k.online ? "on" : ""}"></span>${esc(k.name)}</h2>
      <div>${status}</div>
      <div class="big">${fmtMin(t.minutes_left)} left</div>
      <div class="bar"><div style="width:${pct}%"></div></div>
      <div class="muted">Played ${fmtMin(t.used)} of ${fmtMin(total)} today. ${lock}</div>
      ${k.apps && k.apps.length ? `<div class="muted">PC apps: ${k.apps.map((a) => `${esc(a.app)} <span class="${a.state === "failed" ? "bad" : ""}" title="${esc(a.detail)}">${esc(a.state)}</span>${a.state === "failed" ? ` <button class="ghost" data-installapp="${esc(a.app)}" data-kid="${k.id}">Retry</button>` : ""}`).join(" · ")}</div>` : ""}
      <div class="actions">
        ${[15, 30, 60].map((m) => `<button class="ghost" data-grant="${k.id}" data-min="${m}">+${m}m</button>`).join("")}
      </div>
      <div class="actions">
        ${t.lock_reason === "paused"
          ? `<button data-resume="${k.id}">Resume</button>`
          : `<button class="ghost" data-pause="${k.id}">Pause now</button>`}
        <button class="ghost" data-msg="${k.id}">Message</button>
        <button class="ghost" data-cmd="restart" data-kid="${k.id}">Restart PC</button>
        <button class="ghost" data-cmd="shutdown" data-kid="${k.id}">Shut down</button>
      </div>
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
    const text = prompt(`Message to show on ${kidName(b.dataset.msg)}'s screen:`);
    if (text && text.trim()) { await api(`/api/parent/kids/${b.dataset.msg}/commands`, { method: "POST", body: { kind: "message", arg: text.trim() } }); b.textContent = "Sent"; }
  }));
  document.querySelectorAll("[data-cmd]").forEach((b) => (b.onclick = async () => {
    const verb = b.dataset.cmd === "restart" ? "Restart" : "Shut down";
    if (!confirm(`${verb} ${kidName(b.dataset.kid)}'s PC? He gets a 1-minute warning.`)) return;
    await api(`/api/parent/kids/${b.dataset.kid}/commands`, { method: "POST", body: { kind: b.dataset.cmd } }); b.textContent = "Sent";
  }));
  document.querySelectorAll("[data-installapp]").forEach((b) => (b.onclick = async () => {
    await api(`/api/parent/kids/${b.dataset.kid}/commands`, { method: "POST", body: { kind: "install_app", arg: b.dataset.installapp } }); b.textContent = "Retrying…";
  }));
  document.querySelectorAll("[data-activity]").forEach((d) => (d.ontoggle = async () => {
    if (!d.open) return;
    const a = await api(`/api/parent/kids/${d.dataset.activity}/activity`);
    const label = { stopped_time_up: "Stopped (time up)", game_blocked: "Blocked unapproved", app_installed: "Installed", app_install_failed: "Install failed" };
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

async function loadRequests() {
  const [reqs, games] = await Promise.all([api("/api/parent/requests"), api("/api/parent/games")]);
  $("#requests").innerHTML = reqs.length ? reqs.map((r) => {
    const what = r.kind === "time" ? `${r.detail} more minutes` : r.kind === "game" ? `Game: <b>${esc(r.detail)}</b>` : `Site: <b>${esc(r.detail)}</b>`;
    const gamePick = r.kind === "game"
      ? `<select data-gamefor="${r.id}"><option value="">Choose catalogue game…</option>${games.map((g) => `<option value="${g.id}">${esc(g.title)}</option>`).join("")}</select>
         <div class="muted">Not listed? Add it on the Games tab first.</div>` : "";
    return `<div class="card"><b>${esc(kidName(r.kid_id))}</b> asks for ${what}
      ${r.note ? `<div class="muted">"${esc(r.note)}"</div>` : ""}
      <div class="muted">${esc(r.created_at)} UTC</div>${gamePick}
      <div class="actions"><button data-decide="${r.id}" data-ok="1">Approve</button><button class="bad" data-decide="${r.id}" data-ok="0">Decline</button></div></div>`;
  }).join("") : `<p class="muted">No requests waiting.</p>`;
  document.querySelectorAll("[data-decide]").forEach((b) => (b.onclick = async () => {
    const pick = $(`[data-gamefor="${b.dataset.decide}"]`);
    const body = { approve: b.dataset.ok === "1" };
    if (pick && body.approve) { if (!pick.value) return alert("Choose the catalogue game first."); body.game_id = +pick.value; }
    await api(`/api/parent/requests/${b.dataset.decide}`, { method: "POST", body });
    loadRequests(); refresh();
  }));
}

async function loadWeb() {
  const kid = $("#webKid").value, src = $("#webSource").value;
  const [visits, sites] = await Promise.all([
    api(`/api/parent/visits/${kid}?limit=200${src ? `&source=${src}` : ""}`), api("/api/parent/sites"),
  ]);
  $("#visits").innerHTML = visits.map((v) => `<li>${v.blocked ? '<span class="bad">Blocked</span> ' : ""}<b>${esc(v.title || v.domain)}</b>
    <div class="muted">${esc(v.url || v.domain)} · ${esc(v.at)} · ${v.source}</div></li>`).join("") || `<li class="muted">Nothing yet.</li>`;
  $("#webMode").textContent = sites.mode === "allowlist" ? "(allow-list mode: only these load)" : "(filtered mode: list is informational)";
  $("#sites").innerHTML = sites.sites.map((d) => `<li>${esc(d)} <button class="ghost" data-delsite="${esc(d)}">Remove</button></li>`).join("");
  document.querySelectorAll("[data-delsite]").forEach((b) => (b.onclick = async () => { await api(`/api/parent/sites/${encodeURIComponent(b.dataset.delsite)}`, { method: "DELETE" }); loadWeb(); }));
}
$("#webKid").onchange = $("#webSource").onchange = loadWeb;
$("#siteForm").onsubmit = async (e) => { e.preventDefault(); await api("/api/parent/sites", { method: "POST", body: { domain: new FormData(e.target).get("domain") } }); e.target.reset(); loadWeb(); };

async function loadGames() {
  const games = await api("/api/parent/games");
  $("#games").innerHTML = games.map((g) => `<li><b>${esc(g.title)}</b> <span class="muted">${g.launcher} ${esc(g.launch_ref)} ${esc(g.rating)}</span>
    <div class="actions">${state.kids.map((k) => `<label><input type="checkbox" data-appr="${g.id}" data-kid="${k.id}" ${g.approved_for.includes(k.id) ? "checked" : ""}> ${esc(k.name)}</label>`).join("")}
    <button class="ghost" data-delgame="${g.id}">Remove</button></div></li>`).join("") || `<li class="muted">Catalogue is empty.</li>`;
  document.querySelectorAll("[data-appr]").forEach((c) => (c.onchange = () => api(`/api/parent/games/${c.dataset.appr}/kids/${c.dataset.kid}`, { method: "PUT", body: { approved: c.checked } })));
  document.querySelectorAll("[data-delgame]").forEach((b) => (b.onclick = async () => { await api(`/api/parent/games/${b.dataset.delgame}`, { method: "DELETE" }); loadGames(); }));
}
$("#gameForm").onsubmit = async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  await api("/api/parent/games", { method: "POST", body: {
    title: f.get("title"), launcher: f.get("launcher"), launch_ref: f.get("launch_ref"), rating: f.get("rating"), approved_for: f.getAll("kid"),
  } });
  e.target.reset(); loadGames();
};

async function loadInvites() {
  const inv = await api("/api/parent/invites");
  $("#invites").innerHTML = inv.map((i) => `<li><b>${esc(i.friend_name)}</b> with ${esc(kidName(i.kid_id))} ${i.revoked ? '<span class="bad">revoked</span>' : ""}
    <div class="muted">${esc(i.link)}</div>
    ${i.revoked ? "" : `<div class="actions"><button class="ghost" data-copy="${esc(i.link)}">Copy link</button><button class="bad" data-revoke="${esc(i.code)}">Revoke</button></div>`}</li>`).join("") || `<li class="muted">No invites yet.</li>`;
  document.querySelectorAll("[data-copy]").forEach((b) => (b.onclick = () => navigator.clipboard.writeText(b.dataset.copy).then(() => (b.textContent = "Copied"))));
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
