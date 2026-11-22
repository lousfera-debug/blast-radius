"""Run every test in tests.json in its own pytest process with the tracer loaded.

usage: python trace_isolated.py <sqlglot checkout> <tests.json> <out dir> [jobs]

Fresh processes make each test's record include the lazily built caches it relies
on, which an in-process run credits only to the first test that built them.
"""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

root, tests_json, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2]), Path(sys.argv[3]).resolve()
jobs = int(sys.argv[4]) if len(sys.argv) > 4 else os.cpu_count()
ids = [t["id"] for t in json.loads(tests_json.read_text())["tests"]]
here = Path(__file__).resolve().parent
out.mkdir(parents=True, exist_ok=True)
done = {p.stem for p in out.glob("*.ok")}


def run(i_id):
    i, tid = i_id
    if str(i) in done:
        return 0
    d = out / str(i)
    env = dict(os.environ, SKIP_INTEGRATION="0", BLAST_ROOT=str(root), BLAST_OUT=str(d),
               PYTHONPATH=os.pathsep.join([str(root), str(here)]))
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "tracer",
                        "-p", "no:xdist", tid], cwd=root, env=env, capture_output=True, text=True)
    if r.returncode not in (0,):
        print(f"[{i}] {tid} rc={r.returncode}\n{r.stdout[-800:]}", file=sys.stderr, flush=True)
    (out / f"{i}.ok").write_text(str(r.returncode))
    return r.returncode


with ThreadPoolExecutor(jobs) as ex:
    for n, rc in enumerate(ex.map(run, enumerate(ids))):
        if n % 100 == 0:
            print(f"{n}/{len(ids)}", flush=True)
print("done")
