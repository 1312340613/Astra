/* No content scripts, page access, history enumeration, or private-mode access. */
let generation = 0;
const browser = /Edg\//.test(navigator.userAgent) ? "edge" : "chrome";

function cleanUrl(value, excluded = []) {
  try {
    const url = new URL(value);
    if (!["http:", "https:"].includes(url.protocol)) return null;
    const host = url.hostname.toLowerCase();
    if (excluded.some(domain => host === domain || host.endsWith("." + domain))) return null;
    url.username = "";
    url.password = "";
    url.search = "";
    url.hash = "";
    return url.href;
  } catch { return null; }
}

async function publish() {
  const current = ++generation;
  const observedAt = Date.now() / 1000;
  const settings = await chrome.storage.local.get(["token", "enabled", "excludedDomains"]);
  if (!settings.token) return;
  const payload = { browser, observedAt, focused: false, incognito: true };
  try {
    if (settings.enabled) {
      const win = await chrome.windows.getLastFocused();
      const [tab] = win.focused && !win.incognito ? await chrome.tabs.query({ active: true, windowId: win.id }) : [];
      if (win.focused === true && win.incognito === false &&
          tab?.incognito === false && tab.status === "complete") {
        const url = cleanUrl(tab.url, settings.excludedDomains || []);
        if (url && tab.title) Object.assign(payload, { focused: true, incognito: false, url, title: tab.title.slice(0, 1024) });
      }
    }
  } catch { /* Unknown/private/focus transition: send an empty snapshot. */ }
  if (current !== generation) return;
  try {
    const response = await fetch("http://127.0.0.1:8089/snapshot", {
      method: "POST",
      headers: { "Authorization": "Bearer " + settings.token, "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(2000),
    });
    await chrome.storage.local.set({ lastStatus: response.ok ? "Connected" : "HTTP " + response.status, lastCheck: Date.now() });
  } catch {
    await chrome.storage.local.set({ lastStatus: "Recorder unavailable", lastCheck: Date.now() });
  }
}

chrome.tabs.onActivated.addListener(publish);
chrome.tabs.onUpdated.addListener(publish);
chrome.tabs.onRemoved.addListener(publish);
chrome.windows.onFocusChanged.addListener(publish);
chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("refresh", { periodInMinutes: 0.5 });
  chrome.runtime.openOptionsPage();
});
chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create("refresh", { periodInMinutes: 0.5 });
  publish();
});
chrome.alarms.onAlarm.addListener(publish);
chrome.action.onClicked.addListener(() => chrome.runtime.openOptionsPage());
chrome.storage.onChanged.addListener(changes => {
  if (changes.token || changes.enabled || changes.excludedDomains) publish();
});
// Service workers can be suspended. The receiver expires stale snapshots rather
// than attaching an old URL; alarms/events restart publishing when awakened.
setInterval(publish, 3000);
publish();
