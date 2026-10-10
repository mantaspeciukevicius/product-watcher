"""Manual browser import extraction/security checks against local mock HTML only."""
import json
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1] / "browser-extension"
EXTRACT = (ROOT / "extract.js").read_text()
CONFIG = (ROOT / "config.js").read_text()
BACKGROUND = (ROOT / "background.js").read_text()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        chromium = playwright.chromium.launch(args=["--no-sandbox"])
        yield chromium
        chromium.close()


def extract(browser, html, url="https://www.vinted.lt/catalog"):
    context = browser.new_context()
    page = context.new_page()
    # All requests, including any mock photo/CSS URLs, are fulfilled locally.
    context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html) if route.request.is_navigation_request() else route.abort())
    page.goto(url)
    result = page.evaluate(EXTRACT)
    context.close()
    return result


def test_vinted_batch_deduplicates_and_ignores_hidden_and_wrong_hosts(browser):
    html = """
    <article><a href='/items/123-nvg30?ref=search' title='NVG30, Būklė: Labai gera'><img alt='device'></a>
    <a href='/items/123-nvg30'>NVG30</a><span data-testid='item-price-text'>150,50 €</span>
    <span data-testid='total-combined-price'>158,52 €</span><p>Naudota</p></article>
    <article><a href='/items/456-nvg50'>NVG50</a><span>€ 210.25</span></article>
    <article style='display:none'><a href='/items/789-hidden'>Hidden</a><span>100 €</span></article>
    <article><a href='https://www.vinted.lt.evil.example/items/999-fake'>Fake</a><span>100 €</span></article>
    """
    result = extract(browser, html)
    assert len(result["items"]) == 2
    item = result["items"][0]
    assert item["external_id"] == "123" and item["title"] == "NVG30"
    assert item["url"] == "https://www.vinted.lt/items/123-nvg30"
    assert item["price"] == 150.50 and item["fees"] == 8.02
    assert item["shipping"] is None and item["used"] is True
    assert result["items"][1]["price"] == 210.25


def test_skelbiu_batch_does_not_mix_neighbor_prices(browser):
    html = """
    <section>
      <article><h2>Analog night vision Gen 2</h2><a href='/skelbimai/analog-device-778899.html'>View</a><p>299 €</p></article>
      <article><h2>Unknown price device</h2><a href='/skelbimai/unknown-889900.html'>View</a></article>
      <article><h2>Thermal camera</h2><a href='/skelbimai/thermal-990011.html'>View</a><p>110 €</p></article>
    </section>"""
    result = extract(browser, html, "https://www.skelbiu.lt/paieska/naktinis/")
    assert [item["external_id"] for item in result["items"]] == ["778899", "990011"]
    assert result["items"][0]["title"] == "Analog night vision Gen 2"
    assert result["items"][0]["price"] == 299
    assert result["items"][1]["price"] == 110


def test_allegro_prices_and_unknown_costs(browser):
    html = """<article><h2>Noktowizor Gen 2+</h2><a href='/oferta/noktowizor-1234567890?track=abc'>View</a>
    <div class='price'>1 299,99 zł</div><p>Stan: Używany</p></article>"""
    item = extract(browser, html, "https://allegro.pl/listing?string=noktowizor")["items"][0]
    assert item["external_id"] == "1234567890" and item["currency"] == "PLN"
    assert item["price"] == 1299.99 and item["used"] is True
    assert item["shipping"] is None and item["fees"] is None


def test_detail_uses_current_product_schema_not_recommendations(browser):
    schema = {"@graph": [
        {"@type": "Product", "name": "Unrelated recommended camera", "url": "https://www.vinted.lt/items/999-other", "offers": {"price": "15", "priceCurrency": "EUR"}},
        {"@type": "Product", "name": "NVG30", "url": "https://www.vinted.lt/items/123-nvg30", "description": "<p>Used digital NVG30.</p>", "image": "https://cdn.example/device.jpg", "offers": {"price": "220.00", "priceCurrency": "EUR", "itemCondition": "https://schema.org/UsedCondition"}},
    ]}
    html = f"<h1>NVG30</h1><script type='application/ld+json'>{json.dumps(schema)}</script>"
    result = extract(browser, html, "https://www.vinted.lt/items/123-nvg30?tracking=abc")
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["price"] == 220 and item["used"] is True
    assert item["description"] == "Used digital NVG30."
    assert item["image"] == "https://cdn.example/device.jpg"


def test_detail_visible_fields_and_new_condition(browser):
    html = """<main><h1>Noktowizor NVG50</h1><div data-role='price'>499,00 PLN</div>
    <p data-role='condition'>Nowy</p><div data-role='description'>Skaitmeninis NVG50</div></main>"""
    item = extract(browser, html, "https://allegro.pl/oferta/nvg50-123123123")["items"][0]
    assert item["price"] == 499 and item["used"] is False
    assert item["description"] == "Skaitmeninis NVG50"


def test_detail_does_not_borrow_recommended_price_or_condition(browser):
    html = """<main><h1>Analog Gen 2 night vision</h1><div class='price'>250 €</div>
    <section><a href='/items/999-baby-camera'>New baby camera</a><p class='condition'>Nauja</p><div class='price'>50 €</div></section></main>"""
    item = extract(browser, html, "https://www.vinted.lt/items/123-analog")["items"][0]
    assert item["price"] == 250 and item["used"] is None
    without_current_price = html.replace("<div class='price'>250 €</div>", "")
    assert extract(browser, without_current_price, "https://www.vinted.lt/items/123-analog")["items"] == []


