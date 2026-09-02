"""
Product Pulse — paste a walmart.com product URL, get one report built from the
open web via Exa: sentiment pulse, price history, listing radar, internal
dupes, recall & safety.

Exa endpoints used
  /answer   entity resolution (brand / model / UPC / aliases) with outputSchema
  /search   every surface: includeDomains / excludeDomains / category / date
            windows, highlights + schema summaries in the same call
  /agent    opt-in deep scan through Exa Connect (Affiliate.com catalog)

Run:  EXA_API_KEY=... python3 app.py   (or put the key in .env)  → http://localhost:8020
"""

import asyncio
import json
import math
import os
import re
import statistics
import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

ROOT = Path(__file__).parent
CACHE_DIR = ROOT / "cache"
CACHE_DIR.mkdir(exist_ok=True)


def _load_env():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


_load_env()
EXA_API_KEY = os.environ.get("EXA_API_KEY", "")
EXA_BASE = "https://api.exa.ai"
PORT = int(os.environ.get("PORT", "8020"))

MAX_CONCURRENCY = 8
MIN_CALL_SPACING = 0.12
WINDOW_DAYS = 365          # retrieval + price-series window
SPARK_DAYS = 90            # sentiment sparkline window

# public-deployment spend guard (a live report costs ~$0.30-0.45 in Exa credits)
LIVE_RUNS_PER_HOUR = int(os.environ.get("LIVE_RUNS_PER_HOUR", "24"))
LIVE_RUNS_PER_IP_PER_HOUR = int(os.environ.get("LIVE_RUNS_PER_IP_PER_HOUR", "6"))
MAX_CONCURRENT_RUNS = 3
DEEP_SCANS_PER_HOUR = int(os.environ.get("DEEP_SCANS_PER_HOUR", "6"))
CACHE_TTL_HOURS = float(os.environ.get("CACHE_TTL_HOURS", "24"))

_run_log: deque = deque()
_run_log_by_ip: dict = {}
_active_runs = 0
_deep_log: deque = deque()

DEFAULT_PRESETS = [
    {"label": "Ninja AF101 Air Fryer · 4 qt",
     "url": "https://www.walmart.com/ip/Ninja-AF101-Air-Fryer-that-Crisps-Roasts-Reheats-Dehydrates-for-Quick-Easy-Meals-4-Quart-Capacity-High-Gloss-Finish-Black-Grey/1967919184"},
]


def presets():
    p = ROOT / "presets.json"
    if p.exists():
        try:
            return json.loads(p.read_text())[:3]
        except Exception:
            pass
    return DEFAULT_PRESETS


# ---------------------------------------------------------------- helpers

def now_utc():
    return datetime.now(timezone.utc)


def iso_days_ago(n):
    return (now_utc() - timedelta(days=n)).strftime("%Y-%m-%dT00:00:00Z")


def parse_date(s):
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            d = datetime.strptime(s, fmt)
            return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
        except ValueError:
            continue
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=timezone.utc)
        except ValueError:
            return None
    # "Aug 22, 2026" / "August 22, 2026" / "Aug. 06, 2026"
    m = re.search(r"([A-Z][a-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", s)
    if m:
        for fmt in ("%b %d %Y", "%B %d %Y"):
            try:
                return datetime.strptime(f"{m[1][:3] if fmt=='%b %d %Y' else m[1]} {m[2]} {m[3]}", fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def host_of(url):
    try:
        h = urlparse(url if "://" in url else "https://" + url).netloc.lower()
    except Exception:
        return ""
    for p in ("www.", "m.", "shop.", "business.", "smile."):
        if h.startswith(p):
            h = h[len(p):]
    return HOST_ALIAS.get(h, h)


US_TLD = re.compile(r"\.(com|net|org|us|shop|store|co|io|biz|info|live|app|market|deals|online|site|xyz|tv|me)$", re.I)


def is_us_host(h):
    if not h or NON_US_TLD.search(h) or not US_TLD.search(h):
        return False
    sub = h.split(".")[0] if h.count(".") >= 2 else ""
    if re.search(r"(^|[.-])(ca|uk|au|de|fr|eu|intl|int|mx|br|nz|ie|in)([.-]|$)", sub) or re.search(r"staging|edit|test|dev|preview|sandbox|qa[0-9]*\b", sub):
        return False
    return True


def canon_url(u):
    m = re.match(r"^(https?://(?:www\.)?amazon\.com)/(?:.*/)?(?:dp|gp/product)/([A-Z0-9]{10})", u or "")
    if m:
        return f"{m.group(1)}/dp/{m.group(2)}"
    return (u or "").split("&tag=")[0]


def pretty_host(h):
    return re.sub(r"\.(com|net|org|us|shop|store|co)$", "", h)


def norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def clean_val(v):
    if isinstance(v, str) and v.strip().lower() in ("null", "none", "n/a", "", "unknown", "not available"):
        return None
    return v


def parse_summary(s):
    """Schema summaries come back as a JSON string. Be tolerant."""
    if not s:
        return {}
    if isinstance(s, dict):
        d = s
    else:
        try:
            d = json.loads(s)
        except Exception:
            m = re.search(r"\{.*\}", str(s), re.S)
            if not m:
                return {}
            try:
                d = json.loads(m.group(0))
            except Exception:
                return {}
    if not isinstance(d, dict):
        return {}
    return {k: clean_val(v) for k, v in d.items()}


def to_float(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if 0 < float(v) < 100000 else None
    m = re.search(r"\d[\d,]*\.?\d*", str(v))
    if not m:
        return None
    try:
        f = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    return f if 0 < f < 100000 else None


def to_int(v):
    f = to_float(v)
    return int(f) if f is not None else None


STOP = set("the a an and or of for with to in on at by from is are this that it its & - – — , . qt quart quarts oz ounce inch in ft lb lbs pack count ct pc pcs set new".split())


def tokens(s):
    return [t for t in re.findall(r"[a-z0-9][a-z0-9.+'-]*", (s or "").lower()) if t not in STOP and len(t) > 1]


def contains_word(text, word):
    if not word:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(word.lower()) + r"(?![a-z0-9])", (text or "").lower()) is not None


# ---------------------------------------------------------------- walmart URL parsing

WM_RE = re.compile(
    r"^(?:https?://)?(?:www\.|business\.|m\.)?walmart\.com/(?:ip/(?:[^?#]*?/)?(\d{4,})|reviews/product/(\d{4,}))(?:[/?#].*)?$",
    re.I,
)


def parse_walmart(url):
    url = norm(url)
    m = WM_RE.match(url)
    if not m:
        return None
    item_id = m.group(1) or m.group(2)
    slug = ""
    if m.group(1):
        path = re.sub(r"^(?:https?://)?[^/]+/ip/", "", url, flags=re.I)
        path = path.split("?")[0].split("#")[0]
        segs = [s for s in path.split("/") if s]
        segs = [s for s in segs if s != item_id and not re.fullmatch(r"\d+", s) and s.lower() not in ("seort", "seo")]
        slug = " ".join(segs)
    slug_words = norm(re.sub(r"[-_]+", " ", slug))
    slug_words = re.sub(r"\s+\d{6,}$", "", slug_words)
    canonical = f"https://www.walmart.com/ip/{slug}/{item_id}" if slug else f"https://www.walmart.com/ip/{item_id}"
    return {"id": item_id, "slug_words": slug_words, "url": canonical}


# ---------------------------------------------------------------- exa client

async def _no_emit(e):
    return None


# one process-wide limiter: concurrent reports share the 10 rps key budget
_SEM = None
_SPACE_LOCK = None
_LAST_START = [0.0]


class Exa:
    def __init__(self, emit=None):
        global _SEM, _SPACE_LOCK
        self.emit = emit or _no_emit
        if _SEM is None:
            _SEM, _SPACE_LOCK = asyncio.Semaphore(MAX_CONCURRENCY), asyncio.Lock()
        self.sem = _SEM
        self.lock = _SPACE_LOCK
        self.calls = []
        self.raw = []
        self.cost = 0.0
        self.no_credits = False
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))

    async def close(self):
        await self.client.aclose()

    async def _space(self):
        async with self.lock:
            wait = _LAST_START[0] + MIN_CALL_SPACING - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            _LAST_START[0] = time.monotonic()

    async def call(self, path, body, tag, timeout=45.0):
        async with self.sem:
            for attempt in range(5):
                await self._space()
                t0 = time.monotonic()
                try:
                    r = await self.client.post(EXA_BASE + path, json=body, timeout=timeout,
                                               headers={"x-api-key": EXA_API_KEY, "Content-Type": "application/json"})
                except (httpx.TimeoutException, httpx.TransportError) as e:
                    ms = int((time.monotonic() - t0) * 1000)
                    self.calls.append({"endpoint": path, "tag": tag, "ms": ms, "cost": 0, "error": type(e).__name__})
                    if attempt == 4:
                        await self.emit({"type": "call", "endpoint": path, "tag": tag, "ms": ms, "cost": 0, "error": type(e).__name__})
                        return {}
                    await asyncio.sleep(1.0 + attempt)
                    continue
                ms = int((time.monotonic() - t0) * 1000)
                if r.status_code == 429 or r.status_code >= 500:
                    self.calls.append({"endpoint": path, "tag": tag, "ms": ms, "cost": 0, "error": r.status_code})
                    if attempt == 4:
                        await self.emit({"type": "call", "endpoint": path, "tag": tag, "ms": ms, "cost": 0, "error": r.status_code})
                        return {}
                    await asyncio.sleep((2.0 if r.status_code == 429 else 1.0) * (2 ** attempt))
                    continue
                try:
                    d = r.json()
                except Exception:
                    d = {}
                if r.status_code == 402:
                    self.no_credits = True
                if r.status_code != 200:
                    self.calls.append({"endpoint": path, "tag": tag, "ms": ms, "cost": 0, "error": r.status_code, "detail": str(d)[:200]})
                    await self.emit({"type": "call", "endpoint": path, "tag": tag, "ms": ms, "cost": 0, "error": r.status_code})
                    return {}
                cost = 0.0
                cd = d.get("costDollars")
                if isinstance(cd, dict):
                    cost = float(cd.get("total") or 0)
                self.cost += cost
                self.calls.append({"endpoint": path, "tag": tag, "ms": ms, "cost": cost, "n": len(d.get("results", []) or [])})
                if os.environ.get("PULSE_DEBUG"):
                    self.raw.append({"tag": tag, "path": path, "body": body, "response": d})
                await self.emit({"type": "call", "endpoint": path, "tag": tag, "ms": ms, "cost": round(cost, 4)})
                return d
        return {}

    async def search(self, tag, query, **kw):
        body = {"query": query, "type": kw.pop("type", "auto"), "numResults": kw.pop("numResults", 10)}
        timeout = kw.pop("timeout", 45.0)
        for k, v in kw.items():
            if v is not None:
                body[k] = v
        d = await self.call("/search", body, tag, timeout=timeout)
        return d.get("results", []) or []

    async def answer(self, tag, query, schema=None):
        body = {"query": query}
        if schema:
            body["outputSchema"] = schema
        d = await self.call("/answer", body, tag)
        return d


# ---------------------------------------------------------------- schemas

RESOLVE_SCHEMA = {
    "type": "object",
    "properties": {
        "brand": {"type": "string"},
        "model": {"type": "string", "description": "manufacturer model / part number as printed on the box (e.g. AF101, 75440, MTJV3). Empty string if the product has none (groceries, private label)."},
        "name": {"type": "string", "description": "canonical product name as the manufacturer or major retailers list it"},
        "upc": {"type": "string", "description": "12-14 digit UPC/GTIN if findable, else empty"},
        "category": {"type": "string", "description": "2-4 word product type, e.g. 'air fryer', 'LEGO building set', 'wireless earbuds'"},
        "size": {"type": "string", "description": "the defining size/capacity/count variant if any, e.g. '4 qt', '40 oz', '55 inch'"},
        "aliases": {"type": "array", "items": {"type": "string"}, "description": "3 short names people use for it online, e.g. brand+model, brand+type+size, nickname"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["brand", "name", "category", "aliases", "confidence"],
}

SENT_SCHEMA = {
    "type": "object",
    "properties": {
        "mentions_product": {"type": "boolean", "description": "true if this exact product is discussed (not just the brand or a different model)"},
        "source_kind": {"type": "string", "enum": ["owner_review", "expert_review", "forum_thread", "news", "deal_post", "retailer_listing", "video", "other"]},
        "sentiment": {"type": "string", "enum": ["positive", "negative", "mixed", "neutral"], "description": "overall opinion voiced about THIS product; neutral if no opinion is voiced"},
        "complaint_voiced": {"type": "boolean"},
        "pain_category": {"type": "string", "enum": ["noise", "durability", "size_fit", "cleaning", "performance", "smell_fumes", "safety", "price_value", "controls_usability", "shipping_packaging", "battery_power", "comfort", "quality_materials", "compatibility", "none"]},
        "pain_label": {"type": "string", "description": "3-6 word lowercase label for the voiced complaint, empty if none"},
        "praise_label": {"type": "string", "description": "3-6 word lowercase label for the main voiced praise, empty if none"},
        "quote": {"type": "string", "description": "verbatim quote (<=160 chars) of the strongest voiced opinion, empty if none"},
        "safety_issue": {"type": "boolean", "description": "true only if a fire, smoke, burn, shock, choking, injury or toxic-fumes issue is described for this product"},
    },
    "required": ["mentions_product", "source_kind", "sentiment", "complaint_voiced", "pain_category", "safety_issue"],
}

RETAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "exact_product": {"type": "boolean"},
        "rating": {"type": "number"},
        "review_count": {"type": "integer"},
        "complaints": {"type": "array", "items": {"type": "string"}, "description": "up to 4 complaints customers actually voice in reviews or 'customers say' sections; empty if none present in the text"},
        "praises": {"type": "array", "items": {"type": "string"}, "description": "up to 4 praises customers actually voice; empty if none present"},
        "quote": {"type": "string", "description": "one verbatim customer sentence (<=160 chars), empty if none"},
    },
    "required": ["exact_product", "complaints", "praises"],
}

RECALL_SCHEMA = {
    "type": "object",
    "properties": {
        "is_recall_notice": {"type": "boolean"},
        "product": {"type": "string"},
        "brand": {"type": "string"},
        "models": {"type": "string", "description": "model numbers named in the notice"},
        "date": {"type": "string"},
        "hazard": {"type": "string"},
        "units": {"type": "string"},
        "applies_to_model": {"type": "boolean", "description": "true only if the target model number is explicitly named"},
        "same_brand_family": {"type": "boolean"},
    },
    "required": ["is_recall_notice", "product", "applies_to_model", "same_brand_family"],
}

