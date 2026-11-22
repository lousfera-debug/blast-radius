"""pytest plugin that records, per test, what of the sqlglot checkout it touches.

Loaded with ``-p tracer`` (with this folder on PYTHONPATH). Writes one JSON file per
process into $BLAST_OUT. Recorded per test node id:

* lines  - (file, line) executed inside function code (not module/class bodies)
* types  - sqlglot classes whose ``__init__`` ran with ``self`` of that class
* opens  - repository files opened

Coverage of a test's setup phase and of each test module's collection is kept apart so
the merge step can share class-level and module-level work across the tests it serves.
"""

from __future__ import annotations

import importlib
import json
import os
import pkgutil
import sys
from pathlib import Path

ROOT = Path(os.environ.get("BLAST_ROOT", os.getcwd())).resolve()
OUT = Path(os.environ.get("BLAST_OUT", ROOT / ".blastmap"))
ROOT_S = str(ROOT).replace("\\", "/").rstrip("/") + "/"

mon = sys.monitoring
TOOL = 4
CO_OPTIMIZED = 0x1

_rel: dict[str, str | None] = {}
cur_lines: set = set()
cur_mod_lines: set = set()
cur_types: set = set()
cur_opens: set = set()
active = False


def rel(path: str) -> str | None:
    r = _rel.get(path, 0)
    if r != 0:
        return r
    p = path.replace("\\", "/")
    if p.startswith(ROOT_S):
        r = p[len(ROOT_S):]
        if not (r.startswith("sqlglot/") or r.startswith("tests/") or r.startswith("sqlglotc/")):
            r = None
    else:
        # case-insensitive filesystems
        if p.lower().startswith(ROOT_S.lower()):
            r = p[len(ROOT_S):]
        else:
            r = None
    _rel[path] = r
    return r


def on_line(code, line):
    fn = code.co_filename
    r = _rel.get(fn, 0)
    if r == 0:
        r = rel(fn)
    if r is not None:
        if code.co_flags & CO_OPTIMIZED:
            cur_lines.add((r, line))
            if IMPORTING[0]:
                # which module/class-level statement made this function line run
                f = sys._getframe(1)
                while f is not None:
                    c = f.f_code
                    if not c.co_flags & CO_OPTIMIZED:
                        tr = rel(c.co_filename)
                        if tr is not None:
                            import_triggers.setdefault((r, line), set()).add((tr, f.f_lineno))
                            return None
                    f = f.f_back
                return None
            if COLLECTING[0]:
                o = owner_of(sys._getframe(1))
                if o is not None:
                    owner_data.setdefault(o, (set(), set(), set()))[0].add((r, line))
                return None
        else:
            cur_mod_lines.add((r, line))
    return mon.DISABLE


IMPORTING = [False]
COLLECTING = [False]
owner_data: dict = {}  # test module -> (lines, types, opens) its module-level code caused


def owner_of(f):
    """The test module whose module-level code is (indirectly) running."""
    while f is not None:
        c = f.f_code
        if not c.co_flags & CO_OPTIMIZED:
            r = rel(c.co_filename)
            if r is not None and r.startswith("tests/"):
                return r
        f = f.f_back
    return None
import_triggers: dict = {}


def on_start(code, offset):
    f = sys._getframe(1)
    try:
        s = f.f_locals["self"]
    except Exception:
        return
    cur_types.add(type(s))
    if COLLECTING[0]:
        o = owner_of(f)
        if o is not None:
            owner_data.setdefault(o, (set(), set(), set()))[1].add(type(s))


def audit(event, args):
    if event == "open" and active:
        p = args[0]
        if isinstance(p, (str, os.PathLike)):
            p = os.fspath(p)
            if isinstance(p, bytes):
                return
            r = rel(os.path.abspath(p)) if not os.path.isabs(p) else rel(p)
            if r is not None:
                cur_opens.add(r)
                if COLLECTING[0]:
                    o = owner_of(sys._getframe(1))
                    if o is not None:
                        owner_data.setdefault(o, (set(), set(), set()))[2].add(r)


def take():
    global cur_lines, cur_mod_lines, cur_types, cur_opens
    d = (cur_lines, cur_mod_lines, cur_types, cur_opens)
    cur_lines, cur_mod_lines, cur_types, cur_opens = set(), set(), set(), set()
    mon.restart_events()
    return d


def classkey(c: type) -> str:
    return f"{c.__module__}:{c.__qualname__}"


all_types: set = set()
records: dict = {}
collect: dict = {}
import_phase = None


def _iter_classes(obj, seen):
    for v in list(vars(obj).values()):
        if isinstance(v, type) and v not in seen and getattr(v, "__module__", "").startswith("sqlglot"):
            seen.add(v)
            yield v
            yield from _iter_classes(v, seen)


CURRENT = ["<startup>"]
_child_ready = False
_child_types_seen: set = set()


class _Traced:
    """Wraps work sent to a process pool so the child records it for the submitting test."""

    def __init__(self, fn, nodeid, payload=None):
        self.fn, self.nodeid, self.payload = fn, nodeid, payload

    def __call__(self, *a, **k):
        global _child_ready
        import pickle

        if not _child_ready:
            _child_ready = True
            _start_monitoring()
        take()
        try:
            if self.payload is not None:
                a, k = pickle.loads(self.payload)
            res = self.fn(*a, **k)
            # results go back pickled (sqlglot's serde): do it while recording
            return _Pickled(pickle.dumps(res))
        finally:
            d = take()
            enc = _enc(d)
            new = [t for t in d[2] if t not in _child_types_seen]
            _child_types_seen.update(new)
            enc["mro"] = {
                classkey(t): [classkey(b) for b in t.__mro__ if b.__module__.startswith("sqlglot")] for t in new
            }
            enc["t"] = self.nodeid
            _child_write(json.dumps(enc) + "\n")


