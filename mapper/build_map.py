"""Merge tracer output into the compact map the selector reads.

usage: python build_map.py <sqlglot checkout> <tests.json> <out map> <trace dir>...

Every trace dir is searched recursively for trace-*.json files.
"""

import ast
import json
import lzma
import sys
from collections import defaultdict
from pathlib import Path

root, tests_json, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2]), Path(sys.argv[3])
trace_dirs = [Path(p) for p in sys.argv[4:]]

tests = json.loads(tests_json.read_text())["tests"]
num = {t["id"]: t["number"] for t in tests}
file_of = {t["number"]: t["file"] for t in tests}
cls_of = {t["number"]: t["id"].rsplit("::", 1)[0] for t in tests}

lines = defaultdict(lambda: defaultdict(int))  # file -> line -> mask
types = defaultdict(int)
opens = defaultdict(int)
imp_lines = defaultdict(set)  # lines executed while importing sqlglot (function or module code)
mro = {}
import_types = set()
triggers = defaultdict(dict)  # file -> line -> {(file, line)} statements that ran it at import

setup_by_class = defaultdict(list)
collect_by_file = defaultdict(list)
owner_by_file = defaultdict(list)
per_test = defaultdict(list)


def add(mask, d):
    for f, ls in d["lines"].items():
        fl = lines[f]
        for l in ls:
            fl[l] |= mask
    for c in d["types"]:
        types[c] |= mask
    for o in d["opens"]:
        opens[o] |= mask


nfiles = 0
for td in trace_dirs:
    for p in td.rglob("trace-*.json"):
        nfiles += 1
        data = json.loads(p.read_text())
        mro.update(data.get("mro", {}))
        for f, d in data.get("triggers", {}).items():
            for l, ts in d.items():
                triggers[f].setdefault(int(l), set()).update(tuple(t) for t in ts)
        if data.get("import"):
            import_types.update(data["import"].get("types", []))
            for f, ls in data["import"]["lines"].items():
                imp_lines[f].update(ls)
            for f, ls in data["import"]["mlines"].items():
                imp_lines[f].update(ls)
        for mod, d in data["collect"].items():
            collect_by_file[mod].append(d)
        for own, d in data.get("owners", {}).items():
            owner_by_file[own].append(d)
        for nid, phases in data["tests"].items():
            if nid not in num:
                base = nid.split("[")[0]
                if base not in num:
                    continue
                nid = base
            n = num[nid]
            for ph, d in phases.items():
                per_test[n].append(d)
                if ph == "setup":
                    setup_by_class[cls_of[n]].append(d)
nchild = 0
for td in trace_dirs:
    for p in td.rglob("child-*.jsonl"):
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            nchild += 1
            mro.update(d.get("mro", {}))
            nid = d["t"]
            if nid.endswith(".py"):
                collect_by_file[nid].append(d)
                continue
            if nid not in num:
                nid = nid.split("[")[0]
                if nid not in num:
                    continue
            per_test[num[nid]].append(d)
print(f"read {nfiles} trace files, {nchild} child records, {len(per_test)} tests", file=sys.stderr)

missing = [t["id"] for t in tests if t["number"] not in per_test]
if missing:
    print(f"WARNING: {len(missing)} tests without trace, e.g. {missing[:3]}", file=sys.stderr)

files_tests = defaultdict(int)
class_tests = defaultdict(int)
for t in tests:
    files_tests[t["file"]] |= 1 << t["number"]
    class_tests[cls_of[t["number"]]] |= 1 << t["number"]

for n, ds in per_test.items():
    for d in ds:
        add(1 << n, d)
for c, ds in setup_by_class.items():
    for d in ds:
        add(class_tests[c], d)
for f, ds in collect_by_file.items():
    for d in ds:
        add(files_tests.get(f, 0), d)


