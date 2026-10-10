const $ = id => document.getElementById(id);
let items = [];
let busy = false;

function status(text, kind = "") { $("status").textContent = text; $("status").className = kind; }
function working(value) {
  busy = value;
  for (const id of ["read", "send", "connect", "select-all"]) $(id).disabled = value;
}
async function message(value) {
  const response = await chrome.runtime.sendMessage(value);
  if (!response?.ok) throw new Error(response?.error || "The extension worker did not respond. Reopen the popup.");
  return response.result;
}
function input(labelText, value, type = "text", name = "") {
  const label = document.createElement("label"); label.textContent = labelText;
  const field = document.createElement("input"); field.type = type; field.value = value ?? ""; field.dataset.field = name;
  if (type === "number") {field.min = "0"; field.step = "0.01";}
  label.append(field); return label;
}
function dropdown(labelText, values, current, name) {
  const label = document.createElement("label"); label.textContent = labelText;
  const select = document.createElement("select"); select.dataset.field = name;
  for (const [value, text] of values) { const option = document.createElement("option"); option.value = value; option.textContent = text; select.append(option); }
  select.value = current; label.append(select); return label;
}
function render() {
  const container = $("listings"); container.replaceChildren();
  for (let i = 0; i < items.length; i++) {
    const item = items[i], card = document.createElement("section"); card.className = "listing"; card.dataset.index = i;
    const top = document.createElement("div"); top.className = "listing-top";
    const label = document.createElement("label"), checked = document.createElement("input"); checked.type = "checkbox"; checked.checked = true; checked.className = "selected";
    label.append(checked, document.createTextNode(`${item.source} · ${item.external_id}`));
    const link = document.createElement("a"); link.href = item.url; link.textContent = "View listing"; link.target = "_blank"; link.rel = "noopener noreferrer";
    top.append(label, link); card.append(top, input("Item title", item.title, "text", "title"));
    const row = document.createElement("div"); row.className = "row";
    row.append(input("Item price", item.price, "number", "price"), dropdown("Currency", [["EUR", "EUR"], ["PLN", "PLN"]], item.currency, "currency"), dropdown("Condition", [["unknown", "Unknown"], ["used", "Used"], ["new", "New"]], item.used === null ? "unknown" : item.used ? "used" : "new", "used"));
    const fees = document.createElement("div"); fees.className = "row-two";
    fees.append(input("Shipping (blank = unknown)", item.shipping, "number", "shipping"), input("Fees (blank = unknown)", item.fees, "number", "fees"));
    const details = document.createElement("details"), summary = document.createElement("summary"); summary.textContent = "Description and photo URL";
    const descriptionLabel = document.createElement("label"); descriptionLabel.textContent = "Description";
    const description = document.createElement("textarea"); description.dataset.field = "description"; description.value = item.description;
    descriptionLabel.append(description); details.append(summary, descriptionLabel, input("Photo URL (optional)", item.image, "url", "image"));
    card.append(row, fees, details); container.append(card);
  }
  $("send").hidden = !items.length; $("select-all").hidden = !items.length;
}
function selection() {
  const chosen = [];
  for (const card of $("listings").children) {
    if (!card.querySelector(".selected").checked) continue;
    const item = {...items[Number(card.dataset.index)]};
    for (const field of card.querySelectorAll("[data-field]")) {
      const name = field.dataset.field, value = field.value.trim();
      if (["price", "shipping", "fees"].includes(name)) {
        if (!value && name !== "price") item[name] = null;
        else if (!value || !Number.isFinite(Number(value)) || Number(value) < 0) throw new Error("Every selected item needs a valid nonnegative price, shipping and fees.");
        else item[name] = Number(value);
      } else if (name === "used") item.used = value === "unknown" ? null : value === "used";
      else item[name] = value;
    }
    if (!item.title) throw new Error("Every selected item needs a title.");
    chosen.push(item);
  }
  if (!chosen.length) throw new Error("Select at least one listing to send.");
  return chosen;
}
$("connect").addEventListener("click", async () => {
  if (busy) return;
  try {
    const settings = TrackerConfig.settings({dashboardUrl: $("dashboard-url").value, importToken: $("import-token").value});
    // Permission requests occur only here, in an explicit user gesture.
    const granted = await chrome.permissions.request({origins: [TrackerConfig.permissionPattern(settings.dashboardUrl)]});
    if (!granted) throw new Error("Local tracker permission was declined. Press Connect and allow your tracker address.");
    working(true); await message({action: "connect", settings});
    $("connection").open = false; status("Connected. Open a marketplace page, then press Read this page.", "success");
  } catch (error) {status(error.message, "error");} finally {working(false);}
});
$("read").addEventListener("click", async () => {
  if (busy) return;
  working(true); status("Reading the currently rendered page…");
  try {
    const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
    if (!tab?.id || !tab.url) throw new Error("Open a supported marketplace page first.");
    const parsed = new URL(tab.url);
    if (parsed.protocol !== "https:" || !/^(?:[^.]+\.)*(?:vinted\.lt|skelbiu\.lt|allegro\.pl|allegrolokalnie\.pl)$/.test(parsed.hostname)) throw new Error("Open an HTTPS page on Vinted.lt, Skelbiu.lt, Allegro.pl or AllegroLokalnie.pl.");
    const results = await chrome.scripting.executeScript({target: {tabId: tab.id}, files: ["extract.js"]});
    const result = results[0]?.result;
    if (!result || result.error) throw new Error(result?.error || "The page could not be read. Reload it and try again.");
    items = result.items || []; render();
    status(`${items.length} listing${items.length === 1 ? "" : "s"} found. ${items.length ? "Review prices and condition before sending." : "Wait for the page to load or open an item detail page."}${result.warnings?.length ? "\n" + result.warnings.join("\n") : ""}`);
  } catch (error) {items = []; render(); status(error.message, "error");} finally {working(false);}
});
$("select-all").addEventListener("click", () => {
  const boxes = [...$("listings").querySelectorAll(".selected")], next = boxes.some(box => !box.checked);
  boxes.forEach(box => {box.checked = next;});
});
$("send").addEventListener("click", async () => {
  if (busy) return;
  try {
    const selected = selection(); working(true); status("Sending to your local tracker…");
    const result = await message({action: "import", items: selected});
    status(`${result.queued ?? selected.length} listing${(result.queued ?? selected.length) === 1 ? "" : "s"} queued. Your worker applies the usual filters and notification rules.`, "success");
  } catch (error) {status(error.message, "error");} finally {working(false);}
});
message({action: "settings"}).then(settings => {
  $("dashboard-url").value = settings.dashboardUrl || ""; $("import-token").value = settings.importToken || "";
  if (settings.dashboardUrl) status("Press Connect once to allow your local tracker, then Read this page.");
}).catch(error => status(error.message, "error"));
