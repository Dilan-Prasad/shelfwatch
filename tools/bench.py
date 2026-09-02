"""Coverage benchmark: how many RELEVANT hits does each Exa strategy return per vertical?"""
import asyncio, json, os, sys, time, re
sys.path.insert(0, ".")
import app as A
from datetime import datetime, timezone

PRODUCTS = {
  "ninja":   dict(brand="Ninja", model="AF101", name="Ninja AF101 Air Fryer that Crisps, Roasts, Reheats, & Dehydrates, 4 Quart Capacity, High Gloss Finish, Black/Grey", category="air fryer",
                  aliases=["Ninja AF101", "Ninja 4qt air fryer", "AF101 air fryer"], q="Ninja AF101", q_full="Ninja AF101 air fryer", mil=True),
  "lego":    dict(brand="LEGO", model="75440", name="LEGO Star Wars AT-AT Walker, Collectible Building Set, Ages 18+", category="building set",
                  aliases=["LEGO 75440", "LEGO 75440 AT-AT", "LEGO Star Wars AT-AT 75440"], q="LEGO 75440", q_full="LEGO 75440 AT-AT building set", mil=True),
  "airpods": dict(brand="Apple", model="MTJV3", name="Apple, Wireless Earbuds, AirPods Pro 2, Active Noise Cancellation", category="wireless earbuds",
                  aliases=["AirPods Pro 2", "Apple AirPods Pro 2 USB-C", "AirPods Pro 2nd generation"], q="AirPods Pro 2", q_full="AirPods Pro 2 wireless earbuds", mil=False),
  "stanley": dict(brand="Stanley", model="RP3254340562", name="Stanley Quencher H2.0 FlowState Tumbler 40oz Soft Matte (Dune)", category="tumbler",
                  aliases=["Stanley Quencher 40oz", "Stanley Quencher H2.0 FlowState 40 oz", "Stanley 40 oz tumbler"], q="Stanley Quencher 40oz", q_full="Stanley Quencher 40oz tumbler", mil=False),
}
DEAL_DOMAINS = ["slickdeals.net", "dealnews.com", "bensbargains.com", "bradsdeals.com", "techbargains.com", "9to5toys.com", "dealcatcher.com", "gottadeal.com",
                "pepperdeals.com", "pzdeals.com", "dealmoon.com", "woot.com", "thekrazycouponlady.com", "hip2save.com", "clarkdeals.com", "dealsplus.com",
                "cnet.com", "tomsguide.com", "pcmag.com", "theverge.com", "engadget.com", "people.com", "bestproducts.com", "allrecipes.com", "foodandwine.com",
                "syracuse.com", "pennlive.com", "kansascity.com", "nj.com", "oregonlive.com", "al.com", "mlive.com", "cleveland.com", "masslive.com", "nypost.com",
                "usatoday.com", "forbes.com", "cnn.com", "today.com", "goodhousekeeping.com", "thespruce.com", "kinja.com", "rtings.com", "wirecutter.com", "nytimes.com"]
TRACKER_DOMAINS = ["camelcamelcamel.com", "pricepulse.app", "keepa.com", "pricespy.com", "shopsavvy.com", "brickeconomy.com", "brickset.com", "price.com",
                   "pricegrabber.com", "honey.com", "pricetracker.com", "pricehistory.app", "priceintime.com", "pricepirates.com", "klarna.com"]
FORUM_DOMAINS = ["quora.com", "forums.redflagdeals.com", "slickdeals.net", "hardforum.com", "avsforum.com", "houzz.com", "food52.com", "macrumors.com", "head-fi.org",
                 "dpreview.com", "cooking.stackexchange.com", "lemmy.world", "news.ycombinator.com", "community.bestbuy.com", "community.homedepot.com",
                 "forums.anandtech.com", "eurobricks.com", "brickset.com", "flyertalk.com", "thehulltruth.com", "garagejournal.com", "chefsteps.com", "egullet.org",
                 "resetera.com", "neogaf.com", "askmetafilter.com", "metafilter.com", "reddit.com"]