# test modules each test file imports (transitively): their import-time work is its too
def _test_imports(rel):
    out = set()
    p = root / rel
    try:
        tree = ast.parse(p.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return out
    pkg = rel.rsplit("/", 1)[0]
    for n in ast.walk(tree):
        mods = []
        if isinstance(n, ast.ImportFrom):
            if n.level:
                base = pkg.split("/")
                base = base[: len(base) - (n.level - 1)] if n.level > 1 else base
                mods.append("/".join(base + ([n.module.replace(".", "/")] if n.module else [])))
                mods += ["/".join(base + ([n.module.replace(".", "/")] if n.module else []) + [a.name]) for a in n.names]
            elif n.module and n.module.split(".")[0] == "tests":
                mods.append(n.module.replace(".", "/"))
                mods += [n.module.replace(".", "/") + "/" + a.name for a in n.names]
        elif isinstance(n, ast.Import):
            mods += [a.name.replace(".", "/") for a in n.names if a.name.split(".")[0] == "tests"]
        for m in mods:
            for cand in (m + ".py", m + "/__init__.py"):
                if (root / cand).exists():
                    out.add(cand)
    return out


_deps = {}
for tf in {t["file"] for t in tests}:
    seen, todo = set(), [tf]
    while todo:
        x = todo.pop()
        if x in seen:
            continue
        seen.add(x)
        todo += list(_test_imports(x))
    _deps[tf] = seen
for tf, deps in _deps.items():
    for dep in deps:
        for d in owner_by_file.get(dep, ()):
            add(files_tests[tf], d)
print("test files importing other test modules:", sum(1 for v in _deps.values() if len(v) > 1), file=sys.stderr)

# Lazily filled caches: the test that fills one is just the first of the tests relying on
# it. Every line of a function that stores into a cache counts for every test that ran
# the function; the generator dispatch table counts for every test that built a generator.
import ast as _ast  # noqa: E402


def _span_mask(fl, node):
    m = 0
    for l in range(node.lineno, (node.end_lineno or node.lineno) + 1):
        m |= fl.get(l, 0)
    return m


def _stores_cache(fn):
    for n in _ast.walk(fn):
        if isinstance(n, _ast.Assign):
            for t in n.targets:
                if isinstance(t, _ast.Subscript) and "cache" in _ast.unparse(t.value).lower():
                    return True
    return False


smeared = 0
for p in sorted((root / "sqlglot").rglob("*.py")):
    rel = p.relative_to(root).as_posix()
    fl = lines.get(rel)
    if not fl:
        continue
    tree = _ast.parse(p.read_text(encoding="utf-8"))
    fns = [n for n in _ast.walk(tree) if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))]
    extra = {}
    for fn in fns:
        if _stores_cache(fn):
            m = _span_mask(fl, fn)
            for l in range(fn.lineno, fn.end_lineno + 1):
                if l in fl:
                    fl[l] |= m
            smeared += 1
            # helpers called to fill the cache
            for n in _ast.walk(fn):
                if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Name):
                    extra[n.func.id] = extra.get(n.func.id, 0) | m
    for fn in fns:
        if fn.name in extra:
            for l in range(fn.lineno, fn.end_lineno + 1):
                if l in fl:
                    fl[l] |= extra[fn.name]
print(f"smeared {smeared} cache-filling functions", file=sys.stderr)

# names referenced by executed function code -> tests
name_tests = defaultdict(int)
func_index = defaultdict(list)  # function name -> [(file, first, last)]


import_names = set()  # names read by function code that runs while sqlglot is imported
class_creation_names = set()
class_headers = set()
for _p in sorted((root / "sqlglot").rglob("*.py")):
    _rel = _p.relative_to(root).as_posix()
    for _n in ast.walk(ast.parse(_p.read_text(encoding="utf-8"))):
        if isinstance(_n, ast.ClassDef):
            _first = min([_n.lineno] + [d.lineno for d in _n.decorator_list])
            for _l in range(_first, _n.body[0].lineno):
                class_headers.add((_rel, _l))


