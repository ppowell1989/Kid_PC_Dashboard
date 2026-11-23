// Kids PC Reporter: queues top-level page loads and posts them to the hub every minute.
// Config comes from Chrome policy (chrome.storage.managed), so the kid can't change it.

const FLUSH_MINUTES = 1;
const MAX_QUEUE = 500;

async function config() {
  const c = await chrome.storage.managed.get(["hubUrl", "token"]);
  return c.hubUrl && c.token ? c : null;
}

async function enqueue(visit) {
  const { queue = [] } = await chrome.storage.local.get("queue");
  queue.push(visit);
  await chrome.storage.local.set({ queue: queue.slice(-MAX_QUEUE) });
}

async function flush() {
  const cfg = await config();
  if (!cfg) return;
  const { queue = [] } = await chrome.storage.local.get("queue");
  if (!queue.length) return;
  const batch = queue.slice(0, 200);
  try {
    const r = await fetch(`${cfg.hubUrl.replace(/\/$/, "")}/api/device/visits`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${cfg.token}` },
      body: JSON.stringify(batch),
    });
    if (r.ok) {
      const { queue: now = [] } = await chrome.storage.local.get("queue");
      await chrome.storage.local.set({ queue: now.slice(batch.length) });
    }
  } catch (_) {
    // Hub unreachable: keep the queue and try again next minute.
  }
}

chrome.webNavigation.onCompleted.addListener(async (d) => {
  if (d.frameId !== 0 || !/^https?:/.test(d.url)) return;
  let title = "";
  try { title = (await chrome.tabs.get(d.tabId)).title || ""; } catch (_) {}
  await enqueue({ url: d.url, title, at: new Date().toISOString() });
});

// Titles often arrive after onCompleted; patch the latest entry for that URL.
chrome.tabs.onUpdated.addListener(async (_tabId, info, tab) => {
  if (!info.title || !tab.url) return;
  const { queue = [] } = await chrome.storage.local.get("queue");
  for (let i = queue.length - 1; i >= 0; i--) {
    if (queue[i].url === tab.url) { if (!queue[i].title) { queue[i].title = info.title; await chrome.storage.local.set({ queue }); } break; }
  }
});

chrome.alarms.create("flush", { periodInMinutes: FLUSH_MINUTES });
chrome.alarms.onAlarm.addListener((a) => a.name === "flush" && flush());
