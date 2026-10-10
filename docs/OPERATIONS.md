# NVG Deal Tracker

Deployed on an Ubuntu server at http://YOUR_LAN_IP:8765. The complete application lives in `/opt/containers/nvg-tracker`.

## Access and Android

All dashboard pages require HTTP Basic authentication. Username is `admin`; the generated password is stored in the owner-readable `.env` file. Change `ADMIN_PASSWORD` there and recreate the web container if needed. On Android install ntfy, subscribe to the random topic shown in Settings, use `https://ntfy.sh`, allow Android notification permission and background delivery, then press Test notification. Topic names are secret bearer identifiers on the public ntfy server. Matching listing details are sent outbound to ntfy; no inbound public services are created.

## Actual integration coverage

Public website monitoring is now implemented with ordinary Playwright Chromium, as explicitly requested by the user after deployment. Vinted and Skelbiu use browser search results and item detail pages. Allegro uses the official API when approved credentials are configured, otherwise browser pages. No private marketplace API, CAPTCHA solver, stealth fingerprint modification, proxy rotation or access-control bypass is implemented. A visible challenge or HTTP 401/403/429 stops that source and imposes a one-hour cooldown, including on Scan now. Browser monitoring is not claimed to be marketplace-approved.

- **Vinted.lt:** visible listing cards provide titles, images, condition, item price and the displayed total including buyer-protection fees. Shipping remains unknown unless supplied. Detail pages are read for description/generation. Live search pages were accessible from the deployment host.
- **Skelbiu.lt:** browser searches use the rendered page's `cost_max` budget and `orderBy=1` newest-first fields. Cards supply title, description snippet, image, condition and price. Detail pages enrich ambiguous device generation. Live search pages were accessible initially, but a later page returned HTTP 403. Listings read before that block were saved; further browser checks wait for the one-hour cooldown.
- **Allegro.pl:** ordinary browser search adapter plus the existing approved official API adapter. Live browser access returned HTTP 403; the source is currently blocked and uses the one-hour cooldown. Approved API credentials remain optional and are required only to use that API.
- **PLN/EUR:** official NBP exchange-rate API works; cached for 24 hours, last rate retained for at most seven days during outages. PLN scans/imports defer if no recent rate is available.
- **ntfy:** real outbound publication and retrieval tested. Phone receipt requires the Android subscription.

The latest automated suite has 41 passing tests. Live scan counts vary; consult the source panel for the current deployment state.

Browser coverage is deliberately bounded: first rendered results page per keyword, then up to twelve relevant detail pages per source per cycle, prioritizing recognized target categories. It is partial coverage, explicitly labeled in source status. Failed/unrecognized search pages are logged; existing extracted listings are retained if a later page presents an access challenge. Current marketplace layout changes can require parser maintenance. An empty result page is accepted only when recognizable empty-state text is present; unrecognized layout is never silently reported as a successful zero-result scan.

References: https://www.vinted.lt/terms-and-conditions ; https://www.skelbiu.lt/taisykles/bendrosios-salygos ; https://github.com/allegro/allegro-api/issues/12281 ; https://api.nbp.pl/ ; https://docs.ntfy.sh/publish/

## Behavior

The background worker starts immediately and runs every 15 minutes by default. A Scan now request is processed within roughly five seconds when the worker is idle and also works while automatic monitoring is paused. Pause stops scheduled scans; already queued notifications can still be delivered. Source toggles stop collection and matching alerts after the queued settings scan. Filters are case/accent insensitive and use English, Lithuanian and Polish keywords. Night-vision context is required to prevent unrelated “Gen 2” products from matching. Gen 2 / Gen 2+ are prioritized over unknown analog and NVG30/NVG50. Known Gen 1, thermal, FPV, ordinary digital and replicas are excluded. Text classification is conservative, not seller verification: unknown analog items require generation confirmation, and listings with insufficient text may be omitted. New condition is excluded; unknown condition is labeled for review.