SAFETY_SCHEMA = {
    "type": "object",
    "properties": {
        "is_safety_related": {"type": "boolean", "description": "true only if the page reports a recall, lawsuit, injury, fire, burn, shock, choking, contamination, regulatory action or other safety issue"},
        "kind": {"type": "string", "enum": ["recall", "lawsuit", "incident_report", "regulatory", "investigation", "none"]},
        "product": {"type": "string", "description": "the product named"},
        "issue": {"type": "string", "description": "one short sentence: what the safety issue is"},
        "date": {"type": "string"},
        "applies_to_model": {"type": "boolean", "description": "true only if the target model is explicitly named"},
        "same_brand": {"type": "boolean"},
    },
    "required": ["is_safety_related", "kind", "product", "applies_to_model", "same_brand"],
}

LISTING_SCHEMA = {
    "type": "object",
    "properties": {
        "listing_type": {"type": "string", "enum": ["exact", "variant", "accessory", "bundle", "refurbished_or_used", "not_a_listing"]},
        "merchant": {"type": "string", "description": "the store/site selling it"},
        "seller": {"type": "string", "description": "third-party marketplace seller name if shown, else empty"},
        "price_usd": {"type": "number", "description": "current price in USD as shown, if shown"},
        "in_stock": {"type": "boolean"},
        "condition": {"type": "string", "enum": ["new", "used", "refurbished", "unknown"]},
    },
    "required": ["listing_type", "merchant", "condition"],
}

PRICE_EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "exact_product": {"type": "boolean"},
        "merchant": {"type": "string"},
        "price_usd": {"type": "number"},
        "previous_price_usd": {"type": "number"},
        "observed_date": {"type": "string", "description": "date the price was observed/published, ISO if possible"},
        "event": {"type": "string", "enum": ["price_drop", "sale", "new_low", "price_increase", "out_of_stock", "restock", "listing", "none"]},
        "condition": {"type": "string", "enum": ["new", "used", "refurbished", "unknown"]},
    },
    "required": ["exact_product", "event"],
}

CAMEL_SCHEMA = {
    "type": "object",
    "properties": {
        "exact_product": {"type": "boolean"},
        "current": {"type": "number"},
        "lowest": {"type": "number"},
        "highest": {"type": "number"},
        "average": {"type": "number"},
        "notes": {"type": "string"},
    },
    "required": ["exact_product"],
}

BIG_BOX = {
    "amazon.com": "Amazon", "target.com": "Target", "bestbuy.com": "Best Buy", "newegg.com": "Newegg",
    "homedepot.com": "Home Depot", "lowes.com": "Lowe's", "kohls.com": "Kohl's", "macys.com": "Macy's",
    "jcpenney.com": "JCPenney", "bhphotovideo.com": "B&H", "wayfair.com": "Wayfair", "staples.com": "Staples",
    "officedepot.com": "Office Depot", "samsclub.com": "Sam's Club", "costco.com": "Costco", "bjs.com": "BJ's",
    "chewy.com": "Chewy", "petco.com": "Petco", "petsmart.com": "PetSmart", "gamestop.com": "GameStop",
    "sephora.com": "Sephora", "ulta.com": "Ulta", "cvs.com": "CVS", "walgreens.com": "Walgreens",
    "kroger.com": "Kroger", "acehardware.com": "Ace Hardware", "overstock.com": "Overstock", "qvc.com": "QVC",
    "hsn.com": "HSN", "lego.com": "LEGO", "apple.com": "Apple", "dell.com": "Dell", "bedbathandbeyond.com": "Bed Bath & Beyond",
    "williams-sonoma.com": "Williams Sonoma", "crateandbarrel.com": "Crate & Barrel", "dickssportinggoods.com": "Dick's",
    "academy.com": "Academy", "tractorsupply.com": "Tractor Supply", "menards.com": "Menards", "microcenter.com": "Micro Center",
    "adorama.com": "Adorama", "zappos.com": "Zappos", "nordstrom.com": "Nordstrom", "gnc.com": "GNC", "vitacost.com": "Vitacost",
    "iherb.com": "iHerb", "instacart.com": "Instacart", "shipt.com": "Shipt", "meijer.com": "Meijer", "hy-vee.com": "Hy-Vee",
}
RETAIL_REVIEW_DOMAINS = ["amazon.com", "target.com", "bestbuy.com", "homedepot.com", "lowes.com", "chewy.com", "bhphotovideo.com", "wayfair.com"]
HOST_ALIAS = {"a.co": "amazon.com", "amzn.to": "amazon.com", "amzn.com": "amazon.com", "smile.amazon.com": "amazon.com"}
NON_US_TLD = re.compile(r"\.(ca|co\.uk|uk|ie|com\.au|au|de|fr|es|it|nl|se|no|dk|fi|pl|cz|in|jp|cn|hk|sg|my|ph|nz|mx|br|ar|cl|co|za|ae|sa|tr|ru|kr|tw|th|id|vn|pk|ng|ke|eg|il|gr|pt|be|ch|at|hu|ro|bg|ua|lt|lv|ee|sk|si|hr|rs|eu)$", re.I)
NON_MERCHANT = {"walmart.com", "youtube.com", "tiktok.com", "reddit.com", "ebay.com", "cpsc.gov", "camelcamelcamel.com",
                "manualslib.com", "manualslib.tech", "facebook.com", "instagram.com", "pinterest.com", "x.com", "twitter.com",
                "wikipedia.org", "google.com", "bing.com", "amazon.ca", "amazon.co.uk", "aliexpress.com", "temu.com", "wish.com",
                "poshmark.com", "mercari.com", "offerup.com", "craigslist.org", "shopgoodwill.com", "upcitemdb.com", "upczilla.com",
                "barcodelookup.com", "go-upc.com", "pricepulse.app", "keepa.com", "honey.com", "slickdeals.net", "dealnews.com",
                "brickseek.com", "redditrecs.com", "quora.com", "medium.com", "bricksleuth.com", "brickfact.com", "brickset.com", "bricklink.com",
                "brickeconomy.com", "pricecharting.com", "pricespy.com", "pricerunner.com", "shopsavvy.com", "klarna.com", "google.com", "shopping.google.com",
                "pricegrabber.com", "shopzilla.com", "bizrate.com", "nextag.com", "camelcamelcamel.com", "pepperdeals.com", "dealcatcher.com", "pzdeals.com",
                "woot.com", "home.woot.com", "ubuy.com", "desertcart.com", "fruugo.com", "bonanza.com", "onbuy.com", "johnlewis.com", "ao.com",
                "currys.com", "argos.com", "fnac.com", "darty.com", "bol.com", "mediamarkt.com", "jbhifi.com", "harveynorman.com", "kogan.com",
                "catch.com", "noon.com", "jumia.com", "flipkart.com", "shopee.com", "lazada.com", "ozon.com", "mercadolibre.com", "kiut.com",
                "dhgate.com", "banggood.com", "gearbest.com", "lightinthebox.com", "joom.com", "cdiscount.com", "otto.com", "zalando.com", "bricksworld.com"}
SENTIMENT_EXCLUDE = sorted(set(list(BIG_BOX.keys()) + ["walmart.com", "youtube.com", "tiktok.com", "reddit.com", "cpsc.gov", "ebay.com",
                                                      "manualslib.com", "manualslib.tech", "camelcamelcamel.com", "upcitemdb.com", "upczilla.com",
                                                      "aliexpress.com", "temu.com", "pinterest.com", "facebook.com", "instagram.com"]))

FORUM_DOMAINS = ["quora.com", "forums.redflagdeals.com", "slickdeals.net", "hardforum.com", "avsforum.com", "houzz.com", "food52.com", "macrumors.com",
                 "head-fi.org", "dpreview.com", "cooking.stackexchange.com", "lemmy.world", "news.ycombinator.com", "community.bestbuy.com",
                 "community.homedepot.com", "forums.anandtech.com", "eurobricks.com", "brickset.com", "flyertalk.com", "garagejournal.com", "chefsteps.com",
                 "egullet.org", "resetera.com", "neogaf.com", "askmetafilter.com", "metafilter.com", "thekitchn.com", "seriouseats.com", "chowhound.com",
                 "bogleheads.org", "stackexchange.com", "candlepowerforums.com", "rcgroups.com", "dogforums.com", "dogforum.com", "thedogforum.com"]
TRACKER_DOMAINS = ["camelcamelcamel.com", "pricepulse.app", "keepa.com", "pricespy.com", "shopsavvy.com", "brickeconomy.com", "brickset.com", "price.com",
                   "pricegrabber.com", "honey.com", "pricehistory.app", "priceintime.com", "klarna.com"]
SAFETY_DOMAINS = ["saferproducts.gov", "recalls.gov", "fda.gov", "nhtsa.gov", "usda.gov", "consumerreports.org", "classaction.org", "topclassactions.com"]

MERCHANT_COLORS = ["oklch(74% 0.12 250)", "oklch(74% 0.12 300)", "oklch(74% 0.12 215)", "oklch(74% 0.12 275)",
                   "oklch(74% 0.12 330)", "oklch(74% 0.13 190)", "oklch(74% 0.12 160)", "oklch(74% 0.12 40)",
                   "oklch(74% 0.12 90)", "oklch(74% 0.12 20)", "oklch(74% 0.10 120)", "oklch(74% 0.10 350)"]

PAIN_WORDS = {
    "noise": r"\bloud|\bnois|\bhum\b|rattl|buzz|beep",
    "durability": r"broke|break|stopp?ed working|died|dead|fail|peel|flak|wear|crack|rust|chip|fell apart|defect|malfunction|warranty|last(ed)? only",
    "size_fit": r"\bsmall|too big|bulky|capacity|\bsize|\bfits?\b|\bfitting\b|tight|heavy|\blarge|\bspace",
    "cleaning": r"clean|dishwasher|stain|residue|sticky|greas",
    "performance": r"uneven|undercook|overcook|slow|weak|doesn.t (heat|cook|work)|inconsistent|burn(t|ed)? food|soggy|not crispy|lag|glitch|poor (quality|performance)|drop(s|ped)? connection",
    "smell_fumes": r"smell|odor|odour|fume|plastic smell|chemical",
    "safety": r"fire|smoke|spark|shock|melt|burn hazard|caught fire|injur|choking|toxic|overheat",
    "price_value": r"price|expensive|overpriced|value|cost|cheap(ly)? made|not worth",
    "controls_usability": r"button|control|timer|display|panel|setting|confus|hard to (use|read)|touchscreen|app|remote|instructions|manual",
    "shipping_packaging": r"shipping|packag|arrived|damaged|box|missing part|late|delivery",
    "battery_power": r"battery|charge|charging|power|cord|plug|outlet",
    "comfort": r"comfort|hurt|\bears? (hurt|ache|pain|fatigue)|painful|itch|irritat",
    "quality_materials": r"flimsy|thin|plastic|cheap|quality|material|coating|paint",
    "compatibility": r"compatib|doesn.t (fit|connect|pair|sync)|not compatible|bluetooth",
}
PAIN_DISPLAY = {"noise": "noise", "durability": "durability", "size_fit": "size & fit", "cleaning": "cleaning", "performance": "performance",
                "smell_fumes": "smell / fumes", "safety": "safety", "price_value": "price & value", "controls_usability": "controls & usability",
                "shipping_packaging": "shipping & packaging", "battery_power": "battery & power", "comfort": "comfort",
                "quality_materials": "materials & build", "compatibility": "compatibility"}
PRAISE_WORDS = [
    ("easy to clean", r"clean|dishwasher"),
    ("cooks fast, crispy results", r"crisp|fast|quick|even|cook|result|delicious|tast"),
    ("easy to use", r"easy|simple|intuitive|convenient|user.friendly|setup"),
    ("good value", r"value|price|afford|cheap|worth|budget"),
    ("compact size", r"compact|small|size|space|fits|fit on"),
    ("build quality", r"quality|sturdy|solid|durable|well.made|premium"),
    ("sound / performance", r"sound|audio|bass|performance|powerful|battery|noise cancel"),
    ("looks & design", r"look|design|style|sleek|color|aesthetic"),
    ("quiet", r"quiet|silent"),
    ("comfortable", r"comfort"),
]


def pain_category_of(text):
    t = (text or "").lower()
    for cat, pat in PAIN_WORDS.items():
        if re.search(pat, t):
            return cat
    return None


def praise_bucket(text):
    t = (text or "").lower()
    for label, pat in PRAISE_WORDS:
        if re.search(pat, t):
            return label
    return None


# ---------------------------------------------------------------- pipeline