def test_explicit_shipping_and_european_thousands(browser):
    html = """<main><h1>NVG30</h1><div class='price'>1.299,99 EUR</div>
    <p data-role='shipping-price'>5,50 EUR</p></main>"""
    item = extract(browser, html, "https://www.vinted.lt/items/123-nvg30")["items"][0]
    assert item["price"] == 1299.99 and item["shipping"] == 5.5
    assert item["fees"] is None


def test_allegrolokalnie_slug_ids_are_stable_and_separate(browser):
    html = "<h1>Noktowizor NVG30</h1><div class='price'>900 zł</div>"
    item = extract(browser, html, "https://allegrolokalnie.pl/oferta/noktowizor-nvg30-abcxyz?ref=a")["items"][0]
    assert item["external_id"] == "local:noktowizor-nvg30-abcxyz"
    assert item["currency"] == "PLN" and item["price"] == 900


@pytest.mark.parametrize("body", [
    "<title>Just a moment...</title><p>Checking your browser</p>",
    "<p>Potwierdź, że nie jesteś robotem</p>",
    "<iframe src='https://challenges.cloudflare.com/challenge'></iframe>",
])
def test_challenge_pages_refuse_extraction(browser, body):
    result = extract(browser, body + "<article><a href='/items/123-nvg30'>NVG30</a>100 €</article>")
    assert result["items"] == [] and "challenge" in result["error"]


def test_wrong_host_rejected_even_with_legitimate_looking_links(browser):
    result = extract(browser, "<a href='https://vinted.lt/items/123'>NVG30</a> 100 €", "https://vinted.lt.evil.example/")
    assert result["items"] == [] and "supported" in result["error"]


def test_config_accepts_private_networks_and_rejects_public_and_credentials(browser):
    page = browser.new_page()
    values = ["http://192.168.0.22:8765", "https://10.0.0.3:8765/", "http://172.31.8.2:8765", "http://localhost:8765", "http://home-server.local:8765", "http://[fd00::3]:8765", "https://example.com", "http://8.8.8.8:8765", "http://172.32.0.2:8765", "http://192.168.0.22.evil.example", "http://u:p@192.168.0.22:8765", "http://192.168.0.22:8765/path", "http://192.168.0.22:8765?token=a"]
    result = page.evaluate("values => {" + CONFIG + "; return values.map(v => {try {return TrackerConfig.dashboardUrl(v)} catch (_) {return null}});}", values)
    assert all(result[:6]) and result[6:] == [None] * 7
    page.close()


def test_background_sender_auth_and_private_import_request(browser):
    page = browser.new_page()
    result = page.evaluate("""async ({config,background}) => {
      const requests = [], listeners = [];
      const stored = {trackerSettings:{dashboardUrl:'http://192.168.0.22:8765',importToken:'test-import-token'}};
      globalThis.importScripts = () => {};
      globalThis.chrome = {
        storage:{local:{get:async()=>stored,set:async value=>Object.assign(stored,value)}},
        permissions:{contains:async()=>true},
        runtime:{getURL:path=>'chrome-extension://testid/'+path,onMessage:{addListener:fn=>listeners.push(fn)}}
      };
      globalThis.fetch = async (url, options) => {requests.push({url,options});return {ok:true,json:async()=>({queued:1})}};
      eval(config + '\\n' + background);
      const listen=listeners[0];
      const external=listen({action:'settings'},{url:'https://www.vinted.lt/items/123'},()=>{});
      const imported=await new Promise(resolve=>listen({action:'import',items:[{title:'NVG30'}]},{url:'chrome-extension://testid/popup.html'},resolve));
      return {external, imported, requests};
    }""", {"config": CONFIG, "background": BACKGROUND})
    assert result["external"] is False
    assert result["imported"] == {"ok": True, "result": {"queued": 1}}
    request = result["requests"][0]
    assert request["url"] == "http://192.168.0.22:8765/api/import"
    assert request["options"]["headers"]["Authorization"] == "Bearer test-import-token"
    assert request["options"]["credentials"] == "omit" and request["options"]["redirect"] == "error"
    assert json.loads(request["options"]["body"]) == [{"title": "NVG30"}]
    page.close()


def test_manifest_requests_no_persistent_marketplace_access_or_content_scripts():
    manifest = json.loads((ROOT / "manifest.json").read_text())
    assert manifest["manifest_version"] == 3
    assert set(manifest["permissions"]) == {"activeTab", "scripting", "storage"}
    assert "host_permissions" not in manifest and "content_scripts" not in manifest
    assert "externally_connectable" not in manifest
    assert "fetch(" not in EXTRACT and "XMLHttpRequest" not in EXTRACT
    assert not (ROOT / "paired-config.json").exists()


def test_real_mv3_install_background_and_popup(tmp_path, browser):
    """Load the real package in Chromium, not a mocked chrome.runtime."""
    context = browser.browser_type.launch_persistent_context(
            str(tmp_path / "browser-profile"), channel="chromium", headless=True,
            args=["--no-sandbox", f"--disable-extensions-except={ROOT}", f"--load-extension={ROOT}"],
        )
    context.route("https://**/*", lambda route: route.abort())
    worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker")
    extension_id = worker.url.split("/")[2]
    page = context.new_page()
    page.goto(f"chrome-extension://{extension_id}/popup.html")
    assert page.get_by_role("heading", name="Send to your tracker").is_visible()
    assert page.locator("#status").inner_text() == "Open a Vinted, Skelbiu or Allegro search or listing page."
    result = page.evaluate("""async () => {
            const settings=await chrome.runtime.sendMessage({action:'settings'});
            return {settings,permissions:chrome.runtime.getManifest().permissions};
        }""")
    assert result["settings"] == {"ok": True, "result": {"dashboardUrl": "", "importToken": ""}}
    assert "activeTab" in result["permissions"]
    context.close()
