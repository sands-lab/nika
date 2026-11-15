"""Per-trial time outside the agent from NIKA trial directories (native or Firecracker).

prep_s: native = run.json created_at -> agent_start (deploy, verify, inject);
        firecracker = image lookup + restore-to-daemon-ready + attach + rebind -> agent_start.
teardown_s: agent_end -> last host event.
gap_s: last host event of the previous trial (trials run one at a time) -> this trial's
       last host event, minus agent time: everything outside the agent incl. runner overhead.
"""

import glob
import json
import statistics
import sys
from datetime import datetime


def f(s):
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.astimezone()


rows = []
for t in glob.glob(f"{sys.argv[1]}/trials/*/"):
    try:
        r = json.load(open(t + "run.json"))
        ev, last = {}, None
        for line in open(t + "nika.jsonl"):
            e = json.loads(line)
            if e.get("origin") == "remote":
                continue  # lab clock
            ev.setdefault(e.get("event"), f(e["timestamp"]))
            last = f(e["timestamp"])
        fc = r.get("firecracker")
        agent = (ev["agent_end"] - ev["agent_start"]).total_seconds()
        if fc:
            prep = (
                fc["image_sec"]
                + fc["ready_sec"]
                + fc.get("attach_sec", 0)
                + (ev["agent_start"] - ev["remote_session_rebind"]).total_seconds()
            )
        else:
            prep = (ev["agent_start"] - f(r["created_at"])).total_seconds()
        p = r.get("scenario_params") or {}
        rows.append(
            {
                "trial": t.rstrip("/").split("/")[-1],
                "scenario": r.get("scenario_name"),
                "size": r.get("scenario_topo_size") or p.get("topo_size"),
                "end": last,
                "agent_s": agent,
                "prep_s": round(prep, 1),
                "teardown_s": round((last - ev["agent_end"]).total_seconds(), 1),
            }
        )
    except Exception as exc:
        print("skip", t, repr(exc))
rows.sort(key=lambda x: x["end"])
for prev, x in zip([None] + rows[:-1], rows):
    x["gap_s"] = (
        round((x["end"] - prev["end"]).total_seconds() - x["agent_s"], 1)
        if prev
        else None
    )
    x["outside_s"] = round(x["prep_s"] + x["teardown_s"], 1)
if "-v" in sys.argv:
    for x in rows:
        print({k: v for k, v in x.items() if k != "end"})
for key in sorted({(x["scenario"], x["size"]) for x in rows}, key=str):
    o = [x["outside_s"] for x in rows if (x["scenario"], x["size"]) == key]
    g = [
        x["gap_s"]
        for x in rows
        if (x["scenario"], x["size"]) == key and x["gap_s"] is not None
    ]
    print(
        key,
        f"n={len(o)} outside median={statistics.median(o):.1f}s [{min(o)}-{max(o)}]",
        f"gap median={statistics.median(g):.1f}s [{min(g)}-{max(g)}]" if g else "",
    )