class Pulse:
    def __init__(self, parsed, emit):
        self.p = parsed
        self.id = parsed["id"]
        self.emit_raw = emit
        self.exa = Exa(emit=self._emit_call)
        self.as_of = now_utc()
        self.start = self.as_of - timedelta(days=WINDOW_DAYS - 1)
        self.surfaces = {}
        self.mentions = []
        self.observations = []
        self.listings_raw = []
        self.retail = []
        self.recalls = []
        self.safety_news = []
        self.dupes_raw = []
        self.camel = None
        self.product = {}

    async def _emit_call(self, e):
        await self.emit_raw("call", {k: v for k, v in e.items() if k != "type"})

    async def surface(self, key, status, n=0, note=""):
        label = {"reddit": "reddit", "youtube": "youtube", "tiktok": "tiktok", "news": "news", "forums": "forums & blogs",
                 "retail": "retail reviews", "cpsc": "cpsc"}.get(key, key)
        self.surfaces[key] = {"key": key, "label": label, "status": status, "n": n, "note": note}
        await self.emit_raw("surface", self.surfaces[key])

    # ---- 1. entity resolution
    async def resolve(self):
        p = self.p
        slug = p["slug_words"]
        q = (f"Identify the exact product sold at {p['url']} (Walmart item ID {p['id']}"
             + (f', listing title from the URL: "{slug}"' if slug else "") + "). "
             "Return brand, manufacturer model number, canonical product name, UPC/GTIN, a short product category, the defining "
             "size/capacity/count variant, and 3 short aliases people use for it online.")
        tasks = [self.exa.answer("resolve", q, RESOLVE_SCHEMA),
                 self.exa.search("resolve:walmart-title", f"walmart.com/ip/{p['id']}", type="keyword", numResults=5, includeDomains=["walmart.com"])]
        if slug:
            tasks.append(self.exa.search("resolve:walmart-title", slug, numResults=8, includeDomains=["walmart.com"]))
        res = await asyncio.gather(*tasks, return_exceptions=True)
        ans = res[0] if isinstance(res[0], dict) else {}
        a = ans.get("answer") if isinstance(ans.get("answer"), dict) else {}
        a = {k: clean_val(v) for k, v in (a or {}).items()}
        wm_title = ""
        wm_hits = []
        for r in res[1:]:
            if isinstance(r, list):
                wm_hits.extend(r)
        for r in wm_hits:
            u = r.get("url", "")
            if re.search(rf"/(?:ip|product)/(?:[^/?#]*/)?{p['id']}(?:[/?#]|$)", u):
                t = norm(r.get("title"))
                t = re.sub(r"^customer reviews for\s+", "", t, flags=re.I)
                t = re.sub(r"\s*[-|–]\s*walmart(\.com)?\s*$", "", t, flags=re.I)
                if t and (not wm_title or "/reviews/" not in u):
                    wm_title = t
        brand = norm(a.get("brand") or "")
        model = norm(a.get("model") or "")
        if model and (len(model) < 2 or len(model) > 24 or model.lower() in ("n/a", "none", "unknown")):
            model = ""
        name = wm_title or (slug.title() if slug else "") or norm(a.get("name") or "")
        if not name:
            name = f"Walmart item {p['id']}"
        if not brand:
            brand = (tokens(name) or [""])[0].title()
        # keep the model only if it shows up somewhere credible (name, slug, answer name)
        credible = " ".join([name, slug, norm(a.get("name") or "")]).lower()
        if model and not contains_word(credible, model) and not re.search(r"\d", model):
            model = ""
        category = norm(a.get("category") or "").lower() or " ".join(tokens(name)[-2:])
        SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s?-?\s?(qt|quart|quarts|oz|ounce|lb|lbs|inch|inches|in\b|\"|ft|gallon|gal|ml|l\b|liter|gb|tb|ct|count|pack|pk|piece|pieces|pcs|mm|cm|watt|w\b|mah|hz|mp|k\b)", re.I)
        size = norm(a.get("size") or "")
        m = SIZE_RE.search(size) or SIZE_RE.search(name) or SIZE_RE.search(slug)
        size = f"{m.group(1)} {m.group(2).lower().rstrip('.')}" if m else ""
        upc = re.sub(r"\D", "", str(a.get("upc") or ""))
        upc = upc if 11 <= len(upc) <= 14 else None
        aliases = [norm(x) for x in (a.get("aliases") or []) if isinstance(x, str) and norm(x)]
        base_alias = f"{brand} {model}".strip() if model else name.split(",")[0][:48]
        if base_alias and base_alias.lower() not in [x.lower() for x in aliases]:
            aliases.insert(0, base_alias)
        aliases = aliases[:3]
        short = f"{brand} / {model}" if model else name.split(",")[0][:40]
        if size:
            short += f" ({size})"
        self.product = {"name": name, "brand": brand, "model": model, "upc": upc, "category": category, "size": size,
                        "short": short, "aliases": [{"text": x, "support": None} for x in aliases],
                        "confidence": a.get("confidence") or "low"}
        # which term do people actually use for this product? A model that appears in the Walmart listing
        # itself (AF101, 75440) is the best key; a part number that never appears there (Apple's MTJV3) is not.
        listing_text = f"{name} {slug} {wm_title}".lower()
        model_in_listing = bool(model) and (contains_word(listing_text, model) or re.sub(r"[\s-]", "", model.lower()) in re.sub(r"[\s-]", "", listing_text))
        answer_aliases = [x for x in (a.get("aliases") or []) if isinstance(x, str) and norm(x)]
        if model_in_listing:
            self.q = f"{brand} {model}".strip()
        else:
            cand = [x for x in answer_aliases if not (model and contains_word(x, model))]
            cand = [x for x in cand if 2 <= len(tokens(x)) <= 6]
            if cand:
                self.q = norm(cand[0])
                if brand and not contains_word(self.q, brand) and len(tokens(self.q)) <= 4:
                    self.q = f"{brand} {self.q}"
            else:
                core = [t for t in re.split(r"[,|(]", name)[0].split() if t.lower() not in ("with", "and", "&", "the", "for")][:6]
                self.q = " ".join(core)
            if self.q.lower() not in [x["text"].lower() for x in self.product["aliases"]]:
                self.product["aliases"].insert(0, {"text": self.q, "support": None})
                self.product["aliases"] = self.product["aliases"][:3]
        self.model_in_listing = model_in_listing
        self.q_full = self.q if (category and all(contains_word(self.q, t) for t in tokens(category))) else f"{self.q} {category}".strip()
        size_in_q = bool(size) and re.sub(r"[\s-]", "", size.lower()) in re.sub(r"[\s-]", "", self.q.lower())
        self.product["short"] = short if model_in_listing or not model else (self.q if (size == "" or size_in_q) else f"{self.q} ({size})")
        self.product["label"] = model if model_in_listing else self.q
        self.cat_tokens = [t for t in tokens(category) if t not in tokens(brand)]
        self.name_tokens = [t for t in tokens(name) if t not in tokens(brand)][:8]

    def model_hit(self, text):
        m = self.product["model"]
        if not m:
            return False
        if contains_word(text, m):
            return True
        if re.search(r"[a-z]", m, re.I) and re.search(r"\d", m):
            # "AF-101" vs "AF101" vs "af 101"
            flat = re.sub(r"[\s-]", "", text.lower())
            return re.search(r"(?<![a-z0-9])" + re.escape(re.sub(r"[\s-]", "", m.lower())) + r"(?![a-z0-9])", flat) is not None
        return False

    def alias_hit(self, text):
        tl = (text or "").lower()
        for al in self.product.get("aliases", []):
            toks = tokens(al["text"])
            if len(toks) >= 2 and all(contains_word(tl, k) for k in toks):
                return True
        return False

    MODEL_TOKEN = re.compile(r"(?<![a-z0-9])([a-z]{1,4})-?(\d{2,6})([a-z]{0,4})(?![a-z0-9])", re.I)

    def other_model(self, text):
        """Does the text name a sibling model (same alpha prefix, different number) while not naming ours?"""
        m = self.product["model"]
        if not m or self.model_hit(text):
            return False
        mm = re.match(r"^([A-Za-z]{0,4})-?(\d{2,6})([A-Za-z]{0,4})$", m.replace(" ", ""))
        if not mm:
            return False
        prefix = mm.group(1).lower()
        for t in self.MODEL_TOKEN.finditer(text or ""):
            if prefix and t.group(1).lower() == prefix:
                return True
        if not prefix:
            # pure-number models (LEGO 75440): any other 4-6 digit set number in a title is a sibling
            for t in re.finditer(r"(?<![0-9a-z])(\d{4,6})(?![0-9a-z])", (text or "").lower()):
                if t.group(1) != mm.group(2) and abs(len(t.group(1)) - len(mm.group(2))) == 0:
                    return True
        return False

    def relevant(self, *texts, strict=True):
        """Does this text talk about THIS product? With a model number we insist on the model
        (or a full alias) — brand + category alone would pull in sibling models."""
        text = " ".join(str(t or "") for t in texts)
        m = self.product["model"]
        b = self.product["brand"]
        if m:
            if self.model_hit(text):
                return True
            if self.alias_hit(text) and not self.other_model(text):
                return True
            return False
        if self.alias_hit(text):
            return True
        if b and (contains_word(text, b) or (len(b) >= 5 and b.lower() in text.lower())):
            hits = sum(1 for t in self.cat_tokens if contains_word(text, t))
            nh = sum(1 for t in self.name_tokens if contains_word(text, t))
            if hits >= max(1, min(2, len(self.cat_tokens))) and nh >= max(2, int(len(self.name_tokens) * 0.5)):
                return True
            if not strict and hits >= 1:
                return True
        return False

    def alias_support(self, listing_titles):
        if not listing_titles:
            return
        for al in self.product["aliases"]:
            toks = tokens(al["text"])
            if not toks:
                al["support"] = None
                continue
            hit = 0
            for t in listing_titles:
                tl = t.lower()
                if all(contains_word(tl, k) for k in toks):
                    hit += 1
            al["support"] = round(hit / len(listing_titles), 2)

    # ---- 2. surfaces
    def _mention(self, r, source, s=None):
        s = s or parse_summary(r.get("summary"))
        d = parse_date(r.get("publishedDate"))
        hl = " ".join(r.get("highlights") or [])[:400]
        pc = s.get("pain_category") if s.get("pain_category") in PAIN_WORDS else None
        cv = bool(s.get("complaint_voiced"))
        if cv and not pc:
            pc = pain_category_of((s.get("pain_label") or "") + " " + (s.get("quote") or ""))
        return {
            "url": canon_url(r.get("url", "")), "title": norm(r.get("title"))[:160], "date": d.isoformat()[:10] if d else None,
            "_dt": d, "source": source, "host": host_of(r.get("url", "")),
            "sentiment": s.get("sentiment") if s.get("sentiment") in ("positive", "negative", "mixed", "neutral") else "neutral",
            "complaint_voiced": cv and pc is not None, "pain_category": pc if cv else None,
            "pain_label": norm(s.get("pain_label") or "").lower().replace("_", " ")[:60] or None,
            "praise_label": norm(s.get("praise_label") or "").lower().replace("_", " ")[:60] or None,
            "quote": norm(s.get("quote") or "")[:200] or None,
            "safety_issue": bool(s.get("safety_issue")),
            "source_kind": s.get("source_kind") or "other",
            "mentions_product": s.get("mentions_product"),
            "highlight": hl,
        }

    def _sent_contents(self):
        return {
            "highlights": {"maxCharacters": 240, "numSentences": 2, "query": f"opinion or complaint about the {self.q_full}"},
            "summary": {
                "query": (f"Read this page about the {self.q_full} ({self.q}). Report ONLY opinions that a customer, reviewer or commenter "
                          f"actually voices about THIS product — not other models, not marketing copy, not spec descriptions. "
                          f"If nobody voices a complaint, set complaint_voiced=false and pain_category='none'."),
                "schema": SENT_SCHEMA,
            },
        }

    ACCESSORY_RE = re.compile(r"replacement|\bliners?\b|accessor|compatible with|\bparts?\b|\bfilters? for|\bcover for|\bcase for|\bfits\b|parchment|silicone", re.I)

    def _keep_mentions(self, results, source):
        out = []
        for r in results:
            s = parse_summary(r.get("summary"))
            if self.ACCESSORY_RE.search(r.get("title", "") or "") and not self.ACCESSORY_RE.search(self.product["name"]):
                continue
            title = r.get("title", "") or ""
            blob = " ".join([title, " ".join(r.get("highlights") or []), r.get("url", ""), str(s.get("quote") or ""), str(s.get("pain_label") or ""), str(s.get("praise_label") or "")])
            if self.other_model(title + " " + r.get("url", "")):
                continue   # a page titled for a sibling model
            if self.product["model"]:
                if not (self.model_hit(blob) or self.alias_hit(blob)):
                    continue
            elif not self.relevant(blob):
                continue
            m = self._mention(r, source, s)
            # a safety claim must name this product in the title or the quoted text itself, not a stray highlight
            if m["safety_issue"] and not (self.model_hit(title + " " + (m["quote"] or "")) or self.alias_hit(title + " " + (m["quote"] or "")) or (not self.product["model"] and self.relevant(title + " " + (m["quote"] or "")))):
                m["safety_issue"] = False
                if m["pain_category"] == "safety":
                    m["complaint_voiced"], m["pain_category"] = False, None
            out.append(m)
        return out

    async def scan_youtube(self):
        await self.surface("youtube", "scanning")
        r1, r2 = await asyncio.gather(
            self.exa.search("youtube:neural", f"{self.q_full} review", numResults=30, includeDomains=["youtube.com"],
                            startPublishedDate=iso_days_ago(WINDOW_DAYS), contents=self._sent_contents()),
            self.exa.search("youtube:keyword", f"{self.q}", type="keyword", numResults=30, includeDomains=["youtube.com"],
                            contents=self._sent_contents()),
        )
        seen = set()
        res = [r for r in list(r1) + list(r2) if not (r.get("url") in seen or seen.add(r.get("url")))]
        ms = self._keep_mentions(res, "youtube")
        self.mentions.extend(ms)
        await self.surface("youtube", "done" if len(ms) >= 3 else "thin", len(ms), "" if len(ms) >= 3 else "few dated videos name this model")

    async def scan_tiktok(self):
        await self.surface("tiktok", "scanning")
        res = await self.exa.search("tiktok", f"{self.q}", numResults=20, includeDomains=["tiktok.com"], contents=self._sent_contents())
        ms = self._keep_mentions(res, "tiktok")
        self.mentions.extend(ms)
        await self.surface("tiktok", "done" if len(ms) >= 3 else "thin", len(ms), "" if len(ms) >= 3 else "tiktok pages are rarely dated or indexed")

    async def scan_news(self):
        await self.surface("news", "scanning")
        res = await self.exa.search("news", f"{self.q_full}", numResults=30, category="news",
                                    startPublishedDate=iso_days_ago(WINDOW_DAYS), contents=self._sent_contents())
        ms = self._keep_mentions(res, "news")
        self.mentions.extend(ms)
        await self.surface("news", "done" if len(ms) >= 2 else "thin", len(ms), "" if len(ms) >= 2 else "no dated news coverage names this model")

    async def scan_forums(self):
        await self.surface("forums", "scanning")
        r1, r2, r3, r4 = await asyncio.gather(
            self.exa.search("forums:experience", f"{self.q_full} owner experience review after using it", numResults=30,
                            excludeDomains=SENTIMENT_EXCLUDE, startPublishedDate=iso_days_ago(WINDOW_DAYS), contents=self._sent_contents()),
            self.exa.search("forums:problems", f"{self.q} problems complaints issues", numResults=20,
                            excludeDomains=SENTIMENT_EXCLUDE, startPublishedDate=iso_days_ago(WINDOW_DAYS), contents=self._sent_contents()),
            self.exa.search("forums:keyword", f"{self.q} review", type="keyword", numResults=50,
                            excludeDomains=SENTIMENT_EXCLUDE, startPublishedDate=iso_days_ago(WINDOW_DAYS), contents=self._sent_contents()),
            self.exa.search("forums:communities", f"{self.q_full} opinion", numResults=30, includeDomains=FORUM_DOMAINS, contents=self._sent_contents()),
        )
        seen = set()
        ms = []
        for r in list(r1) + list(r2) + list(r3) + list(r4):
            if r.get("url") in seen:
                continue
            seen.add(r.get("url"))
            ms.extend(self._keep_mentions([r], "forums"))
        self.mentions.extend(ms)
        await self.surface("forums", "done" if len(ms) >= 3 else "thin", len(ms), "" if len(ms) >= 3 else "few forum or blog posts in the window")

    REDDIT_SCHEMA = {"type": "object", "properties": {"quotes": {"type": "array", "items": {"type": "object", "properties": {
        "text": {"type": "string"}, "sentiment": {"type": "string", "enum": ["positive", "negative", "mixed"]}, "url": {"type": "string"},
        "complaint": {"type": "string", "description": "3-6 word lowercase label if the quote voices a complaint, else empty"}},
        "required": ["text", "sentiment", "url"]}}}, "required": ["quotes"]}

    async def scan_reddit(self):
        await self.surface("reddit", "scanning")
        r1, r2, ans = await asyncio.gather(
            self.exa.search("reddit:redditrecs", f"{self.q}", numResults=5, includeDomains=["redditrecs.com"], contents=self._sent_contents()),
            self.exa.search("reddit:quoted", f"reddit users discuss the {self.q_full}", numResults=10, excludeDomains=["reddit.com", "walmart.com"],
                            startPublishedDate=iso_days_ago(WINDOW_DAYS), contents=self._sent_contents()),
            self.exa.answer("reddit:answer", (f"What do Reddit users say about the {self.q_full} ({self.q})? Give up to 8 verbatim quotes from Reddit users "
                                              f"(as reproduced on pages that quote Reddit threads), each with its sentiment, the URL of the page it appears on, "
                                              f"and a short complaint label if it voices one. Only quotes about this exact product."), self.REDDIT_SCHEMA),
        )
        ms = self._keep_mentions(r1, "reddit")
        a = ans.get("answer") if isinstance(ans.get("answer"), dict) else {}
        for qd in (a or {}).get("quotes") or []:
            if not isinstance(qd, dict):
                continue
            text = norm(qd.get("text") or "")
            url = qd.get("url") or ""
            if len(text) < 20 or not url.startswith("http") or "reddit.com" in host_of(url):
                continue
            if not self.relevant(text, url) and not self.alias_hit(text) and not (self.product["brand"] and contains_word(text, self.product["brand"])):
                continue
            sent = qd.get("sentiment") if qd.get("sentiment") in ("positive", "negative", "mixed") else "neutral"
            label = norm(qd.get("complaint") or "").lower()[:60] or None
            pc = pain_category_of((label or "") + " " + text) if sent in ("negative", "mixed") else None
            ms.append({"url": url, "title": f"Reddit user (quoted on {host_of(url)}): {text[:70]}…", "date": None, "_dt": None, "source": "reddit",
                       "host": host_of(url), "sentiment": sent, "complaint_voiced": pc is not None, "pain_category": pc, "pain_label": label,
                       "praise_label": None if sent != "positive" else text[:60].lower(), "quote": text[:200], "safety_issue": False,
                       "source_kind": "forum_thread", "mentions_product": True, "highlight": text[:240], "_reddit_quote": True})
        for r in r2:
            blob = (r.get("title", "") + " " + " ".join(r.get("highlights") or []) + " " + str(r.get("summary") or "")).lower()
            if "reddit" in blob or "r/" in blob:
                ms.extend(self._keep_mentions([r], "reddit"))
        seen = set()
        ms = [m for m in ms if not ((m["url"], m.get("quote")) in seen or seen.add((m["url"], m.get("quote"))))]
        self.mentions.extend(ms)
        await self.surface("reddit", "indirect" if ms else "degraded", len(ms),
                           "reddit.com blocks crawlers · quotes via pages that cite Reddit" if ms else "reddit.com blocks crawlers · no quoting pages found")

    async def scan_retail(self):
        await self.surface("retail", "scanning")
        res = await self.exa.search("retail-reviews", f"{self.q_full} customer reviews", numResults=15, includeDomains=RETAIL_REVIEW_DOMAINS,
                                    contents={"summary": {"query": (f"From the customer reviews / 'customers say' sections on this retail page for the {self.q_full}: "
                                                                    f"star rating, review count, complaints customers voice, praises customers voice. Only what customers actually say."),
                                                          "schema": RETAIL_SCHEMA}})
        n = 0
        junk = re.compile(r"not (available|specified|determined|included|present|listed|provided|found|shown)|cannot be|no (explicit|specific|clear|customer)|provided (text|excerpt|page)|\bn/a\b|customers voice|reviews section|page text|nothing specific|beyond general|remains high|overall (good|positive|high|quality)|no complaints|none (reported|mentioned)", re.I)
        neg_cue = re.compile("|".join(PAIN_WORDS.values()) + r"|\bnot\b|hard|difficult|hate|poor|bad|issue|problem|disappoint|complain|wish|lack|only\b", re.I)
        pos_cue = re.compile("|".join(p for _, p in PRAISE_WORDS) + r"|love|great|good|excellent|recommend|perfect|works well|happy|pleased", re.I)
        by_merchant = {}
        for r in res:
            s = parse_summary(r.get("summary"))
            if not self.relevant(r.get("title"), r.get("url"), s.get("quote")):
                continue
            host = host_of(r.get("url", ""))
            merchant = BIG_BOX.get(host, host)
            rating = to_float(s.get("rating"))
            rc = to_int(s.get("review_count"))
            comps = [norm(c) for c in (s.get("complaints") or []) if isinstance(c, str) and norm(c)]
            prs = [norm(c) for c in (s.get("praises") or []) if isinstance(c, str) and norm(c)]
            spec = re.compile(r"\d+\s?(°|degrees|watts?|w\b|qt\b|quart|lbs?\b|oz\b|ml\b|inch|in\b)|^[A-Z][\w -]{2,30}:\s", re.I)
            comps = [c for c in comps if not junk.search(c) and neg_cue.search(c) and not spec.search(c) and 8 <= len(c) <= 160][:4]
            prs = [c for c in prs if not junk.search(c) and pos_cue.search(c) and 4 <= len(c) <= 160][:4]
            cur = by_merchant.get(merchant)
            entry = {"merchant": merchant, "url": canon_url(r.get("url")), "rating": rating if rating and rating <= 5 else None,
                     "review_count": rc, "complaints": comps, "praises": prs, "quote": norm(s.get("quote") or "")[:200] or None}
            if cur is None:
                by_merchant[merchant] = entry
            else:
                if (entry["review_count"] or 0) > (cur["review_count"] or 0):
                    cur["rating"], cur["review_count"], cur["url"] = entry["rating"], entry["review_count"], entry["url"]
                elif cur["rating"] is None and entry["rating"]:
                    cur["rating"], cur["review_count"] = entry["rating"], entry["review_count"]
                for c in comps:
                    if c.lower() not in [x.lower() for x in cur["complaints"]]:
                        cur["complaints"].append(c)
                for c in prs:
                    if c.lower() not in [x.lower() for x in cur["praises"]]:
                        cur["praises"].append(c)
                cur["quote"] = cur["quote"] or entry["quote"]
        for e in by_merchant.values():
            e["complaints"], e["praises"] = e["complaints"][:5], e["praises"][:5]
            self.retail.append(e)
            r = {"url": e["url"], "publishedDate": None}
            merchant, host, comps, prs = e["merchant"], host_of(e["url"]), e["complaints"], e["praises"]
            for c in comps:
                pc = pain_category_of(c)
                self.mentions.append({"url": canon_url(r.get("url")), "title": f"{merchant} reviews: {c[:80]}", "date": None, "_dt": None, "source": "retail",
                                      "host": host, "sentiment": "negative", "complaint_voiced": pc is not None, "pain_category": pc,
                                      "pain_label": c.lower()[:60], "praise_label": None, "quote": None, "safety_issue": pain_category_of(c) == "safety",
                                      "source_kind": "owner_review", "mentions_product": True, "highlight": c, "_retail": True})
                n += 1
            for c in prs:
                self.mentions.append({"url": canon_url(r.get("url")), "title": f"{merchant} reviews: {c[:80]}", "date": None, "_dt": None, "source": "retail",
                                      "host": host, "sentiment": "positive", "complaint_voiced": False, "pain_category": None,
                                      "pain_label": None, "praise_label": c.lower()[:60], "quote": None, "safety_issue": False,
                                      "source_kind": "owner_review", "mentions_product": True, "highlight": c, "_retail": True})
                n += 1
        await self.surface("retail", "done" if self.retail else "thin", n, "walmart.com excluded" if self.retail else "no retail review page readable")

    async def scan_cpsc(self):
        await self.surface("cpsc", "scanning")
        b, m = self.product["brand"], self.product["model"]
        sq = (f"Is this a CPSC recall notice? Which product, brand and model numbers are recalled, when, what hazard, how many units? "
              f"Does it explicitly name the {b} {m or ''} {self.product['category']}? Is it the same brand family ({b})?")
        r1, r2, r3 = await asyncio.gather(
            self.exa.search("cpsc:model", f"{b} {m} {self.product['category']} recall".strip(), numResults=10, includeDomains=["cpsc.gov"],
                            startPublishedDate=iso_days_ago(730), contents={"summary": {"query": sq, "schema": RECALL_SCHEMA}}),
            self.exa.search("cpsc:brand", f"{b} recalls product due to hazard", numResults=20, includeDomains=["cpsc.gov"],
                            startPublishedDate=iso_days_ago(1825), contents={"summary": {"query": sq, "schema": RECALL_SCHEMA}}),
            self.exa.search("cpsc:category", f"{b} {self.product['category']} recall hazard", numResults=10, includeDomains=["cpsc.gov"],
                            startPublishedDate=iso_days_ago(730), contents={"summary": {"query": sq, "schema": RECALL_SCHEMA}}),
        )
        seen = set()
        for r in list(r1) + list(r2) + list(r3):
            u = r.get("url", "")
            path = urlparse(u).path.lower().rstrip("/")
            if not re.search(r"/(recalls?|noticias|news)/\d{4}/", path):
                continue
            s = parse_summary(r.get("summary"))
            if not s.get("is_recall_notice"):
                continue
            key = re.sub(r"\W+", " ", norm(r.get("title")).lower())[:60]
            if key in seen:
                continue
            seen.add(key)
            text = " ".join([norm(r.get("title")), norm(s.get("product")), norm(s.get("brand")), norm(s.get("models"))])
            model_hit = bool(m) and contains_word(text, m)
            brand_hit = bool(b) and (contains_word(text, b) or (len(b) >= 5 and b.lower() in text.lower()))
            if not (model_hit or brand_hit):
                continue
            d = parse_date(r.get("publishedDate")) or parse_date(s.get("date") or "")
            product = norm(s.get("product") or r.get("title"))[:140]
            hazard = norm(s.get("hazard") or "")[:140] or None
            es = re.match(r"^(https?://www\.cpsc\.gov)/es/Noticias/(\d{4})/([^/?#]+)", u, re.I)
            if es:
                u = f"{es.group(1)}/Recalls/{es.group(2)}/{es.group(3)}"
                slug = es.group(3).replace("-", " ")
                mm = re.match(r"(.+?) Due to (.+)", slug, re.I)
                product = (mm.group(1) if mm else slug)[:140]
                if mm:
                    hazard = mm.group(2)[:140]
                product = re.sub(r"(\d) (\d) (Million|Billion)", r"\1.\2 \3", product)
            units_raw = norm(s.get("units") or "")
            um = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(million|millones|billion|mil\b|thousand|k\b)?", units_raw, re.I)
            units = None
            if um:
                try:
                    n_units = float(um.group(1).replace(",", ""))
                    mult = {"million": 1e6, "millones": 1e6, "billion": 1e9, "mil": 1e3, "thousand": 1e3, "k": 1e3}.get((um.group(2) or "").lower(), 1)
                    n_units *= mult
                    units = f"~{n_units/1e6:.1f}M units" if n_units >= 1e6 else f"~{int(n_units):,} units" if n_units >= 100 else None
                except ValueError:
                    units = None
            self.recalls.append({"product": product, "date": d.isoformat()[:10] if d else None,
                                 "hazard": hazard, "units": units,
                                 "url": u, "model_level": model_hit and bool(s.get("applies_to_model")), "brand_family": not model_hit})
        await self.surface("cpsc", "done", len(self.recalls) + len(self.safety_news), "cpsc.gov + safety news · model 24 mo · brand 5 y")

    async def scan_safety_news(self):
        b, m = self.product["brand"], self.product["model"]
        sq = (f"Does this page report a safety issue (recall, lawsuit, injury, fire, burn, shock, choking, contamination, regulatory action) for a "
              f"{b} product? Which product, what issue, when? Does it explicitly name the {b} {m or ''} {self.product['category']}? Same brand ({b})?")
        r1, r2 = await asyncio.gather(
            self.exa.search("safety:news", f"{b} {self.product['category']} recall fire injury lawsuit safety", numResults=20, category="news",
                            startPublishedDate=iso_days_ago(WINDOW_DAYS), contents={"summary": {"query": sq, "schema": SAFETY_SCHEMA}}),
            self.exa.search("safety:agencies", f"{b} {self.product['category']} recall", numResults=10, includeDomains=SAFETY_DOMAINS,
                            contents={"summary": {"query": sq, "schema": SAFETY_SCHEMA}}),
        )
        seen = set()
        for r in list(r1) + list(r2):
            s = parse_summary(r.get("summary"))
            if not s.get("is_safety_related") or (s.get("kind") or "none") == "none":
                continue
            title, product, issue = norm(r.get("title")), norm(s.get("product") or ""), norm(s.get("issue") or "")
            named = " ".join([title, product])
            brand_hit = bool(b) and (contains_word(named, b) or (len(b) >= 5 and b.lower() in named.lower()))
            if not brand_hit or not s.get("same_brand"):
                continue
            if re.search(r"does not (report|mention|name|involve)|no (safety )?issue|not (a |an )?" + re.escape(b.lower()) + r"|different brand|unrelated|master list|market shift|roundup|round-up", (issue + " " + title).lower()):
                continue
            if re.search(r"\b(list|guide|deals?|sale)\b", title.lower()) and s.get("kind") in ("regulatory", "investigation"):
                continue
            text = " ".join([title, product, issue])
            key = re.sub(r"\W+", " ", title.lower())[:50]
            if key in seen:
                continue
            seen.add(key)
            d = parse_date(r.get("publishedDate")) or parse_date(s.get("date") or "")
            self.safety_news.append({"title": norm(r.get("title"))[:120], "url": r.get("url"), "date": d.isoformat()[:10] if d else None,
                                     "kind": s.get("kind"), "product": norm(s.get("product") or "")[:100], "issue": norm(s.get("issue") or "")[:160],
                                     "applies_to_model": bool(m) and contains_word(text, m) and bool(s.get("applies_to_model")),
                                     "host": host_of(r.get("url", ""))})
        self.safety_news.sort(key=lambda x: (x["applies_to_model"], x["kind"] == "recall", x["date"] or ""), reverse=True)
        self.safety_news = self.safety_news[:8]

    # ---- 3. price & listings
    def _listing_contents(self):
        upc = self.product.get("upc")
        return {
            "maxAgeHours": 168, "livecrawlTimeout": 12000,
            "summary": {"query": (f"Is this page a retail listing for the exact {self.q_full}" + (f" (UPC {upc})" if upc else "") +
                                  f"? Classify listing_type (exact = same model/size/count, variant = different color/size/bundle, accessory, refurbished_or_used). "
                                  f"Extract the store name, third-party seller if shown, current price in USD as shown, stock status, condition."),
                        "schema": LISTING_SCHEMA},
        }

    def _add_listing(self, r, tag):
        s = parse_summary(r.get("summary"))
        lt = s.get("listing_type") or "not_a_listing"
        host = host_of(r.get("url", ""))
        if not host or host in NON_MERCHANT or host.endswith("walmart.com") or not is_us_host(host):
            return
        if lt in ("not_a_listing",):
            return
        if not self.relevant(r.get("title"), r.get("url")):
            return
        cond = s.get("condition") or "unknown"
        if lt == "refurbished_or_used" or cond in ("used", "refurbished"):
            lt = "refurbished"
        price = to_float(s.get("price_usd"))
        stock = s.get("in_stock")
        # big-box: canonical name; long tail: the host itself (a page's "merchant" field is easy to game)
        merchant = BIG_BOX.get(host) or pretty_host(host)
        seller = norm(s.get("seller") or "") or None
        if seller and (seller.lower() in merchant.lower() or merchant.lower() in seller.lower()):
            seller = None
        d = parse_date(r.get("publishedDate"))
        self.listings_raw.append({"host": host, "merchant": merchant[:40], "seller": seller[:40] if seller else None, "price": price,
                                  "in_stock": stock if isinstance(stock, bool) else None, "type": lt, "url": canon_url(r.get("url")),
                                  "title": norm(r.get("title"))[:140], "published": d, "long_tail": host not in BIG_BOX, "tag": tag})

    async def scan_listings(self):
        big = list(BIG_BOX.keys())
        r1, r2, r3 = await asyncio.gather(
            self.exa.search("listings:bigbox", f"{self.q_full} buy", numResults=20, includeDomains=big, contents=self._listing_contents(), timeout=70.0),
            self.exa.search("listings:longtail", f"buy {self.q_full} online store", numResults=15,
                            excludeDomains=sorted(set(big) | NON_MERCHANT), contents=self._listing_contents(), timeout=70.0),
            self.exa.search("listings:keyword", f"{self.q}", type="keyword", numResults=15, includeDomains=big, contents=self._listing_contents(), timeout=70.0),
        )
        seen = set()
        for tag, rs in (("bigbox", r1), ("longtail", r2), ("keyword", r3)):
            for r in rs:
                if r.get("url") in seen:
                    continue
                seen.add(r.get("url"))
                self._add_listing(r, tag)
        self.alias_support([r.get("title", "") for r in list(r1) + list(r2) + list(r3) if r.get("title")])

    async def scan_price_events(self):
        sq = (f"Does this page report a specific price for the exact {self.q_full} at a specific merchant? Extract merchant, price, the previous price if a "
              f"drop is described, the date the price was observed/published, the kind of event, and condition (new/used/refurbished).")
        pc = {"summary": {"query": sq, "schema": PRICE_EVENT_SCHEMA}}
        since = iso_days_ago(WINDOW_DAYS)
        rs = await asyncio.gather(
            self.exa.search("prices:deal", f"{self.q} deal", numResults=20, excludeDomains=["walmart.com"], startPublishedDate=since, contents=pc),
            self.exa.search("prices:sale", f"{self.q} sale", numResults=20, excludeDomains=["walmart.com"], startPublishedDate=since, contents=pc),
            self.exa.search("prices:drop", f"{self.q} price drop lowest price", numResults=20, excludeDomains=["walmart.com"], startPublishedDate=since, contents=pc),
            self.exa.search("prices:news", f"{self.q} deal sale price", numResults=20, category="news", startPublishedDate=since, contents=pc),
            self.exa.search("prices:walmart", f"{self.q} walmart price", numResults=15, excludeDomains=["walmart.com"], startPublishedDate=since, contents=pc),
            self.exa.answer("prices:answer", (f"List dated price observations for the exact {self.q_full} ({self.q}) at named merchants over the last 12 months: "
                                              f"for each give merchant, price in USD, the date observed, and the source URL. Include sales, price drops and current prices. New condition only."),
                            {"type": "object", "properties": {"observations": {"type": "array", "items": {"type": "object", "properties": {
                                "merchant": {"type": "string"}, "price_usd": {"type": "number"}, "date": {"type": "string"}, "url": {"type": "string"}},
                                "required": ["merchant", "price_usd", "date", "url"]}}}, "required": ["observations"]}),
        )
        ans = rs[-1] if isinstance(rs[-1], dict) else {}
        a = ans.get("answer") if isinstance(ans.get("answer"), dict) else {}
        for o in (a or {}).get("observations") or []:
            if not isinstance(o, dict):
                continue
            price, d, url, merchant = to_float(o.get("price_usd")), parse_date(str(o.get("date") or "")), o.get("url") or "", norm(o.get("merchant") or "")
            if not (price and d and url.startswith("http") and merchant) or (now_utc() - d).days > WINDOW_DAYS or d > now_utc() + timedelta(days=1):
                continue
            if re.search(r"used|refurb|renew|open box|3rd party", merchant, re.I):
                continue
            merchant = self._clean_merchant(merchant, host_of(url))
            if not merchant:
                continue
            self.observations.append({"merchant": merchant, "price": price, "prev": None, "date": d, "url": url, "host": host_of(url),
                                      "event": "listing", "kind": "event", "_answer": True})
        seen = set()
        for r in [x for res in rs[:-1] if isinstance(res, list) for x in res]:
            if r.get("url") in seen:
                continue
            seen.add(r.get("url"))
            s = parse_summary(r.get("summary"))
            if not s.get("exact_product"):
                continue
            if not self.relevant(r.get("title"), " ".join(r.get("highlights") or []), r.get("url")):
                continue
            if (s.get("condition") or "new") in ("used", "refurbished"):
                continue
            price = to_float(s.get("price_usd"))
            merchant = norm(s.get("merchant") or "")
            if not price or not merchant:
                continue
            d = parse_date(s.get("observed_date") or "") or parse_date(r.get("publishedDate"))
            if not d:
                continue
            if not is_us_host(host_of(r.get("url", ""))) and "walmart" not in merchant.lower():
                continue
            merchant = self._clean_merchant(merchant, host_of(r.get("url", "")))
            if not merchant:
                continue
            prev = to_float(s.get("previous_price_usd"))
            if prev is not None and abs(prev - price) < 0.5:
                prev = None
            self.observations.append({"merchant": merchant, "price": price, "prev": prev,
                                      "date": d, "url": r.get("url"), "host": host_of(r.get("url", "")),
                                      "event": s.get("event") or "none", "kind": "event"})

    def _clean_merchant(self, merchant, host):
        """Merchant named by a deal post / answer. Known stores keep their canonical name; a store naming itself
        gets its host; aggregates and second-hand marketplaces are dropped."""
        m = re.split(r"\s*[|·(]", norm(merchant))[0].strip()[:28]
        ml = m.lower()
        if not m or re.search(r"\.(it|de|fr|es|eu|uk|ca|au)\b|multiple|various|several|retailers|marketplace|n/a|unknown|walmart marketplace", ml):
            return None
        if re.search(r"ebay|craigslist|facebook|mercari|poshmark|offerup|aliexpress|temu|wish\b|dhgate|shopgoodwill", ml):
            return None
        if "walmart" in ml:
            return "Walmart"
        for h, name in BIG_BOX.items():
            if name.lower() == ml or h.split(".")[0] == re.sub(r"[^a-z0-9]", "", ml):
                return name
        if host and host not in BIG_BOX and host not in NON_MERCHANT and re.sub(r"[^a-z0-9]", "", ml) in re.sub(r"[^a-z0-9]", "", host):
            return pretty_host(host) if is_us_host(host) else None
        return m

    async def scan_camel(self):
        res = await self.exa.search("prices:trackers", f"{self.q} price history", numResults=10, includeDomains=TRACKER_DOMAINS,
                                    contents={"summary": {"query": f"Price history for the exact {self.q_full}: current, lowest ever, highest ever, average, with dates if shown.", "schema": CAMEL_SCHEMA}})
        best = None
        for r in res:
            s = parse_summary(r.get("summary"))
            if not s.get("exact_product") or not self.relevant(r.get("title"), r.get("url"), s.get("notes")):
                continue
            c = {k: to_float(s.get(k)) for k in ("current", "lowest", "highest", "average")}
            filled = sum(1 for v in c.values() if v)
            if filled and (best is None or filled > best[0]):
                host = host_of(r.get("url", ""))
                label = {"camelcamelcamel.com": "Amazon price history", "pricehistory.app": "Amazon price history", "keepa.com": "Amazon price history",
                         "brickeconomy.com": "LEGO market history", "brickset.com": "LEGO price history"}.get(host, f"{pretty_host(host)} price history")
                best = (filled, {**c, "url": r.get("url"), "notes": norm(s.get("notes") or "")[:200], "label": label, "host": host})
        self.camel = best[1] if best else None

    async def scan_dupes(self):
        res = await self.exa.search("walmart:dupes", f"{self.q_full}", numResults=25, includeDomains=["walmart.com"])
        self.dupes_raw = res

    # ---- 4. assemble
    def assemble(self):
        P = self.product
        as_of = self.as_of
        start = self.start

        def day_of(dt):
            if not dt:
                return None
            return max(0, min(WINDOW_DAYS - 1, (dt - start).days))

        # ---------- mentions & sentiment
        seen = set()
        mentions = []
        for m in self.mentions:
            key = (m["url"], m.get("highlight", "")[:40]) if (m.get("_retail") or m.get("_reddit_quote")) else m["url"]
            if key in seen:
                continue
            seen.add(key)
            mentions.append(m)
        web_mentions = [m for m in mentions if not m.get("_retail")]
        val = {"positive": 1.0, "mixed": 0.5, "negative": 0.0}
        opinion = [m for m in mentions if m["sentiment"] in val]

        def score_of(ms, minimum=6):
            xs = [val[m["sentiment"]] for m in ms]
            return round(sum(xs) / len(xs), 2) if len(xs) >= minimum else None

        score = score_of(opinion)
        last30 = [m for m in opinion if m["_dt"] and (as_of - m["_dt"]).days <= 30]
        prev = [m for m in opinion if m["_dt"] and 30 < (as_of - m["_dt"]).days <= 120]
        s_last, s_prev = score_of(last30, 4), score_of(prev, 4)
        delta = round(s_last - s_prev, 2) if s_last is not None and s_prev is not None else None
        if delta is None:
            s_prev = None
        trend_word = "n/a" if delta is None else ("cooling" if delta <= -0.03 else "warming" if delta >= 0.03 else "steady")
        # sparkline: 13 samples, 21-day trailing windows, linear fill
        spark = []
        spark_start = as_of - timedelta(days=SPARK_DAYS - 1)
        for i in range(13):
            t = spark_start + timedelta(days=i * (SPARK_DAYS - 1) / 12)
            win = [val[m["sentiment"]] for m in opinion if m["_dt"] and t - timedelta(days=21) <= m["_dt"] <= t]
            spark.append(round(sum(win) / len(win), 3) if len(win) >= 3 else None)
        if sum(1 for x in spark if x is not None) >= 6:
            idx = [i for i, x in enumerate(spark) if x is not None]
            for i in range(13):
                if spark[i] is None:
                    lo = max([j for j in idx if j < i], default=None)
                    hi = min([j for j in idx if j > i], default=None)
                    if lo is None:
                        spark[i] = spark[hi]
                    elif hi is None:
                        spark[i] = spark[lo]
                    else:
                        spark[i] = round(spark[lo] + (spark[hi] - spark[lo]) * (i - lo) / (hi - lo), 3)
        else:
            spark = None
        dated = [m for m in web_mentions if m["_dt"]]
        m_last30 = sum(1 for m in dated if (as_of - m["_dt"]).days <= 30)
        m_prev30 = sum(1 for m in dated if 30 < (as_of - m["_dt"]).days <= 60)
        m_last7 = sum(1 for m in dated if (as_of - m["_dt"]).days <= 7)
        m_prev7 = sum(1 for m in dated if 7 < (as_of - m["_dt"]).days <= 14)
        velocity = round((m_last7 - m_prev7) / m_prev7 * 100) if m_prev7 >= 3 and m_last7 + m_prev7 >= 6 else None

        # ---------- pain clusters
        groups = defaultdict(list)
        for m in mentions:
            if m["complaint_voiced"] and m["pain_category"]:
                groups[m["pain_category"]].append(m)
        clusters = []
        for cat, items in groups.items():
            def tidy(lbl):
                lbl = re.sub(r"^(some |a few |many |several |most )?(customers?|users?|reviewers?|owners?|people|buyers?)\s+(say|report|mention|note|complain|find|feel|think)\s+(that\s+)?(it\s+|the\s+)?", "", lbl.strip().lower())
                lbl = re.sub(r"^(the|a|an)\s+", "", lbl)
                return re.sub(r"[.\"']+$", "", lbl).strip()
            pat = PAIN_WORDS.get(cat, "")
            labels = Counter(tidy(m["pain_label"]) for m in items if m["pain_label"] and 3 <= len(m["pain_label"]) <= 80)
            labels = Counter({k: v for k, v in labels.items() if 3 <= len(k) <= 60 and pat and re.search(pat, k)})
            if labels and labels.most_common(1)[0][1] >= 2:
                title = labels.most_common(1)[0][0]
            else:
                short = sorted([k for k in labels if len(k) <= 36], key=len)
                title = short[0] if short else PAIN_DISPLAY.get(cat, cat)
            src = Counter(m["source"] for m in items)
            tot = sum(src.values())
            sources = [{"key": k, "pct": round(v / tot * 100)} for k, v in src.most_common(3)]
            if sources:
                sources[0]["pct"] += 100 - sum(s["pct"] for s in sources)
            di = [m for m in items if m["_dt"]]
            c30 = sum(1 for m in di if (as_of - m["_dt"]).days <= 30)
            praw = sum(1 for m in di if 30 < (as_of - m["_dt"]).days <= WINDOW_DAYS)
            cprev = praw / ((WINDOW_DAYS - 30) / 30.0)      # prior months, per-30-day rate
            if len(di) < 3:
                trend, pct = "flat", None
            elif praw == 0 and c30 >= 3:
                trend, pct = "new", None
            elif c30 >= 3 and cprev > 0 and (c30 - cprev) / cprev >= 0.5:
                trend, pct = "rising", round((c30 - cprev) / cprev * 100)
            elif praw >= 4 and c30 >= 1 and c30 <= cprev * 0.5:
                trend, pct = "falling", round((c30 - cprev) / cprev * 100)
            else:
                trend, pct = "flat", None
            firsts = [m["_dt"] for m in di]
            quote = None
            NEG = re.compile(r"\bnot\b|n't|\bno\b|hard|difficult|hate|poor|bad|issue|problem|disappoint|complain|wish|lack|only|broke|stopp|fail|loud|small|too |annoy|worst|cheap|flimsy|uneven|smell|slow|expensive|overpriced|inconsistent|flak|peel|wear|leak|burn", re.I)
            POS = re.compile(r"easy|great|love|excellent|recommend|perfect|best|exceptional|solid|reliable|works well|happy|pleased|fast|crispy|dependable", re.I)
            def voices(m):
                q = m["quote"] or ""
                if not (20 <= len(q) <= 200 and pat and re.search(pat, q.lower())):
                    return False
                if m.get("_retail"):
                    return True
                if not (m["sentiment"] in ("negative", "mixed") or NEG.search(q)):
                    return False
                return not (POS.search(q) and not NEG.search(q))
            cands = [m for m in items if voices(m)]
            cands.sort(key=lambda m: (m["sentiment"] == "negative", bool(NEG.search(m["quote"] or "")), m["source_kind"] in ("owner_review", "forum_thread", "video"), len(m["quote"] or "")), reverse=True)
            if cands:
                q = cands[0]
                quote = {"text": q["quote"], "source_label": q["host"] or q["source"], "url": q["url"]}
            else:
                rq = [m for m in items if m.get("_retail") and m.get("highlight")]
                if rq:
                    q = rq[0]
                    quote = {"text": q["highlight"], "source_label": f"{q['host']} reviews", "url": q["url"]}
            evidence = [{"title": m["title"], "url": m["url"], "date": m["date"], "source": m["source"]} for m in items]
            clusters.append({"title": title, "category": cat, "mentions": len(items), "trend": trend, "trend_pct": pct,
                             "first_seen": min(firsts).isoformat()[:10] if firsts else None, "sources": sources, "quote": quote,
                             "evidence": evidence, "_safety": cat == "safety"})
        clusters.sort(key=lambda c: (c["_safety"] and c["mentions"] >= 2, c["trend"] in ("rising", "new"), c["mentions"]), reverse=True)
        for i, c in enumerate(clusters):
            c["rank"] = i + 1
            c.pop("_safety", None)
        clusters = clusters[:5]

        praise_groups = Counter()
        praise_dates = defaultdict(list)
        for m in mentions:
            if m["praise_label"]:
                b = praise_bucket(m["praise_label"]) or m["praise_label"][:40]
                praise_groups[b] += 1
                if m["_dt"]:
                    praise_dates[b].append(m["_dt"])
        praises = []
        for label, n in praise_groups.most_common(3):
            ds = praise_dates[label]
            c30 = sum(1 for d in ds if (as_of - d).days <= 30)
            praw = sum(1 for d in ds if 30 < (as_of - d).days <= WINDOW_DAYS)
            cp = praw / ((WINDOW_DAYS - 30) / 30.0)
            tr = "flat" if len(ds) < 3 or cp == 0 else ("rising" if c30 >= 3 and c30 >= cp * 1.5 else "falling" if praw >= 4 and 1 <= c30 <= cp * 0.5 else "flat")
            praises.append({"title": label, "mentions": n, "trend": tr})

        retail = None
        rated = [r for r in self.retail if r["rating"] and (r["review_count"] or 0) >= 10]
        if rated:
            r = max(rated, key=lambda r: r["review_count"] or 0)
            retail = {"rating": r["rating"], "review_count": r["review_count"], "merchant": r["merchant"], "url": r["url"]}
        n_rising = sum(1 for c in clusters if c["trend"] in ("rising", "new"))
        safety_cluster = next((c for c in clusters if c["category"] == "safety" and c["mentions"] >= 2), None)

        # ---------- price observations & series
        obs = []
        for l in self.listings_raw:
            if l["type"] == "exact" and l["price"]:
                obs.append({"merchant": l["merchant"], "host": l["host"], "price": l["price"], "date": as_of, "url": l["url"], "event": "listing",
                            "kind": "listing", "in_stock": l["in_stock"], "seller": l["seller"], "long_tail": l["long_tail"], "published": l["published"]})
        for o in self.observations:
            mk = o["merchant"].lower()
            if "walmart" in mk:
                o["host"] = "walmart.com"
            obs.append({**o, "in_stock": None if o["event"] != "out_of_stock" else False, "seller": None, "long_tail": False, "published": None})

        def mkey(o):
            h = o.get("host") or ""
            if h in BIG_BOX:
                return BIG_BOX[h]
            m = o["merchant"]
            for hh, nm in BIG_BOX.items():
                if nm.lower() in m.lower() or hh.split(".")[0] in m.lower():
                    return nm
            if "walmart" in m.lower():
                return "Walmart"
            return m
        for o in obs:
            o["mname"] = mkey(o)
        walmart_obs = sorted([o for o in obs if o["mname"] == "Walmart"], key=lambda o: o["date"])
        walmart = None
        if walmart_obs:
            w = walmart_obs[-1]
            walmart = {"price": w["price"], "observed": w["date"].isoformat()[:10], "source_label": w["host"] or "open web", "url": w["url"]}
        web_obs = [o for o in obs if o["mname"] != "Walmart" and o["date"] >= start - timedelta(days=1)]
        if len(web_obs) >= 4:
            med = statistics.median(o["price"] for o in web_obs)
            web_obs = [o for o in web_obs if 0.3 * med <= o["price"] <= 3.0 * med]
        merchants = {}
        for o in sorted(web_obs, key=lambda o: o["date"]):
            merchants.setdefault(o["mname"], []).append(o)
        # keep the last-observed price per merchant per day
        series_merchants = []
        for i, (name, ol) in enumerate(sorted(merchants.items(), key=lambda kv: (kv[1][-1]["long_tail"], kv[0]))):
            byday = {}
            for o in ol:
                d = day_of(o["date"])
                if d not in byday or o["price"] < byday[d]["price"]:
                    byday[d] = o
            pts = [{"day": d, "price": round(byday[d]["price"], 2), "url": byday[d]["url"]} for d in sorted(byday)]
            oos = next((day_of(o["date"]) for o in ol if o.get("in_stock") is False), None)
            key = re.sub(r"[^a-z0-9]+", "", name.lower())[:16] or f"m{i}"
            series_merchants.append({"key": key, "name": name, "color": MERCHANT_COLORS[i % len(MERCHANT_COLORS)],
                                     "long_tail": all(o["long_tail"] for o in ol if o["kind"] == "listing") and any(o["kind"] == "listing" for o in ol) or (all(o["kind"] != "listing" for o in ol) and name not in BIG_BOX.values()),
                                     "points": pts, "oos_from_day": oos})
        median_arr, low_arr, high_arr = [], [], []
        for d in range(WINDOW_DAYS):
            vals = []
            for sm in series_merchants:
                cur = None
                for p in sm["points"]:
                    if p["day"] <= d:
                        cur = p["price"]
                if cur is not None and not (sm["oos_from_day"] is not None and d >= sm["oos_from_day"]):
                    vals.append(cur)
            if vals:
                median_arr.append(round(statistics.median(vals), 2)); low_arr.append(round(min(vals), 2)); high_arr.append(round(max(vals), 2))
            else:
                median_arr.append(None); low_arr.append(None); high_arr.append(None)
        median_now = median_arr[-1]
        median_start = next((x for x in median_arr if x is not None), None)
        # a median that only "moves" because coverage grew is not a price trend: compare the cohort of
        # merchants observed at least 30 days ago, carried forward, against the same cohort today
        tracked = [sm for sm in series_merchants if sm["points"] and sm["points"][0]["day"] <= WINDOW_DAYS - 31]
        change_pct = None
        if len(tracked) >= 2:
            def cohort_median(d):
                vals = []
                for sm in tracked:
                    cur = None
                    for p in sm["points"]:
                        if p["day"] <= d:
                            cur = p["price"]
                    if cur is not None:
                        vals.append(cur)
                return statistics.median(vals) if vals else None
            d0 = max(sm["points"][0]["day"] for sm in tracked)
            m0, m1 = cohort_median(d0), cohort_median(WINDOW_DAYS - 1)
            if m0 and m1:
                change_pct = round((m1 - m0) / m0 * 100)
                median_start = round(m0, 2)
        all_prices = [o["price"] for o in web_obs]
        prange = {"low": round(min(all_prices), 2), "high": round(max(all_prices), 2)} if all_prices else None
        n_merch = len(series_merchants)

        # events
        events = []
        for o in web_obs:
            if o["kind"] != "event":
                continue
            ev = o["event"]
            if ev in ("price_drop", "sale", "new_low"):
                verb = ("promo" if ev == "sale" else "new web low" if ev == "new_low" else ("cuts" if o.get("prev") else "deal"))
                lab = f"{o['mname']} {verb}: " + (f"${o['prev']:.2f} → " if o.get("prev") else "") + f"${o['price']:.2f}"
                events.append({"day": day_of(o["date"]), "date": o["date"].isoformat()[:10], "label": lab, "url": o["url"], "kind": "new_low" if ev == "new_low" else "price_drop", "_p": 2 if ev == "new_low" else 1})
            elif ev == "price_increase":
                events.append({"day": day_of(o["date"]), "date": o["date"].isoformat()[:10], "label": f"{o['mname']} raises to ${o['price']:.2f}", "url": o["url"], "kind": "price_increase", "_p": 1})
            elif ev == "out_of_stock":
                events.append({"day": day_of(o["date"]), "date": o["date"].isoformat()[:10], "label": f"{o['mname']} goes OOS", "url": o["url"], "kind": "oos", "_p": 1})
            elif ev == "restock":
                events.append({"day": day_of(o["date"]), "date": o["date"].isoformat()[:10], "label": f"{o['mname']} back in stock at ${o['price']:.2f}", "url": o["url"], "kind": "restock", "_p": 0})
        for l in self.listings_raw:
            if l["type"] == "exact" and l["price"] and l["published"] and (as_of - l["published"]).days <= 45 and l["long_tail"]:
                events.append({"day": day_of(l["published"]), "date": l["published"].isoformat()[:10],
                               "label": f"{l['merchant']} listing first seen at ${l['price']:.2f} — new seller", "url": l["url"], "kind": "new_seller", "_p": 1})
        # walmart position
        walmart_lowest_day = None
        if walmart and low_arr[-1] is not None:
            if walmart["price"] <= low_arr[-1] + 0.01:
                wpos = "lowest now"
                walmart_lowest_day = WINDOW_DAYS - 1
            else:
                last = next((d for d in range(WINDOW_DAYS - 1, -1, -1) if low_arr[d] is not None and walmart["price"] <= low_arr[d] + 0.01), None)
                wpos = f"was lowest {WINDOW_DAYS - 1 - last} days ago" if last is not None else "never lowest in window"
                walmart_lowest_day = last
        elif walmart:
            wpos = "no competing observation"
        else:
            wpos = "unknown (walmart price not observed)"

        # price headline
        if change_pct is not None and median_now:
            acc = f"down {abs(change_pct)}%" if change_pct < 0 else f"up {change_pct}%" if change_pct > 0 else "flat"
            lead, tail = "Web median price ", " over 12 months."
            if walmart:
                diff = walmart["price"] - median_now
                em = (f"Walmart last observed at ${walmart['price']:.2f} ({datetime.fromisoformat(walmart['observed']).strftime('%b %-d')}) — "
                      + (f"${diff:.2f} above the web median." if diff > 0.5 else f"${-diff:.2f} below the web median." if diff < -0.5 else "right at the web median."))
            else:
                em = "Walmart's own price is not exposed to crawlers; walmart.com is compared through prices the open web reports."
            price_headline = {"lead": lead, "accent": acc, "tail": tail, "em": em}
        elif median_now:
            price_headline = {"lead": "Web median price ", "accent": f"${median_now:.2f}", "tail": " today.", "em": "Not enough merchants tracked for 30+ days to call a 12-month trend."}
        else:
            price_headline = {"lead": "No exact-product price observations ", "accent": "on the open web", "tail": " in the last 12 months.", "em": "Try a product with broader retail distribution."}

        # ---------- listings radar
        exact = [l for l in self.listings_raw if l["type"] == "exact"]
        rows_by = {}
        for l in exact:
            nm = mkey(l)
            cur = rows_by.get(nm)
            if cur is None or (l["price"] and (cur["price"] is None or l["price"] < cur["price"])) or (cur["price"] is None and l["price"]):
                rows_by[nm] = {**l, "mname": nm}
        rows = []
        for nm, l in rows_by.items():
            evs = sorted([o for o in web_obs if o["mname"] == nm and o["kind"] == "event" and o["price"]], key=lambda o: o["date"])
            d30 = None
            note = None
            if l["price"] and evs:
                base = evs[0]
                if (as_of - base["date"]).days <= 30 and base.get("prev"):
                    d30 = round(l["price"] - base["prev"], 2)
                elif base["price"] and (as_of - base["date"]).days <= 30 and abs(base["price"] - l["price"]) > 0.5:
                    d30 = round(l["price"] - base["price"], 2)
            first_seen = None
            if l["published"]:
                if (as_of - l["published"]).days <= 30:
                    note = "new " + l["published"].strftime("%b %-d")
                first_seen = l["published"].strftime("%b %Y")
            rows.append({"merchant": nm, "price": l["price"], "delta30": d30, "delta30_note": note,
                         "stock": "in stock" if l["in_stock"] is True else "OOS" if l["in_stock"] is False else "unknown",
                         "type": "exact", "first_seen": first_seen, "url": l["url"], "long_tail": l["long_tail"], "seller": l["seller"]})
        for r in rows:
            r["price_note"] = None
            if r["price"] is None:
                evs = sorted([o for o in web_obs if o["mname"] == r["merchant"] and o["kind"] == "event" and o["price"]], key=lambda o: o["date"])
                if evs:
                    o = evs[-1]
                    r["price"] = o["price"]
                    r["price_note"] = "observed " + o["date"].strftime("%b %-d")
                    r["price_url"] = o["url"]
            if r["stock"] == "OOS":
                events.append({"day": WINDOW_DAYS - 1, "date": as_of.isoformat()[:10], "label": f"{r['merchant']} out of stock", "url": r["url"], "kind": "oos", "_p": 0})
        ev_seen = set()
        ev2 = []
        for e in sorted(events, key=lambda e: (-e["_p"], e["day"])):
            k = (e["day"], e["kind"], e["label"].split(" ")[0])
            if k in ev_seen:
                continue
            ev_seen.add(k)
            ev2.append(e)
        events = sorted(ev2[:6], key=lambda e: e["day"])
        for i, e in enumerate(events):
            e["n"] = i + 1
            e.pop("_p", None)
        rows = [r for r in rows if r["price"] or r["stock"] != "unknown" or not r["long_tail"]]
        # a "listing" priced at a small fraction of the market is an accessory or a bundle the classifier let through
        anchor = None
        priced_all = [r["price"] for r in rows if r["price"]]
        if walmart and walmart.get("price"):
            anchor = walmart["price"]
        elif len(priced_all) >= 3:
            anchor = statistics.median(priced_all)
        if anchor:
            rows = [r for r in rows if not r["price"] or 0.3 * anchor <= r["price"] <= 3.0 * anchor]
        rows.sort(key=lambda r: (r["price"] is None, r["stock"] == "OOS", r["price"] or 0))
        rows = rows[:10]
        priced = [r for r in rows if r["price"] and r["stock"] != "OOS"]
        lrange = {"low": min(r["price"] for r in priced), "high": max(r["price"] for r in priced)} if priced else None
        wprice = walmart["price"] if walmart else None
        ticks = []
        for r in priced:
            t = next((t for t in ticks if abs(t["price"] - r["price"]) < 0.01), None)
            if t:
                t["labels"].append(r["merchant"])
            else:
                ticks.append({"price": r["price"], "labels": [r["merchant"]]})
        ticks = ticks[:5]
        chips = []
        chip_seen = set()
        for r in priced:
            if r["delta30"] is not None and r["delta30"] < 0:
                chips.append({"kind": "price_dropped", "text": f"price_dropped · {r['merchant']} −${abs(r['delta30']):.2f}", "tone": "red"})
                chip_seen.add(r["merchant"])
        for e in events:
            if e["kind"] in ("price_drop", "new_low") and (WINDOW_DAYS - 1 - e["day"]) <= 30:
                mname = e["label"].split(" ")[0]
                if mname not in chip_seen:
                    m2 = re.search(r"\$([\d.]+) → \$([\d.]+)", e["label"])
                    txt = f"price_dropped · {mname} −${float(m2.group(1)) - float(m2.group(2)):.2f} ({datetime.fromisoformat(e['date']).strftime('%b %-d')})" if m2 else f"{e['kind']} · {mname} ({datetime.fromisoformat(e['date']).strftime('%b %-d')})"
                    chips.append({"kind": e["kind"] if e["kind"] == "new_low" else "price_dropped", "text": txt, "tone": "red"})
                    chip_seen.add(mname)
        for r in rows:
            if r["delta30_note"]:
                chips.append({"kind": "new_seller", "text": f"new_seller · {r['merchant']} ({r['delta30_note'][4:]})", "tone": "amber"})
        if wprice:
            for r in priced:
                if r["price"] < wprice - 0.5:
                    chips.append({"kind": "below_walmart", "text": f"below_walmart · {r['merchant']} −${wprice - r['price']:.2f}", "tone": "red"})
        for r in rows:
            if r["stock"] == "OOS":
                chips.append({"kind": "competitor_oos", "text": f"competitor_oos · {r['merchant']} · opportunity", "tone": "green"})
        chips = chips[:6]
        if wprice and priced:
            if min(r["price"] for r in priced) < wprice - 0.5:
                l_head = {"lead": "Walmart is ", "accent": "not the lowest price."}
            else:
                l_head = {"lead": "Walmart's last observed price is ", "accent": "the web low."}
        elif priced:
            l_head = {"lead": "Walmart's price is ", "accent": "not observed on the open web."}
        else:
            l_head = {"lead": "No exact listing found ", "accent": "outside walmart.com."}

        # ---------- dupes
        dupes_exact, dupes_other = [], []
        seen_ids = set()
        primary_title = P["name"]
        ptoks = set(tokens(primary_title))
        for r in self.dupes_raw:
            u = r.get("url", "")
            m = re.search(r"/(?:ip|product)/(?:[^?#]*/)?(\d{4,})(?:[/?#]|$)", u)
            if not m:
                continue
            did = m.group(1)
            if did == self.id or did in seen_ids:
                continue
            title = norm(r.get("title"))
            title = re.sub(r"^customer reviews for\s+", "", title, flags=re.I)
            title = re.sub(r"\s*[-|–]\s*walmart(\.com)?\s*$", "", title, flags=re.I)
            if not title:
                continue
            tl = title.lower()
            kind = None
            if re.search(r"restored|refurbished|renewed|pre-owned|open box|\bused\b", tl):
                kind = "refurbished"
            elif re.search(r"\bfor (the )?" + re.escape(P["brand"].lower()) + r"\b|compatible|replacement|\bfits\b|\bliners?\b|accessor|\bcase for|\bcover for|\bspare\b|parts kit|\bfilters? for|\bparchment\b|\bsilicone\b|\bstand for|\bmount for|\bcable for|\bcharger for|\bskin for|\bscreen protector", tl):
                kind = "accessory"
            elif P["model"] and contains_word(tl, P["model"]):
                kind = "exact"
            else:
                ttoks = set(tokens(title))
                sim = len(ptoks & ttoks) / max(1, min(len(ptoks), len(ttoks)))
                cat_ok = self.cat_tokens and all(contains_word(tl, t) for t in self.cat_tokens[:3])
                SZ = re.compile(r"(\d+(?:\.\d+)?)\s?-?\s?(qt|quart|oz|ounce|lb|lbs|inch|in\b|ft|gal|gallon|ml|l\b|liter|gb|tb|ct|count|pack|pk|pc|pcs|piece|pieces)", re.I)
                def size_key(s):
                    mm = SZ.search(s or "")
                    return (mm.group(1), mm.group(2).lower()[:2]) if mm else None
                extra = {t for t in ttoks - ptoks if not re.fullmatch(r"\d+", t)}
                CONTRAST = {"crunchy", "creamy", "natural", "organic", "stir", "reduced", "fat", "honey", "chunky", "smooth", "unsweetened", "sugar", "light",
                            "mini", "max", "xl", "pro", "plus", "lite", "refill", "bundle", "case", "2pack", "3pack", "twin", "family", "value"}
                same_size = size_key(primary_title) is None or size_key(title) is None or size_key(title) == size_key(primary_title)
                if not P["model"] and sim >= 0.7 and same_size and not (extra & CONTRAST) and len(extra) <= 2:
                    kind = "exact"
                elif not P["model"] and sim >= 0.5 and contains_word(tl, P["brand"]):
                    kind = "variant"
                elif contains_word(tl, P["brand"]) and cat_ok and (sim >= 0.45 or (P["model"] and re.search(re.escape(re.sub(r"\d+$", "", P["model"]).lower()) + r"\d", tl))):
                    kind = "variant"
            if not kind:
                continue
            seen_ids.add(did)
            d = parse_date(r.get("publishedDate"))
            item = {"id": did, "url": f"https://www.walmart.com/ip/{did}" if "/reviews/" in u else u.split("?")[0], "title": title[:140], "kind": kind,
                    "indexed": d.isoformat()[:10] if d else None, "price": None, "seller": None}
            (dupes_exact if kind == "exact" else dupes_other).append(item)
        dupes_exact = dupes_exact[:4]
        dupes_other = dupes_other[:6]

        # ---------- recall
        model_items = [r for r in self.recalls if r["model_level"]]
        for sn in self.safety_news:
            if sn["applies_to_model"] and sn["kind"] == "recall":
                model_items.append({"product": sn["product"] or sn["title"], "date": sn["date"], "hazard": sn["issue"], "units": None, "url": sn["url"],
                                    "model_level": True, "brand_family": False, "via": sn["host"]})
        fam = [r for r in self.recalls if not r["model_level"]][:3]
        safety_news = [sn for sn in self.safety_news if not (sn["applies_to_model"] and sn["kind"] == "recall")][:6]
        safety_mentions = [m for m in web_mentions if m["safety_issue"]]
        label = P.get("label") or P["model"] or P["name"].split(",")[0]
        if model_items:
            r_status = "act"
            r_head = f"CPSC recall names the {label}: {model_items[0]['hazard'] or 'see notice'}."
        else:
            r_status = "clear"
            r_head = f"No CPSC recall for the {label} in the last 24 months."
        for f in fam:
            f["why"] = "same brand family, different product — brand-trust spillover in search"

        # ---------- board
        board = []
        # sentiment
        if score is None and not clusters:
            st = "thin"
        elif (score is not None and score < 0.45) or safety_cluster or (n_rising and delta is not None and delta <= -0.05):
            st = "act"
        elif n_rising or (delta is not None and delta <= -0.03):
            st = "watch"
        else:
            st = "clear"
        top = clusters[0] if clusters else None
        board.append({"key": "sentiment", "num": "01", "title": "Sentiment Pulse", "status": st,
                      "line1": (f"{score:.2f} · {delta:+.2f} / 30d" if score is not None and delta is not None else f"{score:.2f} · trend n/a" if score is not None else "score — · thin data"),
                      "line1_color": "red" if delta is not None and delta <= -0.05 else "amber" if delta is not None and delta <= -0.03 else "grey",
                      "line2": (f"{n_rising} pain cluster{'s' if n_rising != 1 else ''} rising" + (f" +{top['trend_pct']}%" if top and top["trend"] == "rising" and top["trend_pct"] else "")) if n_rising
                               else (f"top complaint: {top['title']}" if top else "no pain cluster on the open web"),
                      "line2_color": "red" if n_rising else "grey"})
        # price
        if n_merch < 2 and not walmart:
            st = "thin"
        elif walmart and median_now and walmart["price"] > median_now * 1.05:
            st = "act"
        elif walmart and low_arr[-1] is not None and low_arr[-1] < walmart["price"] * 0.9:
            st = "act"
        elif (walmart is None and change_pct is not None and change_pct <= -10) or (walmart and median_now and walmart["price"] > median_now):
            st = "watch"
        else:
            st = "clear"
        board.append({"key": "price", "num": "02", "title": "Price History", "status": st,
                      "line1": f"web median {change_pct:+d}% / 12mo" if change_pct is not None else (f"web median ${median_now:.2f}" if median_now else "no dated observations"),
                      "line1_color": "red" if change_pct is not None and change_pct <= -10 else "grey",
                      "line2": (f"Walmart {'+' if walmart['price'] >= median_now else '−'}${abs(walmart['price'] - median_now):.2f} vs median" if walmart and median_now else "Walmart price not observed"),
                      "line2_color": "red" if walmart and median_now and walmart["price"] > median_now + 0.5 else "grey"})
        # listings
        undercuts = sorted([r for r in priced if wprice and r["price"] < wprice - 0.5], key=lambda r: r["price"])
        if len(rows) < 2:
            st = "thin"
        elif undercuts:
            st = "act"
        elif wprice is None or any(c["kind"] in ("new_seller", "competitor_oos") for c in chips):
            st = "watch"
        else:
            st = "clear"
        board.append({"key": "listings", "num": "03", "title": "Listing Radar", "status": st,
                      "line1": "not the lowest price" if undercuts else "lowest price on the web" if (wprice and priced) else f"{len(rows)} merchant{'s' if len(rows)!=1 else ''} · Walmart unobserved",
                      "line1_color": "red" if undercuts else "grey",
                      "line2": " · ".join(f"{r['merchant']} −${wprice - r['price']:.0f}" for r in undercuts[:2]) if undercuts else (f"web low ${lrange['low']:.2f} · high ${lrange['high']:.2f}" if lrange else "no priced listings"),
                      "line2_color": "grey"})
        # dupes
        nd = len(dupes_exact)
        board.append({"key": "dupes", "num": "04", "title": "Internal Dupes", "status": "watch" if nd else "clear",
                      "line1": f"{nd} sibling walmart.com listing{'s' if nd != 1 else ''}" if nd else "single walmart.com listing",
                      "line1_color": "amber" if nd else "grey",
                      "line2": (f"{len(dupes_other)} variant/refurb/accessory pages" if dupes_other else "review equity split across pages") if nd else (f"{len(dupes_other)} variant/refurb/accessory pages" if dupes_other else "no sibling pages indexed"),
                      "line2_color": "grey"})
        # recall
        n_flags = len(fam) + len(safety_news)
        board.append({"key": "recall", "num": "05", "title": "Recall & Safety", "status": "act" if model_items else ("watch" if n_flags or safety_mentions else "clear"),
                      "line1": f"recall · {model_items[0]['date'] or 'dated'}" if model_items else f"no CPSC recall · {label}",
                      "line1_color": "red" if model_items else "grey",
                      "line2": (f"{n_flags} brand-family flag{'s' if n_flags != 1 else ''}" if n_flags else
                                (f"{len(safety_mentions)} safety-classified complaint{'s' if len(safety_mentions)!=1 else ''}" if safety_mentions else "no brand-family flags")),
                      "line2_color": "amber" if n_flags or safety_mentions else "grey"})

        # ---------- verdict
        if model_items:
            verdict = {"lead": "Safety first.", "em": "An active CPSC recall names this model:", "accent": (model_items[0]["hazard"] or "see notice").rstrip(".") + ".", "accent_tone": "red"}
        else:
            if score is None:
                lead = "Thin signal on the open web."
            else:
                mood = "Positive" if score >= 0.7 else "Mixed" if score >= 0.5 else "Negative"
                tw = {"cooling": " but cooling." if mood != "Negative" else " and cooling.", "warming": " and warming.", "steady": " and steady.", "n/a": "."}[trend_word]
                lead = mood + tw
            if top and top["trend"] in ("rising", "new"):
                verdict = {"lead": lead, "em": "One pain cluster is growing:" if n_rising == 1 else f"{n_rising} pain clusters are growing, led by", "accent": top["title"] + ".", "accent_tone": "red"}
            elif top:
                verdict = {"lead": lead, "em": "Top complaint on the open web:", "accent": top["title"] + ".", "accent_tone": "amber" if top["mentions"] >= 3 else "grey"}
            else:
                verdict = {"lead": lead, "em": "No voiced complaint cluster in the window.", "accent": "", "accent_tone": "green"}

        report = {
            "id": self.id, "url": self.p["url"], "as_of": as_of.strftime("%Y-%m-%dT%H:%M:%SZ"), "from_cache": False,
            "product": {k: v for k, v in P.items() if k != "confidence"} | {"confidence": P.get("confidence")},
            "surfaces": [self.surfaces[k] for k in ("reddit", "youtube", "tiktok", "news", "forums", "retail", "cpsc") if k in self.surfaces],
            "mentions": {"total": len(web_mentions), "last30": m_last30, "prev30": m_prev30, "velocity_pct": velocity, "window_days": WINDOW_DAYS, "labeled_total": len(mentions)},
            "verdict": verdict,
            "board": board,
            "sentiment": {"score": score, "delta30": delta, "trend_word": trend_word, "score_prev": s_prev, "score_last30": s_last, "spark": spark,
                          "n_labeled": len(opinion), "retail": retail, "clusters": clusters, "praises": praises,
                          "safety_mentions": [{"title": m["title"], "url": m["url"], "date": m["date"]} for m in safety_mentions][:5]},
            "price": {"walmart": walmart, "median_now": median_now, "median_start": median_start, "median_change_pct": change_pct, "range": prange,
                      "n_observations": len(web_obs), "n_merchants": n_merch,
                      "series": {"days": WINDOW_DAYS, "start_date": start.isoformat()[:10], "merchants": series_merchants, "median": median_arr, "low": low_arr, "high": high_arr},
                      "events": events, "walmart_position": wpos, "walmart_lowest_day": walmart_lowest_day, "headline": price_headline,
                      "amazon_history": self.camel,
                      "method_note": (f"exact-product listings only (variants, accessories, used and refurbished excluded). Series built from {len(web_obs)} dated price observations "
                                      f"across {n_merch} merchants; each merchant line carries its last observed price forward. walmart.com is not crawlable, so Walmart's price is the "
                                      f"latest one reported by other sites.")},
            "listings": {"headline": l_head, "range": lrange, "walmart_price": wprice, "walmart_url": self.p["url"], "ticks": ticks, "chips": chips, "rows": rows, "n_merchants": len(rows),
                         "live_date": as_of.isoformat()[:10],
                         "deep_scan": {"available": True, "status": "idle", "note": "Exa Agent + Affiliate.com catalog (~2–3 min)"}},
            "dupes": {"primary": {"id": self.id, "url": self.p["url"], "title": primary_title}, "exact": dupes_exact, "other": dupes_other, "count_exact": nd,
                      "summary": (f"Exa's index of walmart.com holds {nd} other page{'s' if nd != 1 else ''} selling this exact product"
                                  + (f" and {len(dupes_other)} variant, refurbished or accessory page{'s' if len(dupes_other) != 1 else ''}" if dupes_other else "")
                                  + ". Review equity and ad spend split across sibling pages; a shopper can land on any of them.") if nd
                      else (f"Only one walmart.com page sells this exact product" + (f"; {len(dupes_other)} variant, refurbished or accessory pages orbit it" if dupes_other else "") + "."),
                      "note": "walmart.com blocks crawlers, so sibling-listing prices and review counts are not readable from the open web; listing identity comes from Exa's index of walmart.com titles. Run the deep scan for Walmart prices via the Affiliate.com catalog.",
                      "suggestion": "Consolidate listings or align pricing" if nd else "No action"},
            "recall": {"verified": as_of.isoformat()[:10],
                       "model_level": {"status": r_status, "headline": r_head, "items": model_items},
                       "brand_family": fam,
                       "safety_news": safety_news,
                       "complaint_scan": {"count": len(safety_mentions), "items": [{"title": m["title"], "url": m["url"], "date": m["date"]} for m in safety_mentions][:5]}},
            "cost": {"calls": len(self.exa.calls), "dollars": round(self.exa.cost, 3)},
            "scan_errors": getattr(self, "scan_errors", []),
            "calls": self.exa.calls,
        }
        return report

    async def run(self):
        await self.resolve()
        if self.exa.no_credits:
            await self.exa.close()
            raise RuntimeError("Exa credits are exhausted on the server's API key — top up at dashboard.exa.ai, then run again")
        await self.emit_raw("resolve", {"id": self.id, "url": self.p["url"], "name": self.product["name"], "brand": self.product["brand"],
                                        "model": self.product["model"], "short": self.product["short"], "aliases": self.product["aliases"]})
        for k in ("reddit", "youtube", "tiktok", "news", "forums", "retail", "cpsc"):
            await self.surface(k, "queued")
        names = ["reddit", "youtube", "tiktok", "news", "forums", "retail", "cpsc", "listings", "price_events", "camel", "dupes", "safety_news"]
        scans = [self.scan_reddit(), self.scan_youtube(), self.scan_tiktok(), self.scan_news(), self.scan_forums(), self.scan_retail(), self.scan_cpsc(),
                 self.scan_listings(), self.scan_price_events(), self.scan_camel(), self.scan_dupes(), self.scan_safety_news()]
        results = await asyncio.gather(*scans, return_exceptions=True)
        self.scan_errors = []
        for name, res in zip(names, results):
            if isinstance(res, Exception):
                import traceback
                self.scan_errors.append(f"{name}: {type(res).__name__}: {str(res)[:120]}")
                print("SCAN ERROR", name, "".join(traceback.format_exception(type(res), res, res.__traceback__))[-700:])
        # alias support may have landed after the resolve event; resend
        await self.emit_raw("resolve", {"id": self.id, "url": self.p["url"], "name": self.product["name"], "brand": self.product["brand"],
                                        "model": self.product["model"], "short": self.product["short"], "aliases": self.product["aliases"], "final": True})
        report = self.assemble()
        await self.emit_raw("count", {"mentions": report["mentions"]["total"]})
        await self.exa.close()
        return report


