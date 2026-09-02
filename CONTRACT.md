# Product Pulse — backend/frontend contract

Backend: FastAPI (`app.py`), port 8020. Frontend: `static/index.html` (+ `static/app.js`, `static/style.css`), vanilla JS, no build step.
Design source of truth: `design/product-pulse-v2.dc.html` (Claude Design export). The frontend must reproduce its three screens
(Intake → Generating → Report) pixel-faithfully in look and feel, but every number/label/link comes from live data below.

## HTTP

- `GET /` → static/index.html
- `GET /api/pulse?url=<walmart url>&mode=live|cached` → **SSE stream** (text/event-stream). Events (`event: <name>` + `data: <json>`):
  - `resolve`  `{ "id":"1967919184", "url":"<normalized walmart url>", "name":"Ninja AF101 Air Fryer, 4-Quart …", "brand":"Ninja", "model":"AF101", "short":"Ninja / AF101 (4 qt)", "aliases":[{"text":"Ninja AF101","support":0.99},…] }`
     `aliases` arrive in 1–3 items; `support` is a 0–1 fraction of resolved web listings whose title matched the alias (deterministic).
  - `surface`  `{ "key":"youtube", "label":"youtube", "status":"queued|scanning|done|thin|indirect|degraded", "n":12, "note":"…" }` — one event per status change. Surface keys in order: `reddit, youtube, tiktok, news, forums, retail, cpsc`. (`retail` = amazon/target/bestbuy review pages; walmart.com is never a sentiment source.)
  - `count`    `{ "mentions": 87 }` — total unique external mentions retrieved for the last 90 days (this is what the big animated number shows; caption "external mentions found in the last 90 days").
  - `report`   full report JSON (schema below). After this the frontend may switch to the Report view (auto after the count animation, or when the user clicks "skip to report").
  - `error`    `{ "message":"…", "code":"not_walmart|not_found|rate_limited|upstream" }`
  - `call`     `{ "endpoint":"/search", "tag":"youtube", "ms":1432, "cost":0.021 }` — optional telemetry, one per Exa call (for an "under the hood" drawer; frontend may ignore).
- `GET /api/report/<id>` → last cached report JSON for that Walmart item id (404 if none).
- `GET /api/presets` → `[{"label":"Ninja AF101 Air Fryer · 4 qt","url":"https://www.walmart.com/ip/…/1967919184"}, …]` (up to 3; slots without a preset are rendered as the dashed "preset slot · team URL" pills).

Intake validation (client AND server): accept `https?://(www\.|business\.)?walmart\.com/ip/(<slug>/)?<digits>` and `walmart.com/reviews/product/<digits>`, with or without scheme, query strings allowed. Anything else → the amber "Only walmart.com product URLs work in this demo." message.

## Report JSON