def code_names(code):
    out = set(code.co_names)
    for c in code.co_consts:
        if isinstance(c, str) and c.isidentifier():
            out.add(c)
    return out


def walk(code, f, fl):
    ls = {l for _, _, l in code.co_lines() if l}
    if code.co_flags & 1 and ls:
        m = 0
        for l in ls:
            m |= fl.get(l, 0)
        nms = code_names(code)
        if f.startswith("sqlglot/") and ls & imp_lines.get(f, set()):
            import_names.update(nms)
            # run because a class statement executed (metaclass / __init_subclass__ work)
            trig = triggers.get(f, {})
            for l in ls:
                for tf, tl in trig.get(l, ()):
                    if (tf, tl) in class_headers:
                        class_creation_names.update(nms)
                        break
    for c in code.co_consts:
        if hasattr(c, "co_code"):
            walk(c, f, fl)


for p in sorted((root / "sqlglot").rglob("*.py")) + sorted((root / "tests").rglob("*.py")):
    rel = p.relative_to(root).as_posix()
    try:
        code = compile(p.read_text(encoding="utf-8"), rel, "exec")
    except SyntaxError:
        continue
    walk(code, rel, lines.get(rel, {}))

# names -> tests that ran a statement (inside a function) mentioning them
class _StmtNames(ast.NodeVisitor):
    def __init__(self, fl):
        self.fl, self.depth = fl, 0

    def visit_FunctionDef(self, node):
        self.depth += 1
        for st in node.body:
            self.visit(st)
        self.depth -= 1

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self.depth += 1
        self._names(node.body, node.lineno, node.end_lineno)
        self.depth -= 1

    def _names(self, node, lo, hi):
        m = 0
        for l in range(lo, (hi or lo) + 1):
            m |= self.fl.get(l, 0)
        if not m:
            return
        for n in ast.walk(node):
            if isinstance(n, ast.Name):
                name_tests[n.id] |= m
            elif isinstance(n, ast.Attribute):
                name_tests[n.attr] |= m
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier():
                name_tests[n.value] |= m

    def generic_visit(self, node):
        if self.depth and isinstance(node, ast.stmt):
            bodies = [f for f in ("body", "orelse", "finalbody", "handlers", "cases") if getattr(node, f, None)]
            if bodies:
                # compound statement: header expressions only, then the bodies
                for fld, val in ast.iter_fields(node):
                    if fld in ("body", "orelse", "finalbody", "handlers", "cases"):
                        continue
                    if isinstance(val, ast.AST):
                        self._names(val, val.lineno if hasattr(val, "lineno") else node.lineno,
                                    getattr(val, "end_lineno", node.lineno))
                    elif isinstance(val, list):
                        for v in val:
                            if isinstance(v, ast.AST) and hasattr(v, "lineno"):
                                self._names(v, v.lineno, v.end_lineno)
                for fld in bodies:
                    for ch in getattr(node, fld):
                        if isinstance(ch, ast.excepthandler):
                            if ch.type is not None:
                                self._names(ch.type, ch.lineno, ch.type.end_lineno)
                            for c2 in ch.body:
                                self.visit(c2)
                        elif isinstance(ch, ast.match_case):
                            for c2 in ch.body:
                                self.visit(c2)
                        else:
                            self.visit(ch)
                return
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return super().generic_visit(node)
            self._names(node, node.lineno, node.end_lineno)
            for n in ast.walk(node):
                if isinstance(n, ast.Lambda):
                    pass
            return
        super().generic_visit(node)


for p in sorted((root / "sqlglot").rglob("*.py")) + sorted((root / "tests").rglob("*.py")):
    rel = p.relative_to(root).as_posix()
    try:
        _StmtNames(lines.get(rel, {})).visit(ast.parse(p.read_text(encoding="utf-8")))
    except SyntaxError:
        pass

# Static facts about module/class-level code, and tests per function name.
import ast  # noqa: E402