SAFETY_DOMAINS = ["saferproducts.gov", "recalls.gov", "fda.gov", "nhtsa.gov", "usda.gov", "consumerreports.org", "classaction.org", "topclassactions.com"]
COMPLAINT_DOMAINS = ["consumeraffairs.com", "bbb.org", "trustpilot.com", "sitejabber.com", "complaintsboard.com", "pissedconsumer.com"]

def mk(pd):
    p = A.Pulse({"id": "0", "slug_words": "", "url": ""}, lambda e, d: None)
    p.product = {"brand": pd["brand"], "model": pd["model"], "name": pd["name"], "category": pd["category"], "aliases": [{"text": a, "support": None} for a in pd["aliases"]]}
    p.q, p.q_full = pd["q"], pd["q_full"]
    p.cat_tokens = [t for t in A.tokens(pd["category"]) if t not in A.tokens(pd["brand"])]
    p.name_tokens = [t for t in A.tokens(pd["name"]) if t not in A.tokens(pd["brand"])][:8]
    if not pd["mil"]:
        p.product["model"] = ""   # part numbers that never appear in listings are not usable keys
    return p

def rel(p, r):
    s = A.parse_summary(r.get("summary"))
    blob = " ".join([r.get("title", "") or "", " ".join(r.get("highlights") or []), r.get("url", "") or "", str(s.get("quote") or "")])
    if p.other_model((r.get("title") or "") + " " + (r.get("url") or "")):
        return False
    return p.relevant(blob)

def dated(r, days):
    d = A.parse_date(r.get("publishedDate"))
    return bool(d) and (A.now_utc() - d).days <= days

HL = {"highlights": {"maxCharacters": 200, "numSentences": 2}}
PRICE_SCHEMA = A.PRICE_EVENT_SCHEMA
def price_contents(p):
    return {"summary": {"query": f"Does this page report a specific price for the exact {p.q_full} at a specific merchant? Extract merchant, price, previous price if a drop is described, the date observed/published, event kind, condition.", "schema": PRICE_SCHEMA}}
def price_obs(p, results):
    n = 0
    for r in results:
        s = A.parse_summary(r.get("summary"))
        if not s.get("exact_product") or not A.to_float(s.get("price_usd")) or not norm_merchant(s.get("merchant")):
            continue
        if not rel(p, r):
            continue
        d = A.parse_date(s.get("observed_date") or "") or A.parse_date(r.get("publishedDate"))
        if d:
            n += 1
    return n
def norm_merchant(m):
    return A.norm(m or "")

