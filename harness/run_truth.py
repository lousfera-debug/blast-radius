"""Label synthetic commits by running the whole suite on each.

usage: python run_truth.py <sqlglot checkout> <tests.json> <mutant dir> <jobs>
Writes <mutant dir>/answers.json in the sample's format (incrementally, resumable).
"""

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue

root, tests_json, mdir, jobs = Path(sys.argv[1]).resolve(), Path(sys.argv[2]), Path(sys.argv[3]).resolve(), int(sys.argv[4])
tests = json.loads(tests_json.read_text())["tests"]
by_id = {t["id"]: t["number"] for t in tests}
here = Path(__file__).resolve().parent
kinds = json.loads((mdir / "kinds.json").read_text())
ans_path = mdir / "answers.json"
answers = json.loads(ans_path.read_text())["commits"] if ans_path.exists() else {}
lock = threading.Lock()

# Optional: skip the slowest tests when the map says they never touch the changed files.
EXPENSIVE = [t for t in tests if t["seconds"] > 2.0]
coarse = None
if os.environ.get("TRUTH_MAP"):
    import lzma
    import re

    mp = json.loads(lzma.decompress(Path(os.environ["TRUTH_MAP"]).read_bytes()))
    sets = [int(x, 16) for x in mp["sets"]]
    idx = {tid: i for i, tid in enumerate(mp["ids"])}

    def coarse(patch_text):
        files = re.findall(r"^diff --git a/(\S+)", patch_text, re.M)
        m = 0
        for f in files:
            for s in mp["lines"].get(f, {}).values():
                m |= sets[s]
            if f in mp["opens"]:
                m |= sets[mp["opens"][f]]
            mod = f[:-3].replace("/", ".") + ":"
            for ck, s in mp["types"].items():
                if ck.startswith(mod):
                    m |= sets[s]
            for ck, bases in mp["mro"].items():
                if any(b.startswith(mod) for b in bases) and ck in mp["types"]:
                    m |= sets[mp["types"][ck]]
            if not f.startswith(("sqlglot/", "tests/")):
                m = -1
        return m

wts = Queue()
for k in range(jobs):
    wt = root.parent / "wt" / f"{mdir.name}-{k}"
    if not wt.exists():
        subprocess.run(["git", "-C", str(root), "worktree", "add", "-q", "--detach", str(wt), "HEAD"], check=True)
    wts.put(wt)


def label(cid):
    wt = wts.get()
    try:
        subprocess.run(["git", "-C", str(wt), "checkout", "-q", "--", "."], check=True)
        subprocess.run(["git", "-C", str(wt), "clean", "-fdq"], check=True)
        r = subprocess.run(["git", "-C", str(wt), "apply", str(mdir / f"{cid}.patch")], capture_output=True, text=True)
        if r.returncode:
            print(f"{cid}: apply failed {r.stderr}", file=sys.stderr)
            return
        fout = wt / ".fails.txt"
        if fout.exists():
            fout.unlink()
        env = dict(os.environ, SKIP_INTEGRATION="0", FAIL_OUT=str(fout),
                   PYTHONPATH=os.pathsep.join([str(wt), str(here)]))
        deselect = []
        if coarse is not None:
            cm = coarse((mdir / f"{cid}.patch").read_text())
            for t in EXPENSIVE:
                if cm != -1 and not (cm >> idx[t["id"]]) & 1:
                    deselect += ["--deselect", t["id"]]
        subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "failcollect",
                        "-p", "no:xdist", "--timeout", "600", *deselect, "tests"], cwd=wt, env=env,
                       capture_output=True, text=True)
        failing = set()
        if not fout.exists():
            print(f"{cid}: no result", file=sys.stderr)
            return
        for line in fout.read_text().splitlines():
            kind, nid = line.split(" ", 1)
            if kind == "T":
                base = nid.split("[")[0] if nid not in by_id else nid
                if base in by_id:
                    failing.add(by_id[base])
                else:
                    print(f"{cid}: unknown failing id {nid}", file=sys.stderr)
            else:
                failing |= {t["number"] for t in tests if t["file"] == nid or t["file"].startswith(nid.rstrip("/") + "/")}
        with lock:
            answers[cid] = {"failing": sorted(failing), "kind": kinds[cid]}
            for _ in range(100):
                try:
                    tmp = ans_path.with_suffix(".tmp")
                    tmp.write_text(json.dumps({"commits": answers}))
                    os.replace(tmp, ans_path)
                    break
                except OSError:
                    import time

                    time.sleep(0.2)
        print(f"{cid} {kinds[cid]} {len(failing)}", flush=True)
    finally:
        wts.put(wt)


todo = [c for c in kinds if c not in answers]
with ThreadPoolExecutor(jobs) as ex:
    list(ex.map(label, todo))
print("done", len(answers))
