# Product Pulse

**Paste one walmart.com product URL. Get one report from the open web — sentiment, price history,
live listings, internal dupes, recall status — every number traceable to a dated, clickable source.**

Live demo: **[pulse.salua.ai](https://pulse.salua.ai)** · built for the Exa work trial (Walmart track) ·
design: Claude Design → `design/product-pulse-v2.dc.html`

---

## What it does

```
input:   https://www.walmart.com/ip/<anything>/<item id>      (any Walmart product URL, slug optional)
output:  a five-section pulse report, streamed while it is built (~30 s, ~$0.82 of Exa credits, 28 calls)
```

| # | Section | Question it answers | Exa work behind it |
|---|---|---|---|
| 00 | Entity resolution | Which product is this, and what does the web call it? | `/answer` + `outputSchema` (brand, model, UPC, category, aliases) cross-checked against Exa's index of walmart.com titles. Alias *support* = share of retrieved listings whose title matches the alias. |
| 01 | Sentiment Pulse | How do people actually feel about it? | Non-social open web only, 12-month window, five surfaces in parallel: **retail review pages** (15 Amazon / Target / Best Buy / Home Depot… pages, schema-extracted complaints and praises, walmart.com excluded), **owner & expert reviews** (neural + a 50-result keyword pass + a complaints pass, big-box and video domains excluded), **forums & communities** (`includeDomains`: Quora, Slickdeals, RedFlagDeals, Eurobricks, StackExchange, hobby forums…), **news** (`category: news`, 30 results) and **Reddit as quoted by third parties** (`/answer` with a quote schema, plus RedditRecs). Each `/search` carries `highlights` + a schema `summary` that labels sentiment, whether a complaint is actually voiced, its category, a verbatim quote, and a safety flag. Social platforms (Reddit, TikTok, YouTube) are not crawled — Exa is blocked there, so they are left out rather than shown as thin. |
| 02 | Price History | Is it getting cheaper or pricier across the web? | 12 months of dated price observations from four deal/sale/price-drop query variants, news deal coverage, merchant listings, `/answer` (dated observations with source URLs) and tracker sites (camelcamelcamel, pricehistory.app, brickeconomy…), carried forward per merchant into a low/median/high band. Trend is only reported for merchants tracked ≥30 days (a median that "moves" because coverage grew is not a trend). |
| 03 | Listing Radar | Who sells this exact product right now, and where does Walmart stand? | Big-box `includeDomains` + a long-tail sweep + an exact-keyword pass, each listing classified exact / variant / accessory / refurbished by a schema summary; `maxAgeHours` refreshes stale listing pages. Opt-in **deep scan** runs an Exa Agent over the Affiliate.com catalog (Exa Connect) for live offers, including Walmart's own price. |
| 04 | Internal Dupes | Is Walmart selling this product against itself? | Exa's index of walmart.com titles (`includeDomains: walmart.com`), classified against the primary listing: exact sibling pages vs variants, refurbished and accessories. |
| 05 | Recall & Safety | Is anything actually unsafe? | `includeDomains: cpsc.gov` (model 24 months, brand 5 years, three queries) plus a safety-news lane (news category + recalls.gov / FDA / NHTSA / class-action sites, 12 months) classified recall / lawsuit / incident / regulatory. Model-level recall vs *brand-family spillover* (e.g. SharkNinja's Foodi pressure-cooker recall or Stanley's 2.6M-mug recall on a sibling product's report); safety-classified complaints from the mention graph are tracked separately from quality complaints. |

The verdict sentence, signal-board statuses (act now / watch / clear / thin data) and every trend are computed
deterministically from those labels — the model classifies pages, code decides.

`tools/bench.py` is the coverage benchmark that chose these strategies (relevant hits per search type / window / result count / domain set / `/answer`, on four products).

## Honesty rules (what the demo will *not* do)

- **walmart.com is never crawled** (it blocks crawlers, and it is excluded from sentiment by design). Walmart's own
  price is whatever the *open web* last reported, dated and linked, or comes from the Affiliate.com deep scan.
- **Social platforms are not scraped.** reddit.com is not in Exa's index and blocks direct requests, and TikTok /
  YouTube coverage is too thin to be honest, so the sentiment section is built from retail review pages, review
  sites, forums and news. Reddit appears only as verbatim quotes reproduced on third-party pages, labelled *quoted*.
- A page only counts as a mention if it names **this** model (or a full alias) — sibling models are excluded, and
  a safety claim must name the product in its title or quoted text, not a stray highlight.
- Thin data is shown as thin data. Nothing is extrapolated into a 90-day daily series that was not observed.

## Run it

```bash
pip install -r requirements.txt
echo "EXA_API_KEY=your-key" > .env
python3 app.py                # → http://localhost:8020
```

`presets.json` holds up to three demo presets (label + URL). Reports are cached per item id for 24 h
(`cache/<id>.json`); `?url=…&auto=cached` replays a cached report with zero API calls, `&auto=live` forces a run.
`?mock=1&view=report` renders `static/mock.json` without a backend.

Public-deployment spend guard: live reports are rate-limited per IP and globally (`LIVE_RUNS_PER_HOUR`,
`LIVE_RUNS_PER_IP_PER_HOUR`), deep scans separately (`DEEP_SCANS_PER_HOUR`); cached replays are free.

## Deploying (how pulse.salua.ai runs)

Route 53 `A pulse.salua.ai → EC2` (created with the AWS CLI), `deploy/product-pulse.service` (systemd, uvicorn on
127.0.0.1:8020), `deploy/nginx-pulse.salua.ai.conf` (reverse proxy, SSE buffering off) and
`certbot --nginx -d pulse.salua.ai`. Install commands are in each file's header.

## Layout

```
app.py              FastAPI backend: URL parsing, Exa client, 20-call pipeline, deterministic assembly, SSE, cache, deep scan
static/             vanilla-JS frontend (index.html, app.js, style.css) — the Claude Design screens, data-driven
static/mock.json    sample report for offline UI work
design/             the Claude Design export this implements
deploy/             systemd unit + nginx site
tools/              run_once.py (run the pipeline from the CLI), summarize.py (print a report's key numbers)
CONTRACT.md         backend ⇄ frontend data contract
```
