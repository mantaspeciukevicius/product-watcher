importScripts("config.js");

const settingsKey = "trackerSettings";
let initializing;

async function configuration() {
  if (!initializing) initializing = (async () => {
    const stored = await chrome.storage.local.get(settingsKey);
    if (stored[settingsKey]) return stored[settingsKey];
    // The authenticated dashboard download can pair a ZIP. Generic source has no config.
    try {
      const response = await fetch(chrome.runtime.getURL("paired-config.json"));
      if (response.ok) {
        const paired = TrackerConfig.settings(await response.json());
        await chrome.storage.local.set({[settingsKey]: paired});
        return paired;
      }
    } catch (_) { /* An unpaired installation is configured in the popup. */ }
    return {dashboardUrl: "", importToken: ""};
  })();
  return initializing;
}

async function trackerRequest(path, items) {
  const config = TrackerConfig.settings(await configuration());
  if (!await chrome.permissions.contains({origins: [TrackerConfig.permissionPattern(config.dashboardUrl)]})) {
    throw new Error("Press Connect to allow access to your configured local tracker.");
  }
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch(config.dashboardUrl + path, {
      method: items ? "POST" : "GET",
      headers: {"Authorization": `Bearer ${config.importToken}`, ...(items ? {"Content-Type": "application/json"} : {})},
      ...(items ? {body: JSON.stringify(items)} : {}),
      credentials: "omit", redirect: "error", signal: controller.signal
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 401 || response.status === 403) throw new Error("The tracker rejected this token or network address. Copy the current import token and check your LAN connection.");
      const detail = typeof result.detail === "string" ? result.detail : "Check the edited listing fields and try again.";
      throw new Error(`Tracker returned ${response.status}. ${detail}`);
    }
    return result;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("The tracker did not respond. Check the dashboard URL and that this PC is on your home network.");
    if (error instanceof TypeError) throw new Error("Cannot reach the local tracker. Check your home Wi-Fi, dashboard URL, browser local-network permission, and that the tracker is running.");
    throw error;
  } finally { clearTimeout(timeout); }
}

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  // Content scripts never receive configuration or tokens. Only our popup sends these messages.
  if (!sender.url || !sender.url.startsWith(chrome.runtime.getURL("popup.html"))) return false;
  (async () => {
    if (message.action === "settings") return await configuration();
    if (message.action === "connect") {
      const config = TrackerConfig.settings(message.settings || {});
      await chrome.storage.local.set({[settingsKey]: config});
      initializing = Promise.resolve(config);
      return await trackerRequest("/api/import/status");
    }
    if (message.action === "import") {
      if (!Array.isArray(message.items) || !message.items.length || message.items.length > 100) throw new Error("Select between 1 and 100 listings.");
      const encoded = JSON.stringify(message.items);
      if (encoded.length > 1000000) throw new Error("This selection is too large. Send fewer listings at once.");
      return await trackerRequest("/api/import", message.items);
    }
    throw new Error("Unknown extension operation.");
  })().then(result => respond({ok: true, result})).catch(error => respond({ok: false, error: error.message}));
  return true;
});