```jsonc
{
  "id": "1967919184",
  "url": "https://www.walmart.com/ip/Ninja-AF101-…/1967919184",
  "as_of": "2026-09-02T17:04:00Z",             // render as "as_of 2026-09-02 17:04 UTC"
  "from_cache": false,
  "product": { "name": "…", "brand": "Ninja", "model": "AF101", "upc": "622356554572"|null, "category": "air fryer",
               "short": "Ninja / AF101 (4 qt)", "aliases": [{"text":"…","support":0.99}] },
  "surfaces": [ { "key":"youtube","label":"youtube","status":"done","n":12,"note":"" }, … ],   // 7 entries, final statuses
  "mentions": { "total": 87, "last30": 31, "prev30": 24, "velocity_pct": 12 | null, "window_days": 90 },

  "verdict": { "lead": "Positive but cooling.",                       // big serif line, plain
               "em": "One pain cluster is growing:",                 // italic grey part (may be "")
               "accent": "basket coating wear." },                   // red part (may be "")

  "board": [  // exactly 5, in order; drives the Signal board cards and the "N act now · N watch · N clear" legend
    { "key":"sentiment", "num":"01", "title":"Sentiment Pulse", "status":"act|watch|clear|thin",
      "line1":"0.72 · −0.04 / 30d", "line1_color":"grey|amber|red|green", "line2":"1 pain cluster rising +40%", "line2_color":"red" },
    { "key":"price", … "title":"Price History" }, { "key":"listings", … "title":"Listing Radar" },
    { "key":"dupes", … "title":"Internal Dupes" }, { "key":"recall", … "title":"Recall & Safety" }
  ],
  // status colors: act = #FF5A4E (label "act now"), watch = #F5B14A ("watch"), clear = #3DD68C ("clear"), thin = #6E7A94 ("thin data")

  "sentiment": {
    "score": 0.72 | null,            // 0–1; null = insufficient labeled mentions → show "—" and "insufficient signal"
    "delta30": -0.04 | null,         // score(last 30d) − score(30–90d ago)
    "trend_word": "cooling|warming|steady|n/a",
    "score_prev": 0.76 | null,       // for the "0.76 → 0.72 over 30d" caption
    "spark": [0.70, 0.72, …] | null, // 13 points oldest→newest over 90 days; null → hide the sparkline, show "not enough dated mentions"
    "n_labeled": 41,                 // mentions that received a sentiment label
    "retail": { "rating": 4.7, "review_count": 59429, "merchant": "Amazon", "url": "…" } | null,   // shown as a small caption under the score
    "clusters": [                    // pain clusters, ranked; first is rendered as the big article, the rest as compact rows
      { "rank": 1, "title": "basket coating peels", "category": "durability",
        "mentions": 9, "trend": "rising|flat|falling|new", "trend_pct": 40 | null, "first_seen": "2026-06-14" | null,
        "sources": [ {"key":"forums","pct":62}, {"key":"youtube","pct":27}, {"key":"retail","pct":11} ],   // sums to 100, max 3 entries, ordered desc
        "quote": { "text": "…verbatim…", "source_label": "kitchenwarecompare.com", "url": "…" } | null,
        "evidence": [ {"title":"…","url":"…","date":"2026-07-20"|null,"source":"forums"} , … ]   // all mentions in this cluster, for "View all N ↗" (expand inline)
      }
    ],
    "praises": [ { "title": "easy to clean", "mentions": 14, "trend": "flat" }, … ]   // up to 3, for the "what's working" strip
  },

  "price": {
    "walmart": { "price": 119.99, "observed": "2026-08-22", "source_label": "allrecipes.com", "url": "…" } | null,   // last Walmart price observed on the OPEN WEB (walmart.com itself is not crawlable)
    "median_now": 104.99 | null, "median_start": 119.99 | null, "median_change_pct": -13 | null,
    "range": { "low": 89.99, "high": 129.99 } | null,
    "n_observations": 17, "n_merchants": 6,
    "series": {
      "days": 90, "start_date": "2026-06-04",            // day 0 … day 89 = as_of date
      "merchants": [ { "key":"amazon", "name":"Amazon", "color":"oklch(74% 0.12 250)", "long_tail": false,
                       "points": [ {"day": 12, "price": 119.99, "url":"…"}, … ],   // dated observations; frontend draws a step line carrying each price forward to the next point / today
                       "oos_from_day": 72 | null } ],
      "median": [null, …, 104.99],   // 90 values (null before the first observation)
      "low":    [ … 90 ], "high": [ … 90 ]
    },
    "events": [ { "n": 1, "day": 38, "date": "2026-07-12", "label": "Target promo: $119.99 → $109.99", "url": "…", "kind": "price_drop|new_low|new_seller|oos|restock|price_increase" } ],  // ≤ 4, chronological
    "walmart_position": "was lowest 74 days ago" | "lowest now" | "never lowest in window" | "unknown (walmart price not observed)",
    "headline": { "lead": "Web median price ", "accent": "down 13%", "tail": " over 90 days.", "em": "Walmart last observed at $119.99 (Aug 22) — no longer the price anchor." },
    "method_note": "exact-product listings only … Series built from N dated price observations across M merchants; each merchant line carries its last observed price forward."
  },

  "listings": {
    "headline": { "lead": "Walmart is ", "accent": "not the lowest price." } ,   // or "Walmart's last observed price is the web low." / "Walmart price not observed on the open web."
    "range": { "low": 87.99, "high": 149.99 }, "walmart_price": 119.99 | null,
    "ticks": [ { "price": 87.99, "labels": ["Newegg (WovenNest)"] }, … ],   // ≤ 5 tick groups for the price bar, ascending
    "chips": [ { "kind": "price_dropped|new_seller|below_walmart|competitor_oos", "text": "price_dropped · Target −$20 (Aug 29)", "tone": "red|amber|green" } ],
    "rows": [ { "merchant": "Amazon", "price": 119.99 | null, "delta30": -20.00 | null, "delta30_note": "new Aug 20" | null,
                "stock": "in stock|OOS|unknown", "type": "exact|variant|refurbished", "first_seen": "Mar 2026" | null,
                "url": "…", "long_tail": false, "seller": "WovenNest via Newegg" | null } ],
    "n_merchants": 6, "live_date": "2026-09-02",
    "deep_scan": { "available": true, "status": "idle|running|done|error", "note": "Exa Agent + Affiliate.com catalog (~2–3 min)" }
  },

  "dupes": {
    "primary": { "id": "1967919184", "url": "…", "title": "…" },
    "exact":   [ { "id": "516008834", "url": "…", "title": "…", "kind": "exact", "indexed": "2025-08-23" | null, "price": null, "seller": null } ],
    "other":   [ { "id": "…", "url": "…", "title": "…", "kind": "refurbished|variant|accessory", "indexed": null } ],
    "count_exact": 1,
    "note": "walmart.com blocks crawlers, so price and review counts for sibling listings are not readable from the open web; listing identity comes from Exa's index of walmart.com titles.",
    "suggestion": "Consolidate listings or align pricing" | "No action"
  },

  "recall": {
    "verified": "2026-09-02",
    "model_level": { "status": "clear|act|thin", "headline": "No CPSC recall for the AF101 in the last 24 months.",
                     "items": [ { "product": "…", "date": "2025-05-01", "hazard": "…", "units": "…", "url": "…" } ] },
    "brand_family": [ { "product": "SharkNinja Foodi OP300 pressure cookers", "date": "2025-05-01", "hazard": "burn", "units": "~1.8M", "url": "https://www.cpsc.gov/Recalls/…", "why": "sibling product, not this model" } ],
    "complaint_scan": { "count": 0, "items": [ {"title":"…","url":"…","date":"…"} ] }
  },

  "cost": { "calls": 17, "dollars": 0.31 }
}
```