Maximum budget compares **known total strictly below the configured amount**, default EUR 400. Provided shipping and fees are added before conversion. Unknown costs remain explicitly unknown, never silently claimed as free. Vinted displayed buyer fees are included when available. Allegro lowest API shipping is not a promise of shipment to Lithuania. No item's current availability is guaranteed from manual imports.

The SQLite database keeps settings, listings, first/last seen times, price/currency/cost/rate snapshots, favorite/ignore state, source statuses, logs and a notification outbox. Duplicate imports do not cause duplicate new-listing alerts. New alerts use a stable marketplace + item ID. Price drops are based on the previous known total and default to 10%; a repeated alert for the same item/price is suppressed. Failed notifications retry with exponential backoff. ntfy has no server-side idempotency contract; a timeout after a successful remote acceptance can result in a duplicate retry. Ignored/nonmatching listings cancel their pending alerts.

## Imports

Dashboard Import listings supports a form and JSON arrays. Automated permitted integrations can POST JSON arrays to `/api/import` with `Authorization: Bearer <IMPORT_TOKEN from .env>`. This endpoint is LAN restricted and does not accept browser Basic authentication as authorization. Alternatively place `*.json` files in `data/inbox`; Scan now or the next scheduled scan reads them. Successful files become `.processed`; rejected files become `.failed` and are logged. Disabled-source entries are skipped. Imports are normalized and validated before processing. PLN imports defer until the rate is available.

Example schema (example only, not live data):

```json
[{"source":"vinted","external_id":"stable-marketplace-item-id","title":"Used analog Gen 2 night vision monocular","description":"Seller supplied description","url":"https://www.vinted.lt/items/stable-marketplace-item-id","image":"","price":250,"currency":"EUR","shipping":null,"fees":null,"used":true}]
```

## Operations

```sh
cd /opt/containers/nvg-tracker
docker compose ps
docker compose logs --tail 50
docker compose up -d --build --wait
```

Two dedicated containers (`web` and `worker`) use restart `unless-stopped`; the host Docker service is enabled at boot. No existing services are modified. SQLite/inbox are bind-mounted from `./data`; no data is stored only inside containers. Images run as UID 1000 with read-only root filesystems, dropped capabilities and no-new-privileges. Chromium is installed inside the image, with a private writable temporary configuration/cache directory. The worker has a 1.5 GiB memory limit, 1.5 CPU limit, 512 MiB temporary directory and 256 MiB shared memory. Logs are rotated. The web port is published only on `YOUR_LAN_IP`; middleware permits `YOUR_LAN_CIDR` and loopback and ignores forwarded client IP headers. No public ingress or Cloudflare tunnel configuration was added. If LAN address changes, update `LAN_IP`/`ALLOWED_CIDRS` in `.env` and recreate containers. Router forwarding was not inspected. The fixed address should remain assigned to the server.

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

Automated tests run with network disabled and isolated temporary databases. The suite now includes Playwright rendering of mocked search/detail pages, listing/fee parsing and access-challenge detection. Tests run with the same read-only/capability restrictions as deployment. They cover multilingual classification/exclusions, exact price threshold and PLN costs, persistent flags, notification deduplication and failure retries, mock Allegro pipeline and denied access, exchange-rate caching, HTML route rendering, Basic authentication, CSRF checks, source IP filtering and authenticated imports.

```sh
docker run --rm --network none --read-only --tmpfs /tmp:size=512m --cap-drop ALL --security-opt no-new-privileges -e DATA_DIR=/tmp/test-data -e XDG_CONFIG_HOME=/tmp/chromium-config -e XDG_CACHE_HOME=/tmp/chromium-cache nvg-tracker:local python -m pytest -q -p no:cacheprovider /srv/tests
```

`tests/mock_marketplaces.json` is exclusively a fixture, not deployed listings. The deployment was additionally checked using a synthetic above-budget import and subsequent cleanup. Live HTTP pages, NBP retrieval, ntfy acceptance, SQLite integrity and container restart/persistence were checked. Desktop (1440px) and mobile (390px) Playwright checks verified all four dashboard pages, no horizontal overflow and no JavaScript errors. Android receipt itself is not verified. The dashboard source statuses are the authority for live source access and recent check results.
