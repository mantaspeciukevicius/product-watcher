# Product Watcher

Run Product Watcher on a trusted local network at `http://YOUR_LAN_IP:8765`. Use a deployment directory appropriate to your host, such as `/opt/containers/product-watcher`.

## Access and Android

All dashboard pages require HTTP Basic authentication. Username is `admin`; the generated password is stored in the owner-readable `.env` file. Change `ADMIN_PASSWORD` there and recreate the web container if needed. On Android install ntfy, subscribe to the random topic shown in Settings, use `https://ntfy.sh`, allow Android notification permission and background delivery, then press Test notification. Topic names are secret bearer identifiers on the public ntfy server. Matching listing details are sent outbound to ntfy; no inbound public services are created.

## Actual integration coverage

Public website monitoring is now implemented with ordinary Playwright Chromium, for the configured marketplace adapters. Vinted and Skelbiu use browser search results and item detail pages. Allegro uses the official API when approved credentials are configured, otherwise browser pages. No private marketplace API, CAPTCHA solver, stealth fingerprint modification, proxy rotation or access-control bypass is implemented. A visible challenge or HTTP 401/403/429 stops that source. Repeated access blocks wait 1, 2, 4, 8, 16 and then 24 hours; a longer valid Retry-After deadline is respected. Scan now obeys the same waits and request limits. Browser monitoring is not claimed to be marketplace-approved.

- **Vinted.lt:** visible listing cards provide titles, images, condition, item price and the displayed total including buyer-protection fees. Shipping remains unknown unless supplied. Detail pages are read for description/generation. Live search pages were accessible from the deployment host.
- **Skelbiu.lt:** browser searches use the rendered page's `cost_max` budget and `orderBy=1` newest-first fields. Cards supply title, description snippet, image, condition and price. Detail pages enrich ambiguous device generation. Live search pages were accessible initially, but a later page returned HTTP 403. Listings read before that block were saved; further browser checks wait for the persisted access backoff.
- **Allegro.pl:** ordinary browser search adapter plus the existing approved official API adapter. Live browser access returned HTTP 403; the source has been blocked during live verification. The existing API adapter requires separately approved access; API credentials alone do not establish permission to retrieve public listings. No new approved API access has been verified.
- **PLN/EUR:** official NBP exchange-rate API works; cached for 24 hours, last rate retained for at most seven days during outages. PLN scans/imports defer if no recent rate is available.
- **ntfy:** real outbound publication and retrieval tested. Phone receipt requires the Android subscription.

Live listing counts change over time; the source panel is the authority for current availability and recent errors.

Browser coverage is deliberately bounded: one keyword per enabled source per step, the first rendered results page for that keyword, and at most two attempted detail reads per step. Across a complete keyword rotation, each source can attempt at most twelve detail reads, including failures. New or changed candidates receive priority. It is partial coverage, explicitly labeled in source status. Failed/unrecognized search pages are logged; existing extracted listings are retained if a later page presents an access challenge. Current marketplace layout changes can require parser maintenance. An empty result page is accepted only when recognizable empty-state text is present; unrecognized layout is never silently reported as a successful zero-result scan.

References: https://www.vinted.lt/terms-and-conditions ; https://www.skelbiu.lt/taisykles/bendrosios-salygos ; https://github.com/allegro/allegro-api/issues/12281 ; https://api.nbp.pl/ ; https://docs.ntfy.sh/publish/

## Behavior

The worker starts automatically. The configured monitoring interval now means a full keyword rotation: the default is 15 minutes; existing saved intervals are preserved. Steps are spread across that interval with at least 60 seconds between source checks; request durations and access backoffs can extend a rotation. Scan now queues at most one eligible keyword per source and works while scheduled monitoring is paused, but cannot bypass the minimum spacing, detail budget or an access block. Pause stops scheduled marketplace checks; imports and queued notification delivery continue. Source toggles disable collection and matching alerts for that source. Filters are case/accent insensitive and use English, Lithuanian and Polish keywords. Night-vision context is required to prevent unrelated “Gen 2” products from matching. Gen 2 / Gen 2+ are prioritized over unknown analog and NVG30/NVG50. Known Gen 1, thermal, FPV, ordinary digital and replicas are excluded. Text classification is conservative, not seller verification: unknown analog items require generation confirmation, and listings with insufficient text may be omitted. New condition is excluded; unknown condition is labeled for review.

Maximum budget compares **known total strictly below the configured amount**, default EUR 400. Provided shipping and fees are added before conversion. Unknown costs remain explicitly unknown, never silently claimed as free. Vinted displayed buyer fees are included when available. Allegro lowest API shipping is not a promise of shipment to Lithuania. No item's current availability is guaranteed from manual imports.

The SQLite database keeps settings, listings, first/last seen times, price/currency/cost/rate snapshots, favorite/ignore state, source statuses, logs and a notification outbox. Duplicate imports do not cause duplicate new-listing alerts. New alerts use a stable marketplace + item ID. Price drops are based on the previous known total and default to 10%; a repeated alert for the same item/price is suppressed. Failed notifications retry with exponential backoff. ntfy has no server-side idempotency contract; a timeout after a successful remote acceptance can result in a duplicate retry. Ignored/nonmatching listings cancel their pending alerts.

