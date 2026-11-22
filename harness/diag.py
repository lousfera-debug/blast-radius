"""Compare a selector output with answers: per commit, missed tests and cost.

usage: python diag.py <tests.json> <answers.json> <output.jsonl> [--all]
"""

import json
import sys

tests = {t["number"]: t for t in json.load(open(sys.argv[1]))["tests"]}
ans = json.load(open(sys.argv[2]))["commits"]
out = {}
for line in open(sys.argv[3]):
    d = json.loads(line)
    out[d["commit"]] = d["workers"]
show_all = "--all" in sys.argv
tot = 0
misses = 0
by_kind = {}
for c, a in sorted(ans.items()):
    ws = [w for w in out.get(c, []) if w]
    sel = {n for w in ws for n in w}
    cost = (max(sum(round(tests[n]["seconds"] * 1000) for n in w) for w in ws) + 2000 * len(ws)) if ws else 0
    missed = [n for n in a["failing"] if n not in sel]
    k = by_kind.setdefault(a["kind"], [0, 0, 0])
    k[0] += 1
    k[1] += cost / 1000
    if missed:
        misses += 1
        k[2] += 1
    tot += cost
    if missed or show_all:
        print(f"{c} {a['kind']} cost={cost/1000:.1f} sel={len(sel)} fail={len(a['failing'])} missed={len(missed)}")
        for n in missed[:6]:
            print("    ", tests[n]["id"])
print(f"cost {tot/1000:.1f}s misses {misses}/{len(ans)} total {tot/1000 + 300*misses:.1f}")
for k, v in by_kind.items():
    print(f"  {k}: n={v[0]} cost={v[1]:.1f} misses={v[2]}")
