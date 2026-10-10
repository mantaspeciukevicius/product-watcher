/* Runs only when the user presses Read this page. No fetch, navigation or secrets. */
(() => {
  const warnings = [];
  const hosts = {vinted: ["vinted.lt"], skelbiu: ["skelbiu.lt"], allegro: ["allegro.pl", "allegrolokalnie.pl"]};
  const normalize = text => String(text || "").toLowerCase().replace(/ł/g, "l").normalize("NFKD").replace(/[\u0300-\u036f]/g, "");
  const clean = (text, maximum = 30000) => String(text || "").replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, "").trim().slice(0, maximum);
  const visible = node => Boolean(node && node.getClientRects().length && getComputedStyle(node).visibility !== "hidden" && getComputedStyle(node).display !== "none");
  const visibleNode = (selector, root = document) => [...root.querySelectorAll(selector)].find(visible);
  const text = node => clean(node?.innerText || "");
  function sourceFor(hostname) {
    for (const [source, allowed] of Object.entries(hosts)) if (allowed.some(host => hostname === host || hostname.endsWith("." + host))) return source;
    return null;
  }
  function listingUrl(value, expectedSource) {
    let url;
    try {url = new URL(value, location.href);} catch (_) {return null;}
    if (url.protocol !== "https:" || url.username || url.password || (url.port && url.port !== "443")) return null;
    const source = sourceFor(url.hostname);
    if (!source || (expectedSource && source !== expectedSource)) return null;
    let id;
    if (source === "vinted") id = url.pathname.match(/^\/items\/(\d+)(?:[-/]|$)/)?.[1];
    if (source === "skelbiu") id = url.pathname.match(/^\/skelbimai\/[^/]*-(\d+)\.html\/?$/)?.[1];
    if (source === "allegro") {
      if (url.hostname === "allegrolokalnie.pl" || url.hostname.endsWith(".allegrolokalnie.pl")) {
        const slug = url.pathname.match(/^\/oferta\/([a-zA-Z0-9_-]+)\/?$/)?.[1];
        if (slug && slug.length <= 140) id = "local:" + slug;
      } else id = url.pathname.match(/^\/oferta\/(?:[^/]*-)?(\d+)\/?$/)?.[1];
    }
    if (!id) return null;
    url.search = ""; url.hash = "";
    return {source, external_id: id, url: url.href};
  }
  function amount(value) {
    const raw = String(value).replace(/[\s\u00a0\u202f]/g, "");
    if (!/^\d+(?:[.,]\d+)*$/.test(raw)) return null;
    const last = Math.max(raw.lastIndexOf("."), raw.lastIndexOf(","));
    let number;
    if (last >= 0 && raw.length - last - 1 <= 2) number = Number(raw.slice(0, last).replace(/[.,]/g, "") + "." + raw.slice(last + 1));
    else number = Number(raw.replace(/[.,]/g, ""));
    return Number.isFinite(number) && number >= 0 && number <= 1000000 ? number : null;
  }
  function money(value, preferredCurrency) {
    const raw = String(value || "").replace(/[\u00a0\u202f]/g, " ");
    const number = "(?:\\d{1,3}(?:[ .]\\d{3})+|\\d+)(?:[.,]\\d{1,2})?";
    const symbol = preferredCurrency === "PLN" ? "(?:zł|zl|PLN)" : "(?:€|EUR)";
    const suffix = new RegExp("(?<![\\w.,])(" + number + ")[ \\t]*" + symbol + "(?![a-zA-Z])", "i").exec(raw);
    const prefix = new RegExp(symbol + "[ \\t]*(" + number + ")(?![\\d.,])", "i").exec(raw);
    return amount((suffix || prefix)?.[1] || "");
  }
  function condition(value) {
    const normalized = normalize(value);
    if (/\b(?:newcondition|new with|new without|naujas|nauja|naujos|nowy|nowa|nowe)\b/.test(normalized)) return false;
    if (/\b(?:usedcondition|used|naudotas|naudota|naudoti|uzywan\w*|labai gera|very good|good condition)\b/.test(normalized)) return true;
    if (/\b(?:condition|bukle|stan)\s*[:\n]\s*(?:new|nauj|now)/.test(normalized)) return false;
    if (/\b(?:condition|bukle|stan)\s*[:\n]\s*(?:good|gera|used|naud|uzyw)/.test(normalized)) return true;
    return null;
  }
  function photo(value) {
    if (Array.isArray(value)) value = value[0];
    if (value && typeof value === "object") value = value.url || value.contentUrl;
    try {
      const url = new URL(value || "", location.href);
      return url.protocol === "https:" && !url.username && !url.password && value ? url.href.slice(0, 2000) : "";
    } catch (_) {return "";}
  }
  function withoutHtml(value) {
    // Detached template: this never inserts markup into the live document or loads images.
    const template = document.createElement("template"); template.innerHTML = String(value || "");
    return clean(template.content.textContent || "");
  }
  const source = sourceFor(location.hostname);
  if (!source || location.protocol !== "https:") return {error: "This is not a supported HTTPS marketplace page.", items: []};
  const pageText = text(document.body), firstText = normalize(document.title + "\n" + pageText.slice(0, 12000));
  const markers = ["verify you are human", "checking your browser", "just a moment", "access denied", "attention required", "unusual traffic", "potwierdz, ze nie jestes robotem", "potwierdz ze nie jestes robotem", "patvirtinkite, kad esate zmogus", "enable javascript and cookies to continue", "captcha verification", "verify that you are not a robot", "please enable js and disable any ad blocker", "please enable javascript and disable any ad blocker"];
  const challenge = markers.some(marker => firstText.includes(marker)) || [...document.querySelectorAll('iframe[src*="captcha"],iframe[src*="challenges.cloudflare.com"],iframe[src*="challenge-platform"]')].some(visible);
  if (challenge) return {error: "This page is showing an access challenge. Finish normal access yourself, then read the loaded listings. The extension does not interact with challenges.", items: []};
  const currency = source === "allegro" ? "PLN" : "EUR";
  const current = listingUrl(location.href, source);

  if (current) {
    const products = [];
    function walk(value) {
      if (Array.isArray(value)) return value.forEach(walk);
      if (!value || typeof value !== "object") return;
      const types = Array.isArray(value["@type"]) ? value["@type"] : [value["@type"]];
      if (types.includes("Product")) products.push(value);
      if (value["@graph"]) walk(value["@graph"]);
      if (value.itemListElement) walk(value.itemListElement);
      if (value.item) walk(value.item);
    }
    for (const node of document.querySelectorAll('script[type="application/ld+json"]')) {
      try {walk(JSON.parse(node.textContent));} catch (_) { /* Invalid embedded metadata is ignored. */ }
    }
    const headingNode = visibleNode("h1");
    const heading = text(headingNode);
    function detailNode(selector) {
      return [...document.querySelectorAll(selector)].filter(visible).find(node => {
        // Ignore a recommendation card even if it uses the same price/description classes.
        for (let ancestor = node, depth = 0; ancestor && depth < 8; ancestor = ancestor.parentElement, depth++) {
          if (headingNode && ancestor.contains(headingNode)) return true;
          if ([...ancestor.querySelectorAll("a[href]")].some(anchor => {
            const linked = listingUrl(anchor.href, source);
            return linked && linked.external_id !== current.external_id;
          })) return false;
        }
        return true;
      });
    }
    const matching = products.filter(product => {
      const offers = Array.isArray(product.offers) ? product.offers : [product.offers || {}];
      const urls = [product.url, ...offers.map(offer => offer?.url)].filter(Boolean);
      return urls.length ? urls.some(url => listingUrl(url, source)?.external_id === current.external_id) : (!heading || normalize(product.name) === normalize(heading));
    });
    const product = matching[0] || {};
    const offers = Array.isArray(product.offers) ? product.offers : [product.offers || {}];
    const offer = offers.find(value => value && (!value.url || listingUrl(value.url, source)?.external_id === current.external_id)) || {};
    const title = clean(heading || product.name, 500);
    const priceNode = detailNode('[itemprop="price"],[data-testid="item-price"],[data-testid="item-price-text"],[data-testid="price"],[data-role="price"],.price,#price');
    let price = money(text(priceNode), currency);
    let actualCurrency = currency;
    if (price === null && offer.price !== undefined && ["EUR", "PLN"].includes(String(offer.priceCurrency || currency).toUpperCase())) {
      price = amount(offer.price); actualCurrency = String(offer.priceCurrency || currency).toUpperCase();
    }
    if (price === null) {
      // Never borrow a price from recommended products elsewhere on the detail page.
      let container = headingNode?.parentElement;
      for (let depth = 0; container && depth < 4; depth++, container = container.parentElement) {
        const otherItems = [...container.querySelectorAll("a[href]")].some(anchor => {
          const linked = listingUrl(anchor.href, source);
          return linked && linked.external_id !== current.external_id;
        });
        if (otherItems) break;
        const quoted = money(text(container), currency);
        if (quoted !== null) {price = quoted; break;}
      }
    }
    if (!title || price === null) return {items: [], source, warnings: ["Could not identify the current item’s title and price. Try waiting for the page to finish rendering."]};
    const descriptionNode = detailNode('[itemprop="description"],[data-testid="item-description"],[data-testid="description"],[data-role="description"],.item-description,#description,.description');
    const image = detailNode('[itemprop="image"] img,img[itemprop="image"],[data-testid*="item-photo"] img,[data-testid*="item-image"] img,article img,main img');
    const conditionNode = detailNode('[itemprop="itemCondition"],[data-testid*="condition"],[data-role="condition"],.condition');
    const used = condition(text(conditionNode) || [offer.itemCondition, product.itemCondition].filter(Boolean).join(" ")) ?? condition(title + "\n" + text(descriptionNode) + "\n" + withoutHtml(product.description));
    const combinedNode = detailNode('[data-testid="total-combined-price"]');
    const combined = money(text(combinedNode), actualCurrency);
    const fees = combined !== null && combined >= price ? Math.round((combined - price) * 100) / 100 : null;
    const shipping = money(text(detailNode('[data-testid="shipping-price"],[data-role="shipping-price"],.shipping-price')), actualCurrency);
    warnings.push("Shipping and fees stay unknown unless shown explicitly; check them before sending.");
    return {source, items: [{...current, title, description: text(descriptionNode) || withoutHtml(product.description), image: photo(image?.currentSrc || image?.src || product.image || document.querySelector('meta[property="og:image"]')?.content), price, currency: actualCurrency, shipping, fees, used}], warnings};
  }

  const found = new Map();
  let missingPrice = 0;
  for (const anchor of document.querySelectorAll("a[href]")) {
    const listing = listingUrl(anchor.href, source);
    if (!listing || found.has(listing.external_id) || !visible(anchor)) continue;
    let node = anchor, card = null;
    for (let depth = 0; node && depth < 9; depth++, node = node.parentElement) {
      const urls = new Set([...node.querySelectorAll("a[href]")].map(element => listingUrl(element.href, source)?.external_id).filter(Boolean));
      if (urls.size > 1) break;
      const content = text(node);
      if (content.length <= 4500 && money(content, currency) !== null) {card = node; break;}
    }
    if (!card) {missingPrice++; continue;}
    const cardText = text(card);
    const image = visibleNode("img", card);
    const titleNode = visibleNode('h2,h3,[data-testid*="title"],.title,.item-title', card);
    let title = clean(text(titleNode) || anchor.getAttribute("title") || image?.alt || text(anchor) || cardText.split("\n")[0], 500);
    if (source === "vinted") title = title.split(/,\s*(?:Prekių ženklas|Būklė|Brand|Condition)\s*:/i)[0].trim();
    const priceNode = visibleNode('[data-testid="item-price-text"],[data-testid="item-price"],[data-testid="price"],[data-role="price"],[itemprop="price"],.price', card);
    const price = money(text(priceNode), currency) ?? money(cardText, currency);
    if (!title || price === null) {missingPrice++; continue;}
    const combined = money(text(visibleNode('[data-testid="total-combined-price"]', card)), currency);
    const shipping = money(text(visibleNode('[data-testid="shipping-price"],[data-role="shipping-price"],.shipping-price', card)), currency);
    found.set(listing.external_id, {...listing, title, description: text(visibleNode(".description,.item-description,.item-desc,.first-dataline", card)), image: photo(image?.currentSrc || image?.src || image?.getAttribute("data-src")), price, currency, shipping, fees: combined !== null && combined >= price ? Math.round((combined - price) * 100) / 100 : null, used: condition(cardText + "\n" + (anchor.getAttribute("title") || ""))});
    if (found.size >= 100) {warnings.push("Preview is limited to 100 rendered listings. Send a smaller selection or open another results page."); break;}
  }
  if (missingPrice) warnings.push(`${missingPrice} visible item link${missingPrice === 1 ? "" : "s"} skipped because a title or price was unavailable.`);
  warnings.push("Search pages often omit descriptions and condition. Open an item page for better matching data.");
  return {source, items: [...found.values()], warnings};
})();