## Browser and manual imports

Import listings provides a paired PC extension, a mobile-friendly single-listing form, and JSON arrays. An independent worker task polls `data/inbox` about every five seconds, including while automatic monitoring is paused, a source is blocked, or another browser check is running. Imports do not request an extra marketplace search. They use the same categories, relevance feedback, deduplication, price history and notification rules as monitored listings.

To install on a PC with Chrome, Edge or Brave:

1. Open **Import listings** in the authenticated dashboard, download the paired extension ZIP, and extract it to a permanent private folder.
2. Open `chrome://extensions` or `edge://extensions`, enable **Developer mode**, choose **Load unpacked**, and select the extracted `product-watcher-extension` folder containing `manifest.json`.
3. Pin **Product Watcher — Send to Tracker**, open its popup, and press **Connect / Test connection**. Allow access to the configured local tracker. The paired download already contains this server's LAN address and import token.
4. Open a supported marketplace search or listing normally, press **Read this page**, review or correct prices, condition and costs, select listings, then send them.

The Manifest V3 extension reads only currently rendered content after an explicit click. It does not fetch marketplace pages, scroll, navigate or interact with access challenges. A detail page generally provides richer matching information than a search card. Review the preview: marketplace layout changes can affect extraction. Normal Android Chrome does not support this desktop extension; use the dashboard form on Android.

The paired ZIP and unpacked `paired-config.json` contain the private import token. Keep them private; generic extension source contains no pairing secrets. Configuration is stored locally rather than synchronized to a browser account. The token is never injected into marketplace pages, and account sessions, passwords and cookies are not imported. If the token is exposed, rotate `IMPORT_TOKEN` in `.env`, recreate the web container, and reconnect the extension. A downloaded ZIP does not rotate itself.

Authorized local integrations can POST an array to `/api/import` with `Authorization: Bearer <IMPORT_TOKEN>`, or check `/api/import/status` with the same token. These routes retain the LAN restriction and accept extension origins, not arbitrary website origins. Dashboard Basic authentication does not authorize API imports. Alternatively place `*.json` files in `data/inbox`; no Scan now is needed. Successful files become `.processed`; invalid files become `.failed` and are logged. Disabled-source entries are skipped. A batch with enabled PLN entries waits intact until a valid rate is available; disabled PLN sources do not hold up enabled EUR entries. The dashboard reports pending files and the last imported count/time. Search-card imports with an unchanged title preserve a saved description, photo and known condition when the new preview omits them. Current price, shipping and fees remain authoritative, including unknown costs. The roughly five-second inbox poll is not a phone-delivery guarantee: notification delivery can wait for an active marketplace check to finish.

Example schema (example only, not live data):

```json
[{"source":"vinted","external_id":"stable-marketplace-item-id","title":"Used analog Gen 2 night vision monocular","description":"Seller supplied description","url":"https://www.vinted.lt/items/stable-marketplace-item-id","image":"","price":250,"currency":"EUR","shipping":null,"fees":null,"used":true}]
```

## Operations

```sh
cd /opt/containers/product-watcher
docker compose ps
docker compose logs --tail 50
docker compose up -d --build --wait
```

Two dedicated containers (`web` and `worker`) use restart `unless-stopped`; the host Docker service is enabled at boot. No existing services are modified. SQLite/inbox are bind-mounted from `./data`; no data is stored only inside containers. Images run as UID 1000 with read-only root filesystems, dropped capabilities and no-new-privileges. Chromium is installed inside the image, with a private writable temporary configuration/cache directory. The worker has a 1.5 GiB memory limit, 1.5 CPU limit, 512 MiB temporary directory and 256 MiB shared memory. Logs are rotated. The web port is published only on `YOUR_LAN_IP`; middleware permits `YOUR_LAN_CIDR` and loopback and ignores forwarded client IP headers. No public ingress or Cloudflare tunnel configuration was added. If LAN address changes, update `LAN_IP`/`ALLOWED_CIDRS` in `.env` and recreate containers. Router forwarding was not inspected. Keep the configured LAN address assigned to the deployment host.

Secrets are in `.env` (mode 0600) and the owner-only `data` directory; exclude both from version control/backups shared with others. SQLite settings contain configured API secrets. Back up consistently with Python SQLite's backup API, not a raw copy of an active WAL database:

```sh
docker compose exec -T worker python - <<'PY'
import sqlite3
source = sqlite3.connect('/data/tracker.sqlite3')
destination = sqlite3.connect('/data/tracker-backup.sqlite3')
source.backup(destination)
destination.close()
source.close()
PY
```

## Verification

Automated tests run with network disabled and isolated temporary databases. The suite now includes Playwright rendering of mocked search/detail pages, listing/fee parsing and access-challenge detection. Tests run with the same read-only/capability restrictions as deployment. They cover multilingual classification/exclusions, exact price threshold and PLN costs, persistent flags, notification deduplication and failure retries, mock Allegro pipeline and denied access, exchange-rate caching, HTML route rendering, Basic authentication, CSRF checks, source IP filtering, authenticated imports, extension-origin/private-network CORS, paired ZIP secret handling, paused import processing, keyword pacing and persistent backoff.