## Frontend behaviours (from the design)
- Intake: hero, URL input (Enter submits), "Check the pulse →", amber error line, preset pills from `/api/presets` (click fills the input), footer strip.
- Generating: thin progress bar at top (advance by stage: resolve 25% → surfaces 25→80% → count 90% → report 100%), "01 · resolving entity" card (name, then `short · WMT:<id>`, alias pills with support, "✓ entity resolved · N web aliases matched"), "02 · scanning surfaces" list driven by `surface` events (queued grey / scanning pulsing blue dot / done ✓ / thin / indirect / degraded), then the big animated count with caption. "skip to report →" bottom-right is enabled once the `report` event has arrived. Auto-advance to Report ~1.5 s after the count animation ends.
- Report: sticky bar (name short · WMT id, as_of pill, "external web only", "New report"); header with product name + walmart link + three pills + the verdict sentence; signal board (5 cards, statuses, legend counts computed from statuses); sections 01–05 exactly as designed but data-driven; price chart in SVG with band/low/high/median/merchant step lines/Walmart line (only if `price.walmart`)/event markers/hover tooltip/merchant toggles; listing radar bar + chips + table with real "open ↗" links; dupes grid; recall cards; footer.
- Every "source ↗ / open ↗ / View all N ↗" is a real link (target=_blank rel=noopener). "View all N" expands the cluster's evidence list inline.
- Empty/thin states must be honest and styled (grey "thin data" status, explanatory sentence), never fabricated numbers.
- `mode=cached` replays the last cached report (for presets); show a small "cached · <as_of>" pill in the sticky bar when `from_cache` is true.
