const token = document.getElementById("token");
const enabled = document.getElementById("enabled");
const domains = document.getElementById("domains");
chrome.storage.local.get(["token", "enabled", "excludedDomains"]).then(settings => {
  token.value = settings.token || "";
  enabled.checked = settings.enabled === true;
  domains.value = (settings.excludedDomains || []).join("\n");
});
document.getElementById("settings").addEventListener("submit", async event => {
  event.preventDefault();
  const excludedDomains = domains.value.split(/[\s,]+/).map(x => x.toLowerCase().replace(/^\.+|\.+$/g, "")).filter(Boolean);
  await chrome.storage.local.set({ token: token.value.trim(), enabled: enabled.checked, excludedDomains });
  document.getElementById("message").textContent = "已保存。普通网页在前台时会更新记录。";
});
setInterval(async () => {
  const data = await chrome.storage.local.get(["lastStatus", "lastCheck"]);
  document.getElementById("status").textContent = (data.lastStatus || "尚未连接") +
    (data.lastCheck ? " · " + new Date(data.lastCheck).toLocaleTimeString() : "");
}, 2000);