```sh
docker run --rm --network none --read-only --tmpfs /tmp:size=512m --cap-drop ALL --security-opt no-new-privileges -e DATA_DIR=/tmp/test-data -e XDG_CONFIG_HOME=/tmp/chromium-config -e XDG_CACHE_HOME=/tmp/chromium-cache product-watcher:local python -m pytest -q -p no:cacheprovider /srv/tests
```

`tests/mock_marketplaces.json` is exclusively a fixture, not deployed listings. The live import, worker, database, history and flags path was additionally checked using a synthetic above-budget record and subsequent cleanup. Live HTTP pages, NBP retrieval, ntfy acceptance, SQLite integrity and container restart/persistence were checked. Desktop (1440px) and mobile (390px) Playwright checks verified all four dashboard pages, no horizontal overflow and no JavaScript errors. Android receipt itself is not verified. The dashboard source statuses are the authority for live source access and recent check results.

## Discovery sorting and relevance feedback

Listings default to Found: newest first, with oldest-first, lowest-price and favorites/Gen 2 priority options. Discovery time is the first time this tracker saved an item, not the marketplace publication date. Cards show that timestamp, which later scans preserve.

Irrelevant is distinct from Ignore. Marking an item irrelevant saves a persistent negative example and immediately filters its matches and pending alerts, plus sufficiently similar titles and learned baby/security camera families. Future scans apply the same rules. This is conservative rule-based learning, not a trained AI model; it uses titles rather than seller descriptions to avoid broad accidental exclusions. Explicit NVG30/NVG50/PVS-7/PVS-14 titles are protected from camera-family generalization. The Irrelevant tab shows both manual examples and learned exclusions; Undo irrelevant removes the example and recalculates saved results. Undo does not immediately send historical alerts. All history remains visible in All history. Ignore suppresses only the individual item and does not teach the filter.

## Efficiency update (11 October 2026)

Successful marketplace detail reads are cached in SQLite for 24 hours. Changes to a search card invalidate its cache immediately; new/changed items receive the detail budget before expired unchanged ones. Cached descriptive metadata never replaces fresh search-card prices, currency, shipping, fees or condition. Descriptions that change without any visible card change may take up to 24 hours to refresh. Ignored/irrelevant items remain in history but do not consume detail requests. The budget is two attempted details per source step and twelve per full keyword rotation, including failed attempts. Cached entries can be reused without consuming fresh detail requests.

Ingestion uses transactions of up to 50 listings with one prepared feedback snapshot per transaction. Explicit write transactions prevent concurrent feedback from being overwritten by a stale read. Full history is rechecked when matching filters or the exchange rate change; unchanged filter passes do no listing rewrites. During a rate outage saved PLN amounts can still be refiltered using their saved conversion, with new PLN alerts deferred. Feedback normalization/tokenization is cached per snapshot and unchanged feedback refreshes skip writes.

An independent 15-second heartbeat keeps worker health current during long asynchronous scans. Worker restarts retain each source's persisted next check, keyword position and access backoff instead of immediately contacting every marketplace again; Scan now stays bounded by those limits. Dashboard status includes latest scan duration and historical records rechecked. Manual test notifications are marked in-flight before publishing to avoid duplicate worker delivery.

Automated verification includes mocked marketplace cache/expiry/changed-price behavior, irrelevant skips, detail priority, transaction counts, feedback parity, heartbeat, restart scheduling and notification concurrency. Synthetic benchmarks (not end-to-end marketplace speed): 500 unchanged listings / 25 negative examples reduced a history pass from 0.430 seconds and 500 connections to 0.00046 seconds and one connection; 100 feedback examples / 1,000 candidates yielded identical decisions in 0.036 seconds versus 0.934 seconds. Actual scans remain limited by marketplace response times and first-page coverage.

Cached exclusions and new-condition details are refreshed after expiry when current search cards remain eligible. A cached known condition is reused when unchanged cards omit condition; explicit current card conditions always take precedence.

## Request pacing and block recovery

Each enabled source rotates through deduplicated search keywords in the order saved in Settings. The per-source step spacing is `max(60 seconds, interval / keyword count)`. Settings keeps existing saved monitoring intervals. **Browser request spacing** defaults to ten seconds and can be set from three to sixty seconds; this is the delay between actual page navigations, separate from the interval for a complete keyword rotation.

The scheduler persists reservations before making requests, so concurrent manual requests or a worker restart cannot repeat a source step inside the minimum sixty-second window. At most two detail pages are attempted per step and twelve per keyword rotation. A Scan now request advances only the next eligible keyword; repeated presses do not create a full burst of all searches.

Access blocks use persisted exponential waits of 1, 2, 4, 8, 16 and then 24 hours, with longer server Retry-After deadlines retained. The source panel reports the next eligible check and repeated-block count. Successful browser access resets the block escalation. These changes reduce request volume and bursts; they do not guarantee access to a marketplace that rejects ordinary automated browsers. User-opened browser imports remain available while automated checks wait.