# ---------------------------------------------------------------- cache & gate

def cache_path(item_id):
    return CACHE_DIR / f"{item_id}.json"


def load_cache(item_id, max_age_hours=None):
    p = cache_path(item_id)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
    except Exception:
        return None
    if max_age_hours is not None:
        t = parse_date(d.get("as_of"))
        if not t or (now_utc() - t).total_seconds() > max_age_hours * 3600:
            return None
    return d


def live_gate(ip):
    if _active_runs >= MAX_CONCURRENT_RUNS:
        return "Three live reports are already running — give them a minute, or replay a preset (cached, instant)."
    now = time.time()
    while _run_log and now - _run_log[0] > 3600:
        _run_log.popleft()
    per_ip = _run_log_by_ip.setdefault(ip, deque())
    while per_ip and now - per_ip[0] > 3600:
        per_ip.popleft()
    if len(_run_log) >= LIVE_RUNS_PER_HOUR or len(per_ip) >= LIVE_RUNS_PER_IP_PER_HOUR:
        return "Live-report budget for this hour is used up (each report spends real Exa credits). Replay a preset, or try again later."
    _run_log.append(now)
    per_ip.append(now)
    return None


def client_ip(request):
    xf = request.headers.get("x-forwarded-for")
    return (xf.split(",")[0].strip() if xf else request.client.host) or "?"