_child_fh = []


class _Pickled:
    """A pickled result that unpickles into the original object in the parent."""

    def __init__(self, data):
        self.data = data

    def __reduce__(self):
        import pickle

        return (pickle.loads, (self.data,))


def _child_write(s):
    import time

    for attempt in range(50):
        try:
            if not _child_fh:
                OUT.mkdir(parents=True, exist_ok=True)
                _child_fh.append(open(OUT / f"child-{os.getpid()}-{attempt}.jsonl", "a"))
            _child_fh[0].write(s)
            _child_fh[0].flush()
            return
        except OSError:
            _child_fh.clear()
            time.sleep(0.05)
    print("tracer: could not write child record", file=sys.stderr)


def _patch_pools():
    import concurrent.futures.process as cfp

    orig_submit = cfp.ProcessPoolExecutor.submit
    orig_init = cfp.ProcessPoolExecutor.__init__

    def submit(self, fn, /, *args, **kwargs):
        import pickle

        return orig_submit(self, _Traced(fn, CURRENT[0], pickle.dumps((args, kwargs))))

    def init(self, *args, **kwargs):
        if kwargs.get("initializer") is not None:
            kwargs["initializer"] = _Traced(kwargs["initializer"], CURRENT[0])
        return orig_init(self, *args, **kwargs)

    cfp.ProcessPoolExecutor.submit = submit
    cfp.ProcessPoolExecutor.__init__ = init


def _start_monitoring():
    global active
    mon.use_tool_id(TOOL, "blastmap")
    mon.register_callback(TOOL, mon.events.LINE, on_line)
    mon.register_callback(TOOL, mon.events.PY_START, on_start)
    sys.addaudithook(audit)
    active = True
    mon.set_events(TOOL, mon.events.LINE)
    IMPORTING[0] = True
    import sqlglot

    mods = []
    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if m.name == "sqlglot.__main__":
            continue
        try:
            mods.append(importlib.import_module(m.name))
        except Exception as e:  # optional deps
            print(f"tracer: skip {m.name}: {e}", file=sys.stderr)
    seen: set = set()
    for mod in [sqlglot, *mods]:
        for c in _iter_classes(mod, seen):
            init = c.__dict__.get("__init__")
            code = getattr(init, "__code__", None)
            if code is not None and rel(code.co_filename):
                mon.set_local_events(TOOL, code, mon.events.PY_START)
    IMPORTING[0] = False


def pytest_configure(config):
    global import_phase
    _patch_pools()
    _start_monitoring()
    import_phase = take()


def pytest_collection(session):
    COLLECTING[0] = True


def pytest_collection_finish(session):
    COLLECTING[0] = False


def pytest_collectstart(collector):
    if collector.__class__.__name__ == "Module":
        CURRENT[0] = collector.nodeid
        take()


def pytest_collectreport(report):
    if report.nodeid.endswith(".py"):
        collect[report.nodeid] = take()


def _store(nodeid, phase, d):
    records.setdefault(nodeid, {})[phase] = d


import pytest  # noqa: E402


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    CURRENT[0] = item.nodeid
    take()
    yield
    _store(item.nodeid, "setup", take())


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    take()
    yield
    _store(item.nodeid, "call", take())


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    take()
    yield
    _store(item.nodeid, "teardown", take())


def _enc(d):
    lines, mlines, types, opens = d
    for t in types:
        all_types.add(t)
    byfile: dict = {}
    for f, l in lines:
        byfile.setdefault(f, []).append(l)
    mbyfile: dict = {}
    for f, l in mlines:
        mbyfile.setdefault(f, []).append(l)
    return {
        "lines": {f: sorted(v) for f, v in byfile.items()},
        "mlines": {f: sorted(v) for f, v in mbyfile.items()},
        "types": sorted(classkey(t) for t in types),
        "opens": sorted(opens),
    }


def pytest_unconfigure(config):
    global active
    active = False
    mon.set_events(TOOL, 0)
    out_owner = {k: _enc((v[0], set(), v[1], v[2])) for k, v in owner_data.items()}
    out = {
        "owners": out_owner,
        "import": _enc(import_phase) if import_phase else None,
        "collect": {k: _enc(v) for k, v in collect.items()},
        "tests": {k: {ph: _enc(d) for ph, d in v.items()} for k, v in records.items()},
    }
    trig: dict = {}
    for (f, l), ts in import_triggers.items():
        trig.setdefault(f, {})[str(l)] = sorted([list(t) for t in ts])
    out["triggers"] = trig
    out["mro"] = {
        classkey(t): [classkey(b) for b in t.__mro__ if b.__module__.startswith("sqlglot")]
        for t in all_types
    }
    OUT.mkdir(parents=True, exist_ok=True)
    wid = os.environ.get("PYTEST_XDIST_WORKER", "main")
    (OUT / f"trace-{wid}-{os.getpid()}.json").write_text(json.dumps(out))