async def run_product(key, pd, exa, out):
    p = mk(pd)
    D90, D365 = A.iso_days_ago(90), A.iso_days_ago(365)
    EX = A.SENTIMENT_EXCLUDE
    tests = []
    def T(label, coro_fn, score_fn):
        tests.append((label, coro_fn, score_fn))
    def sent_score(days):
        def f(res):
            rr = [r for r in res if rel(p, r)]
            return {"n": len(res), "rel": len(rr), "dated": sum(1 for r in rr if dated(r, days))}
        return f
    # ---- sentiment
    T("S1 forums neural 90d n20", lambda x: x.search("b", f"{p.q_full} owner experience review after using it", numResults=20, excludeDomains=EX, startPublishedDate=D90, contents=HL), sent_score(90))
    T("S2 forums neural 365d n20", lambda x: x.search("b", f"{p.q_full} owner experience review after using it", numResults=20, excludeDomains=EX, startPublishedDate=D365, contents=HL), sent_score(365))
    T("S3 forums neural 90d n50", lambda x: x.search("b", f"{p.q_full} owner experience review after using it", numResults=50, excludeDomains=EX, startPublishedDate=D90, contents=HL), sent_score(90))
    T("S4 forums keyword 365d n50", lambda x: x.search("b", f"{p.q} review", type="keyword", numResults=50, excludeDomains=EX, startPublishedDate=D365, contents=HL), sent_score(365))
    T("S5 forums deep 365d n25", lambda x: x.search("b", f"{p.q_full} owner reviews and complaints", type="deep", numResults=25, excludeDomains=EX, startPublishedDate=D365, contents=HL, timeout=90), sent_score(365))
    T("S5b forums deep-lite 365d n25", lambda x: x.search("b", f"{p.q_full} owner reviews and complaints", type="deep-lite", numResults=25, excludeDomains=EX, startPublishedDate=D365, contents=HL, timeout=90), sent_score(365))
    async def fanout(x):
        rs = await asyncio.gather(*[x.search("b", f"{a} review", numResults=20, excludeDomains=EX, startPublishedDate=D90, contents=HL) for a in pd["aliases"]])
        seen, out2 = set(), []
        for res in rs:
            for r in res:
                if r.get("url") not in seen:
                    seen.add(r.get("url")); out2.append(r)
        return out2
    T("S6 forums alias fan-out 90d 3x20", fanout, sent_score(90))
    T("S7 youtube neural 365d n30", lambda x: x.search("b", f"{p.q_full} review", numResults=30, includeDomains=["youtube.com"], startPublishedDate=D365, contents=HL), sent_score(365))
    T("S7b youtube neural 90d n15 (current)", lambda x: x.search("b", f"{p.q_full} review", numResults=15, includeDomains=["youtube.com"], startPublishedDate=D90, contents=HL), sent_score(90))
    T("S8 youtube keyword n50", lambda x: x.search("b", f"{p.q}", type="keyword", numResults=50, includeDomains=["youtube.com"], contents=HL), sent_score(3650))
    T("S9 tiktok keyword n30", lambda x: x.search("b", f"{p.q}", type="keyword", numResults=30, includeDomains=["tiktok.com"], contents=HL), sent_score(3650))
    T("S9b tiktok neural n12 (current)", lambda x: x.search("b", f"{p.q}", numResults=12, includeDomains=["tiktok.com"], contents=HL), sent_score(3650))
    T("S10 news neural 365d n30", lambda x: x.search("b", f"{p.q_full}", numResults=30, category="news", startPublishedDate=D365, contents=HL), sent_score(365))
    T("S10b news neural 90d n15 (current)", lambda x: x.search("b", f"{p.q_full}", numResults=15, category="news", startPublishedDate=D90, contents=HL), sent_score(90))
    T("S11 news keyword 365d n30", lambda x: x.search("b", f"{p.q}", type="keyword", numResults=30, category="news", startPublishedDate=D365, contents=HL), sent_score(365))
    T("S14 forum domains n30", lambda x: x.search("b", f"{p.q_full} opinion", numResults=30, includeDomains=FORUM_DOMAINS, contents=HL), sent_score(3650))
    T("S14b forum domains keyword n30", lambda x: x.search("b", f"{p.q}", type="keyword", numResults=30, includeDomains=FORUM_DOMAINS, contents=HL), sent_score(3650))
    async def retail_pages(x):
        res = await x.search("b", f"{p.q} customer reviews", numResults=15, includeDomains=A.RETAIL_REVIEW_DOMAINS,
                               contents={"summary": {"query": f"From the customer reviews on this page for the {p.q_full}: rating, review count, complaints customers voice, praises customers voice, one verbatim customer quote.", "schema": A.RETAIL_SCHEMA}})
        return res
    def retail_score(res):
        rr = [r for r in res if rel(p, r)]
        review_pages = sum(1 for r in rr if re.search(r"review", r.get("url", ""), re.I))
        items = 0
        for r in rr:
            s = A.parse_summary(r.get("summary"))
            items += len([c for c in (s.get("complaints") or []) if isinstance(c, str)]) + len([c for c in (s.get("praises") or []) if isinstance(c, str)])
        return {"n": len(res), "rel": len(rr), "review_pages": review_pages, "extracted_items": items}
    T("S12 retail review pages n15 + schema", retail_pages, retail_score)
    async def subpages(x):
        return await x.search("b", f"{p.q}", numResults=3, includeDomains=["amazon.com"], subpages=2, subpageTarget=["reviews", "customer reviews"], contents={"text": {"maxCharacters": 200}})
    def sub_score(res):
        sp = sum(len(r.get("subpages") or []) for r in res)
        urls = [s.get("url", "")[:60] for r in res for s in (r.get("subpages") or [])][:3]
        return {"n": len(res), "subpages": sp, "sample": urls}
    T("S13 amazon subpages target=reviews", subpages, sub_score)
    async def reddit_answer(x):
        d = await x.answer("b", f"What do Reddit users say about the {p.q_full}? Give up to 8 verbatim quotes from Reddit users (or from pages quoting Reddit threads), each with its sentiment and the URL of the page it appears on.",
                             {"type": "object", "properties": {"quotes": {"type": "array", "items": {"type": "object", "properties": {"text": {"type": "string"}, "sentiment": {"type": "string", "enum": ["positive", "negative", "mixed"]}, "url": {"type": "string"}}, "required": ["text", "sentiment", "url"]}}}, "required": ["quotes"]})
        return d
    def ra_score(d):
        qs = ((d.get("answer") or {}) if isinstance(d.get("answer"), dict) else {}).get("quotes") or []
        return {"quotes": len(qs), "sample": [q.get("text", "")[:70] for q in qs[:2]], "cites": len(d.get("citations") or [])}
    T("S15 reddit via /answer schema", reddit_answer, ra_score)
    # ---- price
    def pscore(res):
        return {"n": len(res), "rel": sum(1 for r in res if rel(p, r)), "obs": price_obs(p, res)}
    T("P1 events neural 90d n20 (current)", lambda x: x.search("b", f"{p.q} price drop deal sale", numResults=20, excludeDomains=["walmart.com"], startPublishedDate=D90, contents=price_contents(p)), pscore)
    T("P2 events neural 365d n30", lambda x: x.search("b", f"{p.q} price drop deal sale", numResults=30, excludeDomains=["walmart.com"], startPublishedDate=D365, contents=price_contents(p)), pscore)
    T("P3 deal domains 365d n30", lambda x: x.search("b", f"{p.q} deal", numResults=30, includeDomains=DEAL_DOMAINS, startPublishedDate=D365, contents=price_contents(p)), pscore)
    T("P4 news category deals 365d n30", lambda x: x.search("b", f"{p.q} deal sale price", numResults=30, category="news", startPublishedDate=D365, contents=price_contents(p)), pscore)
    T("P5 keyword deal 365d n30", lambda x: x.search("b", f"{p.q} deal", type="keyword", numResults=30, excludeDomains=["walmart.com"], startPublishedDate=D365, contents=price_contents(p)), pscore)
    async def variants(x):
        qs = [f"{p.q} deal", f"{p.q} sale", f"{p.q} lowest price", f"{p.q} price drop", f"{p.q} Prime Day", f"{p.q} Black Friday"]
        rs = await asyncio.gather(*[x.search("b", qq, numResults=15, excludeDomains=["walmart.com"], startPublishedDate=D365, contents=price_contents(p)) for qq in qs])
        seen, out2 = set(), []
        for res in rs:
            for r in res:
                if r.get("url") not in seen:
                    seen.add(r.get("url")); out2.append(r)
        return out2
    T("P6 6 query variants 365d 6x15", variants, pscore)
    async def trackers(x):
        return await x.search("b", f"{p.q} price history", numResults=10, includeDomains=TRACKER_DOMAINS,
                                contents={"summary": {"query": f"Price history for the exact {p.q_full}: current, lowest ever, highest ever, average, and any dated price changes with dates.", "schema": A.CAMEL_SCHEMA}})
    def tscore(res):
        rr = [r for r in res if rel(p, r)]
        stats = 0
        for r in rr:
            s = A.parse_summary(r.get("summary"))
            if s.get("exact_product") and any(A.to_float(s.get(k)) for k in ("current", "lowest", "highest", "average")):
                stats += 1
        return {"n": len(res), "rel": len(rr), "pages_with_stats": stats, "hosts": sorted({A.host_of(r.get('url','')) for r in rr})[:6]}
    T("P7 tracker domains n10", trackers, tscore)
    T("P8 deep price 365d n25", lambda x: x.search("b", f"{p.q} price history recent deals and discounts", type="deep", numResults=25, excludeDomains=["walmart.com"], startPublishedDate=D365, contents=price_contents(p), timeout=90), pscore)
    async def answer_prices(x):
        d = await x.answer("b", f"List dated price observations for the exact {p.q_full} ({p.q}) at named merchants over the last 12 months: for each give merchant, price in USD, the date, and the source URL. Include sales, price drops and current prices.",
                             {"type": "object", "properties": {"observations": {"type": "array", "items": {"type": "object", "properties": {"merchant": {"type": "string"}, "price_usd": {"type": "number"}, "date": {"type": "string"}, "url": {"type": "string"}}, "required": ["merchant", "price_usd", "date", "url"]}}}, "required": ["observations"]})
        return d
    def ascore(d):
        obs = ((d.get("answer") or {}) if isinstance(d.get("answer"), dict) else {}).get("observations") or []
        good = [o for o in obs if A.to_float(o.get("price_usd")) and A.parse_date(o.get("date") or "")]
        return {"obs": len(obs), "dated": len(good), "sample": [(o.get("merchant"), o.get("price_usd"), o.get("date")) for o in good[:3]]}
    T("P9 /answer dated observations", answer_prices, ascore)
    # ---- safety
    def bscore(res):
        rr = [r for r in res if A.contains_word((r.get("title") or "") + " " + " ".join(r.get("highlights") or []), p.product["brand"]) or (len(p.product["brand"]) >= 5 and p.product["brand"].lower() in ((r.get("title") or "") + " " + " ".join(r.get("highlights") or [])).lower())]
        return {"n": len(res), "brand_hits": len(rr), "model_hits": sum(1 for r in rr if rel(p, r)), "sample": [r.get("title", "")[:60] for r in rr[:2]]}
    T("R2 saferproducts.gov n20", lambda x: x.search("b", f"{p.product['brand']} {p.product['category']}", numResults=20, includeDomains=["saferproducts.gov"], contents=HL), bscore)
    T("R3 news safety 365d n20", lambda x: x.search("b", f"{p.product['brand']} {p.product['category']} recall fire injury lawsuit safety", numResults=20, category="news", startPublishedDate=D365, contents=HL), bscore)
    T("R4 fda/nhtsa/recalls.gov n10", lambda x: x.search("b", f"{p.product['brand']} {p.product['category']} recall", numResults=10, includeDomains=SAFETY_DOMAINS, contents=HL), bscore)
    T("R5 complaint sites n20", lambda x: x.search("b", f"{p.product['brand']} {p.product['category']} complaints", numResults=20, includeDomains=COMPLAINT_DOMAINS, contents=HL), bscore)
    T("R6 cpsc 5y brand n20", lambda x: x.search("b", f"{p.product['brand']} recalls product due to hazard", numResults=20, includeDomains=["cpsc.gov"], startPublishedDate=A.iso_days_ago(1825), contents=HL), bscore)
    # run all
    class Tagged:
        def __init__(self, tag): self.tag = tag
        async def search(self, _t, *a, **k): return await exa.search(self.tag, *a, **k)
        async def answer(self, _t, *a, **k): return await exa.answer(self.tag, *a, **k)
    async def one(label, fn, score):
        t0 = time.time(); tag = f"{key}|{label}"
        try:
            res = await fn(Tagged(tag))
            sc = score(res)
        except Exception as e:
            import traceback; traceback.print_exc()
            sc = {"error": f"{type(e).__name__}: {str(e)[:80]}"}
        cost = sum(c["cost"] for c in exa.calls if c["tag"] == tag)
        out.append({"product": key, "test": label, "ms": int((time.time() - t0) * 1000), "cost": round(cost, 4), **sc})
    await asyncio.gather(*[one(l, f, s) for l, f, s in tests])

async def main():
    exa = A.Exa()
    out = []
    keys = sys.argv[1:] or list(PRODUCTS)
    await asyncio.gather(*[run_product(k, PRODUCTS[k], exa, out) for k in keys])
    await exa.close()
    out.sort(key=lambda x: (x["test"], x["product"]))
    json.dump(out, open("/home/ubuntu/.claude/jobs/ac183c6f/tmp/bench.json", "w"), indent=1)
    cur = None
    for x in out:
        if x["test"] != cur:
            cur = x["test"]; print(f"\n== {cur}")
        extra = {k: v for k, v in x.items() if k not in ("product", "test", "ms", "cost")}
        print(f"  {x['product']:8} {x['ms']:6}ms ${x['cost']:.3f}  {json.dumps(extra)[:170]}")
    print(f"\nTOTAL cost ${exa.cost:.2f}, calls {len(exa.calls)}")
asyncio.run(main())
