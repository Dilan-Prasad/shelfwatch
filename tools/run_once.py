import asyncio, json, sys, time, os
sys.path.insert(0, ".")
import app as A

async def main(url, out):
    parsed = A.parse_walmart(url)
    print("parsed:", parsed)
    t0 = time.time()
    async def emit(event, data):
        if event == "call":
            print(f"  call {data.get('endpoint')} {data.get('tag')} {data.get('ms')}ms ${data.get('cost')} {data.get('error','')}")
        elif event == "surface":
            print(f"  surface {data['key']}: {data['status']} n={data['n']} {data.get('note','')}")
        else:
            print(f"  EVENT {event}: {json.dumps(data)[:300]}")
    p = A.Pulse(parsed, emit)
    rep = await p.run()
    print(f"done in {time.time()-t0:.1f}s, cost ${rep['cost']['dollars']}, calls {rep['cost']['calls']}")
    json.dump(rep, open(out, "w"), indent=1, ensure_ascii=False)
    if os.environ.get("PULSE_DEBUG"):
        json.dump(p.exa.raw, open(out.replace(".json", ".raw.json"), "w"), ensure_ascii=False)
    print("saved", out)

asyncio.run(main(sys.argv[1], sys.argv[2]))