# ---------------------------------------------------------------- app

app = FastAPI(title="Product Pulse")


@app.get("/")
async def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/presets")
async def api_presets():
    out = []
    for p in presets():
        parsed = parse_walmart(p["url"])
        out.append({**p, "id": parsed["id"] if parsed else None, "cached": bool(parsed and cache_path(parsed["id"]).exists())})
    return out


@app.get("/api/report/{item_id}")
async def api_report(item_id: str):
    d = load_cache(re.sub(r"\D", "", item_id))
    if not d:
        return JSONResponse({"error": "no cached report"}, status_code=404)
    d["from_cache"] = True
    return d


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/api/pulse")
async def api_pulse(request: Request, url: str = "", mode: str = "live"):
    parsed = parse_walmart(url)

    async def gen():
        global _active_runs
        if not parsed:
            yield sse("error", {"code": "not_walmart", "message": "Only walmart.com product URLs work in this demo."})
            return
        cached = load_cache(parsed["id"], CACHE_TTL_HOURS if mode == "live" else None)
        if cached and (mode == "cached" or mode == "live"):
            cached["from_cache"] = True
            yield sse("resolve", {"id": cached["id"], "url": cached["url"], "name": cached["product"]["name"], "brand": cached["product"]["brand"],
                                  "model": cached["product"]["model"], "short": cached["product"]["short"], "aliases": cached["product"]["aliases"], "final": True})
            await asyncio.sleep(0.4)
            for s in cached.get("surfaces", []):
                yield sse("surface", {**s, "status": "scanning"})
            for s in cached.get("surfaces", []):
                await asyncio.sleep(0.25)
                yield sse("surface", s)
            yield sse("count", {"mentions": cached["mentions"]["total"]})
            yield sse("report", cached)
            return
        if mode == "cached":
            yield sse("error", {"code": "not_found", "message": "No cached report for this item yet — run it live."})
            return
        if not EXA_API_KEY:
            yield sse("error", {"code": "upstream", "message": "Server has no EXA_API_KEY configured."})
            return
        err = live_gate(client_ip(request))
        if err:
            yield sse("error", {"code": "rate_limited", "message": err})
            return
        q: asyncio.Queue = asyncio.Queue()

        async def emit(event, data):
            await q.put((event, data))

        pulse = Pulse(parsed, emit)
        _active_runs += 1

        async def runner():
            try:
                report = await pulse.run()
                cache_path(parsed["id"]).write_text(json.dumps(report, ensure_ascii=False))
                await q.put(("report", report))
            except Exception as e:  # noqa
                import traceback
                traceback.print_exc()
                msg = str(e)[:200] if isinstance(e, RuntimeError) else f"Report failed: {type(e).__name__}: {str(e)[:160]}"
                await q.put(("error", {"code": "upstream", "message": msg}))
            finally:
                await q.put((None, None))

        task = asyncio.create_task(runner())
        try:
            last = time.monotonic()
            while True:
                try:
                    event, data = await asyncio.wait_for(q.get(), timeout=8.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if event is None:
                    break
                yield sse(event, data)
                last = time.monotonic()
        finally:
            _active_runs -= 1
            if not task.done():
                task.cancel()
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- deep scan (Exa Agent + Affiliate.com)

_deep: dict = {}


def deep_path(item_id):
    return CACHE_DIR / f"{item_id}.deep.json"


@app.post("/api/deepscan")
async def deep_start(request: Request, id: str = ""):
    item_id = re.sub(r"\D", "", id)
    if not item_id:
        return JSONResponse({"status": "error", "message": "missing id"}, status_code=400)
    if deep_path(item_id).exists():
        return json.loads(deep_path(item_id).read_text())
    if item_id in _deep and _deep[item_id].get("status") == "running":
        return {"status": "running"}
    rep = load_cache(item_id)
    if not rep:
        return JSONResponse({"status": "error", "message": "run the report first"}, status_code=400)
    now = time.time()
    while _deep_log and now - _deep_log[0] > 3600:
        _deep_log.popleft()
    if len(_deep_log) >= DEEP_SCANS_PER_HOUR or any(v.get("status") == "running" for v in _deep.values()):
        return JSONResponse({"status": "error", "message": "deep-scan budget in use — try again in a few minutes"}, status_code=429)
    _deep_log.append(now)
    P = rep["product"]
    q = (f"Find every current retail offer for the exact {P['brand']} {P['model'] or ''} {P['category']} — \"{P['name']}\""
         + (f" (UPC {P['upc']})" if P.get("upc") else "") + ". New condition only, US merchants. For each offer give merchant name, price in USD, "
         f"in-stock status, condition and the direct product URL. Include walmart.com offers if the catalog has them.")
    body = {"query": q, "dataSources": [{"provider": "affiliate"}],
            "outputSchema": {"type": "object", "required": ["offers"], "properties": {"offers": {"type": "array", "maxItems": 15, "items": {
                "type": "object", "required": ["merchant", "price_usd", "url"], "properties": {
                    "merchant": {"type": "string"}, "price_usd": {"type": "number"}, "in_stock": {"type": "boolean"},
                    "url": {"type": "string"}, "condition": {"type": "string"}}}}}}}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(EXA_BASE + "/agent/runs", json=body, headers={"Authorization": f"Bearer {EXA_API_KEY}", "Content-Type": "application/json"})
    if r.status_code >= 300:
        return JSONResponse({"status": "error", "message": f"agent start failed ({r.status_code})"}, status_code=502)
    run_id = r.json().get("id")
    _deep[item_id] = {"status": "running", "run_id": run_id, "started": now}
    (CACHE_DIR / f"{item_id}.deep.run.json").write_text(json.dumps(_deep[item_id]))
    return {"status": "running"}


@app.get("/api/deepscan")
async def deep_status(id: str = ""):
    item_id = re.sub(r"\D", "", id)
    if deep_path(item_id).exists():
        return json.loads(deep_path(item_id).read_text())
    st = _deep.get(item_id)
    if not st and (CACHE_DIR / f"{item_id}.deep.run.json").exists():
        try:
            st = json.loads((CACHE_DIR / f"{item_id}.deep.run.json").read_text())
            _deep[item_id] = st
        except Exception:
            st = None
    if not st:
        return {"status": "idle"}
    if st["status"] != "running":
        return st
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{EXA_BASE}/agent/runs/{st['run_id']}", headers={"Authorization": f"Bearer {EXA_API_KEY}"})
    if r.status_code >= 300:
        return {"status": "running", "elapsed": int(time.time() - st["started"])}
    d = r.json()
    if d.get("status") in ("completed", "failed", "cancelled"):
        if d.get("status") != "completed":
            st.update({"status": "error", "message": f"agent run {d.get('status')}"})
            return st
        offers = []
        walmart = None
        seen = set()
        for o in ((d.get("output") or {}).get("structured") or {}).get("offers", []) or []:
            price = to_float(o.get("price_usd"))
            url = o.get("url") or ""
            host = host_of(url)
            if not price or not host:
                continue
            cond = (o.get("condition") or "new").lower()
            if cond not in ("new", "unknown", ""):
                continue
            merchant = BIG_BOX.get(host) or norm(o.get("merchant") or host)[:40]
            if host.endswith("walmart.com"):
                if not walmart or price < walmart["price"]:
                    walmart = {"price": price, "url": url, "merchant": norm(o.get("merchant") or "Walmart")[:40]}
                continue
            k = merchant.lower()
            if k in seen:
                continue
            seen.add(k)
            offers.append({"merchant": merchant, "price_usd": price, "in_stock": o.get("in_stock"), "url": url, "condition": cond or "new",
                           "long_tail": host not in BIG_BOX})
        res = {"status": "done", "offers": sorted(offers, key=lambda o: o["price_usd"]), "walmart": walmart,
               "cost": (d.get("costDollars") or {}).get("total"), "as_of": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
               "elapsed": int(time.time() - st["started"])}
        deep_path(item_id).write_text(json.dumps(res))
        _deep[item_id] = res
        return res
    return {"status": "running", "elapsed": int(time.time() - st["started"])}


@app.get("/{path:path}")
async def static_files(path: str):
    if path.startswith("static/"):
        path = path[len("static/"):]
    f = (ROOT / "static" / path).resolve()
    if f.is_file() and str(f).startswith(str((ROOT / "static").resolve())):
        return FileResponse(f)
    return FileResponse(ROOT / "static" / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
