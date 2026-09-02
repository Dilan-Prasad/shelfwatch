"""Convert a raw Exa Agent run JSON (Affiliate.com offers) into the app's cache/<id>.deep.json format."""
import json, sys, re
sys.path.insert(0, ".")
import app as A
run = json.load(open(sys.argv[1])); item_id = sys.argv[2]
offers, walmart, walmart_offers, seen = [], None, [], set()
for o in ((run.get("output") or {}).get("structured") or {}).get("offers", []) or []:
    price = A.to_float(o.get("price_usd")); url = o.get("url") or ""; host = A.host_of(url)
    if not price or not host:
        continue
    cond = (o.get("condition") or "new").lower()
    if cond not in ("new", "unknown", ""):
        continue
    merchant = A.BIG_BOX.get(host) or A.norm(o.get("merchant") or host)[:40]
    entry = {"merchant": merchant, "price_usd": price, "list_price_usd": A.to_float(o.get("list_price_usd")), "variant": A.norm(o.get("variant") or "")[:80] or None,
             "in_stock": o.get("in_stock"), "url": url, "condition": cond or "new", "long_tail": host not in A.BIG_BOX}
    if host.endswith("walmart.com"):
        w = {"price": price, "url": url, "merchant": A.norm(o.get("merchant") or "Walmart")[:40], "variant": entry["variant"], "list_price_usd": entry["list_price_usd"], "in_stock": o.get("in_stock")}
        walmart_offers.append(w)
        if not walmart or price < walmart["price"]:
            walmart = w
        continue
    k = merchant.lower()
    if k in seen:
        continue
    seen.add(k); offers.append(entry)
res = {"status": "done", "offers": sorted(offers, key=lambda o: o["price_usd"]), "walmart": walmart, "walmart_offers": walmart_offers,
       "cost": (run.get("costDollars") or {}).get("total"), "as_of": A.now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"), "elapsed": None}
json.dump(res, open(f"cache/{item_id}.deep.json", "w"))
print(json.dumps(res, indent=1)[:3000])
