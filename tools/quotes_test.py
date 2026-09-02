import json, os, re, httpx, concurrent.futures as cf
KEY = os.environ["EXA_API_KEY"]
PRODUCTS = {
  "dyson": {"phrase": "Dyson V8 Cyclone", "exact": r"v8 cyclone", "sibling": r"\b(origin|absolute|animal|extra|plus|motorhead|titanium|detect|v7|v10|v11|v12|v15|gen5|ball)\b"},
  "ninja": {"phrase": "Ninja AF101", "exact": r"\baf101\b", "sibling": r"\b(af100|af141|af150|af161|af300|dz\d+|foodi|crispi|af101c)\b"},
  "lego":  {"phrase": "LEGO 75440", "exact": r"\b75440\b", "sibling": r"\b(75\d{3}|76\d{3}|10\d{3}|21\d{3}|42\d{3})\b"},
}
RETAIL = ["amazon.com", "target.com", "bestbuy.com", "homedepot.com", "dyson.com", "lego.com", "kohls.com", "macys.com", "staples.com", "qvc.com"]
def search(q, **kw):
    body = {"query": q, "numResults": 20, "type": "auto", "contents": {"highlights": {"maxCharacters": 200, "numSentences": 1}}}
    body.update(kw)
    r = httpx.post("https://api.exa.ai/search", headers={"x-api-key": KEY}, json=body, timeout=90)
    d = r.json()
    return d.get("results", []) or [], (d.get("costDollars") or {}).get("total")
def score(pid, res):
    P = PRODUCTS[pid]; ex = sib = 0; ex_title = 0
    for r in res:
        title = (r.get("title") or "").lower(); hl = " ".join(r.get("highlights") or []).lower(); url = (r.get("url") or "").lower()
        blob = title + " " + hl + " " + url
        e = bool(re.search(P["exact"], blob)); s = bool(re.search(P["sibling"], title)) and not re.search(P["exact"], title)
        ex += e; sib += s; ex_title += bool(re.search(P["exact"], title + " " + url))
    n = len(res)
    return f"n={n:2d}  exact(any)={ex:2d}  exact(title/url)={ex_title:2d}  sibling-titled={sib:2d}"
VARIANTS = [
  ("neural plain",        lambda p: (f"{p} review", {})),
  ("neural quoted",       lambda p: (f'"{p}" review', {})),
  ("neural quoted-only",  lambda p: (f'"{p}"', {})),
  ("keyword plain",       lambda p: (f"{p} review", {"type": "keyword"})),
  ("keyword quoted",      lambda p: (f'"{p}" review', {"type": "keyword"})),
  ("fast quoted",         lambda p: (f'"{p}"', {"type": "fast"})),
  ("retail plain",        lambda p: (f"{p} buy", {"includeDomains": RETAIL})),
  ("retail quoted",       lambda p: (f'"{p}" buy', {"includeDomains": RETAIL})),
  ("retail kw quoted",    lambda p: (f'"{p}"', {"type": "keyword", "includeDomains": RETAIL})),
  ("walmart plain",       lambda p: (f"{p}", {"includeDomains": ["walmart.com"]})),
  ("walmart quoted",      lambda p: (f'"{p}"', {"includeDomains": ["walmart.com"]})),
  ("walmart kw quoted",   lambda p: (f'"{p}"', {"type": "keyword", "includeDomains": ["walmart.com"]})),
]
jobs = [(pid, name, fn) for pid in PRODUCTS for name, fn in VARIANTS]
def run(job):
    pid, name, fn = job; q, kw = fn(PRODUCTS[pid]["phrase"])
    try:
        res, cost = search(q, **kw)
        return pid, name, score(pid, res), cost, [ (r.get("title") or "")[:60] for r in res[:3] ]
    except Exception as e:
        return pid, name, f"ERROR {e}", 0, []
out = {}
with cf.ThreadPoolExecutor(6) as ex:
    for pid, name, sc, cost, samples in ex.map(run, jobs):
        out.setdefault(pid, []).append((name, sc, cost, samples))
total = 0
for pid, rows in out.items():
    print(f"\n=== {pid}: phrase \"{PRODUCTS[pid]['phrase']}\"")
    for name, sc, cost, samples in sorted(rows, key=lambda x: [v[0] for v in VARIANTS].index(x[0])):
        total += cost or 0
        print(f"  {name:20} {sc}   ${cost}")
        for s in samples: print(f"      · {s}")
print(f"\nTOTAL ${total:.2f}")
