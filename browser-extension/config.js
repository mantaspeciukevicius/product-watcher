/* Local configuration and validation; no marketplace network access. */
const TrackerConfig = (() => {
  function localHostname(host) {
    const name = host.toLowerCase().replace(/^\[|\]$/g, "");
    if (name === "localhost" || name === "::1" || /\.(?:local|lan|home)$/.test(name)) return true;
    if (/^(?:fc|fd)[a-f0-9]{2}:/.test(name) || /^fe[89ab][a-f0-9]:/.test(name)) return true;
    const octets = name.split(".");
    if (octets.length !== 4 || octets.some(n => !/^\d+$/.test(n) || +n > 255)) return false;
    const [a, b] = octets.map(Number);
    return a === 10 || a === 127 || (a === 192 && b === 168) || (a === 172 && b >= 16 && b <= 31);
  }
  function dashboardUrl(value) {
    let url;
    try { url = new URL(value.trim()); } catch (_) { throw new Error("Enter the full local dashboard URL, including http:// and its port."); }
    if (!["http:", "https:"].includes(url.protocol) || !localHostname(url.hostname)) {
      throw new Error("Use a private LAN address, localhost, or a .local/.lan/.home hostname.");
    }
    if (url.username || url.password || url.search || url.hash || !["", "/"].includes(url.pathname)) {
      throw new Error("Use only the dashboard origin, for example http://192.168.1.10:8765.");
    }
    return url.origin;
  }
  function permissionPattern(origin) {
    const url = new URL(dashboardUrl(origin));
    return `${url.protocol}//${url.hostname}/*`;
  }
  function settings(value) {
    const dashboard = dashboardUrl(value.dashboardUrl || "");
    const token = String(value.importToken || "").trim();
    if (token.length < 8 || token.length > 512 || /\s/.test(token)) throw new Error("Paste the import token from your dashboard’s Browser Import page.");
    return {dashboardUrl: dashboard, importToken: token};
  }
  return {localHostname, dashboardUrl, permissionPattern, settings};
})();