static_stmts = []  # [file, class qualname or "", [targets], [refs]]
func_tests = defaultdict(int)


def names_in(node):
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return out


def targets_of(st):
    ts = []
    if isinstance(st, ast.Assign):
        tg = st.targets
    elif isinstance(st, (ast.AnnAssign, ast.AugAssign)):
        tg = [st.target]
    elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [st.name]
    elif isinstance(st, (ast.Import, ast.ImportFrom)):
        return [(a.asname or a.name).split(".")[0] for a in st.names]
    else:
        return []
    for t in tg:
        for n in ast.walk(t):
            if isinstance(n, ast.Name):
                ts.append(n.id)
            elif isinstance(n, ast.Attribute):
                ts.append(n.attr)
    return ts


def scan(body, rel, qual, fl):
    for st in body:
        if isinstance(st, ast.ClassDef):
            q = f"{qual}.{st.name}" if qual else st.name
            static_stmts.append([rel, qual, [st.name], sorted(names_in(ast.Module(body=[], type_ignores=[])) | {n for b in st.bases + st.decorator_list for n in names_in(b)})])
            scan(st.body, rel, q, fl)
        elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            m = 0
            for l in range(st.lineno, st.end_lineno + 1):
                m |= fl.get(l, 0)
            func_tests[st.name] |= m
            for sub in ast.walk(st):
                if sub is not st and isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    mm = 0
                    for l in range(sub.lineno, sub.end_lineno + 1):
                        mm |= fl.get(l, 0)
                    func_tests[sub.name] |= mm
        elif isinstance(st, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
            static_stmts.append([rel, qual, targets_of(st), sorted(names_in(st))])
            for fld in ("body", "orelse", "finalbody"):
                scan(getattr(st, fld, []), rel, qual, fl)
            for h in getattr(st, "handlers", []):
                scan(h.body, rel, qual, fl)
        else:
            static_stmts.append([rel, qual, targets_of(st), sorted(names_in(st))])


for p in sorted((root / "sqlglot").rglob("*.py")) + sorted((root / "tests").rglob("*.py")):
    rel = p.relative_to(root).as_posix()
    try:
        tree = ast.parse(p.read_text(encoding="utf-8"))
    except SyntaxError:
        continue
    scan(tree.body, rel, "", lines.get(rel, {}))

sets: dict[int, int] = {}
sets_list: list[str] = []


def sid(m: int) -> int:
    i = sets.get(m)
    if i is None:
        i = sets[m] = len(sets_list)
        sets_list.append(format(m, "x"))
    return i


out_map = {
    "pin": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa",
    "ids": [t["id"] for t in sorted(tests, key=lambda t: t["number"])],
    "files": [t["file"] for t in sorted(tests, key=lambda t: t["number"])],
    "lines": {f: {str(l): sid(m) for l, m in sorted(fl.items())} for f, fl in sorted(lines.items())},
    "types": {c: sid(m) for c, m in sorted(types.items())},
    "opens": {o: sid(m) for o, m in sorted(opens.items())},
    "names": {nm: sid(m) for nm, m in sorted(name_tests.items())},
    "class_creation_names": sorted(class_creation_names),
    "import_types": sorted(import_types),
    "funcs": {nm: sid(m) for nm, m in sorted(func_tests.items())},
    "static": static_stmts,
    "triggers": {f: {str(l): sorted(map(list, ts)) for l, ts in sorted(d.items())} for f, d in sorted(triggers.items())},
    "import_lines":{f: sorted(ls) for f, ls in sorted(imp_lines.items())},
    "mro": mro,
    "sets": sets_list,
}
raw = json.dumps(out_map, separators=(",", ":")).encode()
out.write_bytes(lzma.compress(raw, preset=9))
print(f"map: {len(raw)/1e6:.1f} MB raw, {out.stat().st_size/1e6:.2f} MB xz, {len(sets_list)} sets", file=sys.stderr)
