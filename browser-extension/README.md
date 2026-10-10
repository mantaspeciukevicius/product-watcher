# Send marketplace listings to your local tracker

This Chrome/Edge/Brave extension reads the page **you have opened** when you press **Read this page**. Review or correct the extracted listings, select the items you want, and press **Send selected listings**. Your tracker’s normal price conversion, filtering, feedback, history and notification rules handle the imported data.

It supports visible search results and item detail pages on Vinted.lt, Skelbiu.lt, Allegro.pl and AllegroLokalnie.pl. Marketplaces change their layouts, so always review the preview. Open a detail page if a search card does not provide enough information for matching.

## Install on your PC

1. Open your dashboard on your home network and go to **Import listings**.
2. Download the extension ZIP and extract it into a permanent folder. The dashboard download may already contain your server address and import token; keep that ZIP private.
3. Open `chrome://extensions` in Chrome or Brave, or `edge://extensions` in Edge.
4. Turn on **Developer mode**, select **Load unpacked**, and choose the extracted folder containing `manifest.json`.
5. Pin **Product Watcher — Send to Tracker** using your browser’s extensions menu.
6. Open the extension, enter your dashboard URL and import token if they are not prefilled, then press **Connect / Test connection**. Allow access to that local address. If your browser asks about local-network access, allow your home tracker.

The generic extension source does not contain your address, credentials or token. Get the token from the protected **Import listings** page. Example URL format: `http://YOUR_LAN_IP:8765`; use the actual LAN URL shown on your dashboard.

## Use it

1. Open a marketplace search or listing normally in the same browser. Sign in or complete normal access yourself if required.
2. Open the extension and press **Read this page**.
3. Review titles, prices, EUR/PLN currency, condition, descriptions and optional photo URLs. Deselect unrelated products. Blank shipping and fees mean **unknown**, not zero; enter costs when you know them. Imported data does not include your account, cookies or browsing history.
4. Press **Send selected listings**. Look for the queued confirmation, then check the tracker. The worker processes imports even while automated marketplace scans are waiting or blocked.

Only rendered items are read. Scroll to load more results and read again; the extension does not scroll, click, navigate or fetch marketplace pages for you. It sends only your reviewed selection. Existing listings are deduplicated by the tracker, and changed prices enter the usual price history.

Unknown condition remains unknown. New items can be recorded but the tracker’s normal used-device filter excludes them from matching alerts. Search cards with no description may remain unclassified until you import the detail page.

## Privacy and permissions

- `activeTab` and `scripting` allow one user-triggered read of your active marketplace tab. There are no persistent marketplace host permissions or automatic content scripts.
- `storage` saves configuration locally in the extension; it does not synchronize your token to your browser account.
- The manifest declares optional HTTP/HTTPS host patterns because a home server’s address is unknown in advance. **Connect requests only the configured local host**, and the background worker accepts private addresses, localhost and `.local`/`.lan`/`.home` hostnames. No public tracker URLs are accepted.
- The import token stays in the extension’s background/popup context and is never passed into marketplace pages. Requests are made directly to the configured tracker with Bearer authentication, without cookies and without following redirects.
- No CAPTCHA interaction, access challenge bypass, proxy, private marketplace API, extra marketplace requests or unattended browser monitoring is included.

Do not publish a paired ZIP or `paired-config.json`. If one is disclosed, rotate the import token on the server and reconnect the extension. A shared folder containing the unpacked extension should also be private. Browser extension storage is not an encrypted password vault.

## Troubleshooting

- **Cannot connect:** open the dashboard URL directly in your PC browser, check home Wi-Fi/LAN and server availability, then try Connect again. The tracker is intended for your local network.
- **Token rejected:** copy the current import token from the protected dashboard. The dashboard’s administrator password is a different credential.
- **No listings:** wait for page rendering, scroll normally, or open a detail page. Unsupported layouts and missing prices are not guessed.
- **Access challenge:** finish normal access yourself. This extension refuses to read a challenge page.
- **No notification:** check the item’s category, price, condition and your feedback, source enablement and notification settings. An import is not a promise that the item meets your filters.
- **Popup closed:** reopen and read the page again. Previews are kept only while the popup is open; settings remain saved.

## Development

Source files use plain JavaScript and require no build tools or third-party libraries. Extraction tests run only on mock pages using Playwright; they never contact a marketplace. Run `pytest tests/test_extension.py` in the project’s Python/Playwright environment.
