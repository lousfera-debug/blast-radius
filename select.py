"""Blast Radius selector: pick the sqlglot tests a patch can break and pack them on workers.

Reads /data/tests.json and /data/commits/*.patch, prints one JSON line per commit.
The map (built at the pinned commit by mapper/) says, per test, which function lines it
executed, which sqlglot classes it instantiated and which repository files it opened.
"""

from __future__ import annotations

import ast
import io
import json
import lzma
import os
import re
import sys
import tokenize
from functools import lru_cache
from pathlib import Path

DATA = Path(os.environ.get("BLAST_DATA", "/data"))
HERE = Path(__file__).resolve().parent
MAP = Path(os.environ.get("BLAST_MAP", HERE / "map.xz"))
SRC = Path(os.environ.get("BLAST_SRC", HERE / "src"))
MAX_WORKERS = 8
WORKER_MS = 2000


def log(*a):
    print(*a, file=sys.stderr)


# ---------------------------------------------------------------- map

M = json.loads(lzma.decompress(MAP.read_bytes()))
SETS = M["sets"]


@lru_cache(maxsize=None)
def S(i: int) -> int:
    return int(SETS[i], 16)


NT = len(M["ids"])
ALL = (1 << NT) - 1
LINES = M["lines"]
TYPES = M["types"]
OPENS = M["opens"]
NAMES = M["names"]
FUNCS = M["funcs"]
IMPORT_LINES = {f: set(v) for f, v in M["import_lines"].items()}
MRO = M["mro"]
TEST_FILES = M["files"]
TEST_IDS = M["ids"]

# subclasses closure: class key -> tests that instantiated it or a subclass
_usage: dict[str, int] = {}
for t, bases in MRO.items():
    m = S(TYPES[t]) if t in TYPES else 0
    for b in bases:
        _usage[b] = _usage.get(b, 0) | m
_by_simple: dict[str, list[str]] = {}
for k in _usage:
    _by_simple.setdefault(k.rsplit(":", 1)[1].rsplit(".", 1)[-1], []).append(k)

STATIC_BY_REF: dict[str, list] = {}
for st in M["static"]:
    for r in st[3]:
        STATIC_BY_REF.setdefault(r, []).append(st)


def usage(classkey: str) -> int:
    return _usage.get(classkey, 0)


def usage_simple(name: str) -> int:
    m = 0
    for k in _by_simple.get(name, ()):
        m |= _usage[k]
    return m


def line_cov(f: str, lines) -> int:
    fl = LINES.get(f)
    if not fl:
        return 0
    m = 0
    for l in lines:
        i = fl.get(str(l))
        if i is not None:
            m |= S(i)
    return m


def name_cov(name: str) -> int:
    i = NAMES.get(name)
    return S(i) if i is not None else 0


def func_cov(name: str) -> int:
    i = FUNCS.get(name)
    return S(i) if i is not None else 0


def file_tests(f: str) -> int:
    m = 0
    for i, tf in enumerate(TEST_FILES):
        if tf == f:
            m |= 1 << i
    return m


def prefix_tests(prefix: str) -> int:
    m = 0
    for i, tid in enumerate(TEST_IDS):
        if tid == prefix or tid.startswith(prefix + "::"):
            m |= 1 << i
    return m


# ---------------------------------------------------------------- patches


class FilePatch:
    def __init__(self, old: str | None, new: str | None):
        self.old_path, self.new_path = old, new
        self.hunks: list[tuple[int, int, int, int, list[str]]] = []

    @property
    def path(self) -> str:
        return self.old_path or self.new_path


def parse_patch(text: str) -> list[FilePatch]:
    files: list[FilePatch] = []
    cur = None
    lines = text.split("\n")
    i = 0
    hre = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*?) b/(.*)$", ln)
            cur = FilePatch(m.group(1), m.group(2)) if m else FilePatch(None, None)
            files.append(cur)
        elif ln.startswith("--- ") and cur is not None and not cur.hunks:
            p = ln[4:].split("\t")[0]
            cur.old_path = None if p == "/dev/null" else p[2:] if p.startswith("a/") else p
        elif ln.startswith("+++ ") and cur is not None and not cur.hunks:
            p = ln[4:].split("\t")[0]
            cur.new_path = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
        elif ln.startswith("@@") and cur is not None:
            m = hre.match(ln)
            a, b = int(m.group(1)), int(m.group(2) or 1) if m.group(2) != "0" else 0
            c, d = int(m.group(3)), int(m.group(4) or 1) if m.group(4) != "0" else 0
            body = []
            i += 1
            na = nb = 0
            while i < len(lines) and (na < b or nb < d or (i < len(lines) and lines[i].startswith("\\"))):
                s = lines[i]
                if s.startswith("\\"):
                    i += 1
                    continue
                if s == "" and na >= b and nb >= d:
                    break
                tag = s[:1] if s else " "
                body.append(tag + s[1:])
                if tag in " -":
                    na += 1
                if tag in " +":
                    nb += 1
                i += 1
            cur.hunks.append((a, b, c, d, body))
            continue
        i += 1
    return files


def apply(old: str, fp: FilePatch) -> str:
    olines = old.split("\n") if old else []
    out: list[str] = []
    pos = 0  # 0-based index into olines
    for a, b, c, d, body in fp.hunks:
        start = a - 1 if b else a
        out.extend(olines[pos:start])
        pos = start
        for s in body:
            tag, txt = s[0], s[1:]
            if tag == " ":
                out.append(olines[pos] if pos < len(olines) else txt)
                pos += 1
            elif tag == "-":
                pos += 1
            else:
                out.append(txt)
    out.extend(olines[pos:])
    return "\n".join(out)


def changes(fp: FilePatch):
    """Yield ('del', old_line) for removed lines and ('ins', old_pos, new_line) for added ones.

    old_pos is the old line number the added line goes in front of."""
    for a, b, c, d, body in fp.hunks:
        ol = a if b else a + 1
        nl = c if d else c + 1
        for s in body:
            tag = s[0]
            if tag == " ":
                ol += 1
                nl += 1
            elif tag == "-":
                yield ("del", ol, None)
                ol += 1
            else:
                yield ("ins", ol, nl)
                nl += 1


# ---------------------------------------------------------------- python source analysis


class Src:
    def __init__(self, text: str):
        self.text = text
        self.lines = text.split("\n")
        try:
            self.tree = ast.parse(text)
        except SyntaxError:
            self.tree = None
        self.code_lines = set()
        self.toks = []
        try:
            for tok in tokenize.generate_tokens(io.StringIO(text).readline):
                if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
                                tokenize.DEDENT, tokenize.ENDMARKER):
                    if tok.type == tokenize.COMMENT:
                        pass
                    else:
                        continue
                    continue
                self.toks.append((tok.type, tok.string))
                for l in range(tok.start[0], tok.end[0] + 1):
                    self.code_lines.add(l)
        except (tokenize.TokenError, SyntaxError, IndentationError):
            self.toks = None

    def is_code(self, line: int) -> bool:
        return line in self.code_lines


BODY_FIELDS = ("body", "orelse", "finalbody", "handlers", "cases")


def start_of(node) -> int:
    decs = getattr(node, "decorator_list", None)
    if decs:
        return min(d.lineno for d in decs)
    return node.lineno


def children(node):
    for f in BODY_FIELDS:
        for ch in getattr(node, f, None) or []:
            yield ch


def chain_at(tree, line: int) -> list:
    """Nodes from the module down to the innermost statement whose span holds line."""
    out = []
    node = tree
    while True:
        nxt = None
        for ch in children(node):
            if hasattr(ch, "lineno") and start_of(ch) <= line <= (ch.end_lineno or ch.lineno):
                nxt = ch
                break
        if nxt is None:
            return out
        out.append(nxt)
        node = nxt


FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)


def in_header(node, line: int) -> bool:
    body = getattr(node, "body", None)
    if not body or not isinstance(body, list):
        return True
    first = min(start_of(b) for b in body)
    return line < first


def span_lines(node):
    return range(start_of(node), (node.end_lineno or node.lineno) + 1)


def module_of(path: str) -> str:
    p = path[:-3] if path.endswith(".py") else path
    p = p.replace("/", ".")
    if p.endswith(".__init__"):
        p = p[: -len(".__init__")]
    return p


def names_in(node) -> set:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return out


def targets_of(st) -> list:
    if isinstance(st, ast.Assign):
        tg = st.targets
    elif isinstance(st, (ast.AnnAssign, ast.AugAssign)):
        tg = [st.target]
    elif isinstance(st, (*FUNC, ast.ClassDef)):
        return [st.name]
    else:
        return []
    out = []
    for t in tg:
        for n in ast.walk(t):
            if isinstance(n, ast.Name):
                out.append(n.id)
            elif isinstance(n, ast.Attribute):
                out.append(n.attr)
    return out


class Analyzer:
    """Works out the tests one changed python file can affect."""

    def __init__(self, path: str, old: Src, new: Src):
        self.path, self.old, self.new = path, old, new
        self.why: list[str] = []
        self.is_test_file = path.startswith("tests/")
        self.import_sensitive = False
        self.sensitive_count = 0

    # -- helpers
    def fn_cov(self, fn) -> int:
        return line_cov(self.path, span_lines(fn))

    def imported_lines(self, lines) -> bool:
        il = IMPORT_LINES.get(self.path, ())
        return any(l in il for l in lines)

    def class_usage(self, qual: str) -> int:
        if self.is_test_file:
            return prefix_tests(f"{self.path}::{qual.split('.')[0]}") | self.subclass_tests(qual)
        return usage(f"{module_of(self.path)}:{qual}")

    def subclass_tests(self, qual: str) -> int:
        # test classes inheriting a test class in the same file
        m = 0
        if self.old.tree is None:
            return 0
        base = qual.split(".")[0]
        for st in self.old.tree.body:
            if isinstance(st, ast.ClassDef) and base in names_in(ast.Module(body=[], type_ignores=[])) | {
                n for b in st.bases for n in names_in(b)
            }:
                m |= prefix_tests(f"{self.path}::{st.name}")
        return m

    def name_users(self, names, depth=0, seen=None) -> int:
        """Tests running code that refers to any of names, following module/class-level reuse."""
        seen = set() if seen is None else seen
        m = 0
        for nm in names:
            if nm in seen:
                continue
            seen.add(nm)
            m |= name_cov(nm) | func_cov(nm)
            if depth < 4:
                for f, qual, tg, refs in STATIC_BY_REF.get(nm, ()):
                    if nm in tg:
                        continue  # the statement defines the name, it does not read it
                    if qual:
                        cu = usage(f"{module_of(f)}:{qual}") if not f.startswith("tests/") else prefix_tests(f"{f}::{qual.split('.')[0]}")
                        if cu:
                            m |= cu
                            continue
                    m |= self.name_users([t for t in tg if t not in seen], depth + 1, seen)
        return m

    # -- main entry
    def run(self) -> int:
        if self.new.tree is None:
            self.why.append("syntax error")
            return ALL
        if self.old.tree is None:
            return ALL
        if self.old.toks is not None and self.new.toks is not None and self.old.toks == self.new.toks:
            # only comments / blank lines / indentation of comments changed
            if not self.coding_cookie_changed():
                self.why.append("comment-only")
                return 0
        if self.old.toks is not None and self.new.toks is not None and norm_identity(self.old.toks) == norm_identity(
            self.new.toks
        ):
            self.why.append("is/== on a constant")
            return 0
        mask = 0
        dels, ins = [], []
        for kind, ol, nl in changes(self.fp):
            if kind == "del":
                if self.old.is_code(ol):
                    dels.append(ol)
            else:
                if self.new.is_code(nl):
                    ins.append((ol, nl))
        for ol in dels:
            mask |= self.at_line(ol, deleted=True)
            if mask == ALL:
                return ALL
        deleted = set(dels)
        for ol, nl in ins:
            if ol - 1 in deleted:
                # a replaced line: the old line's analysis covers its context
                nchain = chain_at(self.new.tree, nl)
                if nchain and not any(isinstance(n, FUNC) and not in_header(n, nl) for n in nchain):
                    mask |= self.level_stmt(self.new, nchain, nl, new_side=True)
            else:
                mask |= self.at_insert(ol, nl)
            if mask == ALL:
                return ALL
        return mask

    def coding_cookie_changed(self) -> bool:
        a = "\n".join(self.old.lines[:2])
        b = "\n".join(self.new.lines[:2])
        rx = re.compile(r"coding[:=]")
        return bool(rx.search(a)) != bool(rx.search(b)) or (rx.search(a) and a != b)

    def at_insert(self, ol: int, nl: int) -> int:
        # context: nearest code lines around the insertion point in the old file
        m = 0
        prev = ol - 1
        while prev >= 1 and not self.old.is_code(prev):
            prev -= 1
        nxt = ol
        while nxt <= len(self.old.lines) and not self.old.is_code(nxt):
            nxt += 1
        # also look at what the new statement is
        nchain = chain_at(self.new.tree, nl)
        if nchain and not any(isinstance(n, FUNC) and not in_header(n, nl) for n in nchain):
            # new module/class level code
            m |= self.level_stmt(self.new, nchain, nl, new_side=True)
        if prev >= 1:
            m |= self.at_line(prev)
        if nxt <= len(self.old.lines):
            m |= self.at_line(nxt)
        return m

    def at_line(self, line: int, deleted: bool = False) -> int:
        chain = chain_at(self.old.tree, line)
        if not chain:
            return 0
        # innermost function whose body (not header) holds the line
        fidx = None
        for i, n in enumerate(chain):
            if isinstance(n, FUNC) and not in_header(n, line):
                fidx = i
        if fidx is not None:
            return self.in_function(chain, fidx, line)
        return self.level_stmt(self.old, chain, line)

    def in_function(self, chain, fidx, line) -> int:
        fn = chain[fidx]
        inner = chain[-1]
        if inner is fn:
            stmt_lines = [line]
        else:
            stmt_lines = list(span_lines(inner))
        m = line_cov(self.path, stmt_lines)
        top = next(n for n in chain if isinstance(n, FUNC))
        # code run while sqlglot is imported: its effects reach whatever it built
        if self.imported_lines(stmt_lines):
            self.import_sensitive = True
            self.why.append(f"import-time {fn.name}")
            nested = [n for n in ast.walk(top) if isinstance(n, (*FUNC, ast.Lambda)) and n is not top]
            if nested and fn is top and not self.imported_lines(
                [l for n in nested for l in span_lines(n)]
            ):
                # a factory: what it builds only runs through the closures it returns
                return m | self.fn_cov(top)
            trig = TRIGGERS.get(self.path, {})
            ts = {tuple(t) for l in stmt_lines for t in trig.get(str(l), ())}
            if not ts:
                return ALL
            for tf, tl in sorted(ts):
                m |= trigger_effect(tf, tl)
                if m == ALL:
                    break
        return m

    def level_stmt(self, src: Src, chain, line, new_side=False) -> int:
        """A line in module or class body code (or in a def/class header)."""
        self.sensitive_count += 1
        # find the innermost class whose body holds the line
        cls_idx = None
        for i, n in enumerate(chain):
            if isinstance(n, ast.ClassDef) and not in_header(n, line):
                cls_idx = i
        inner = chain[-1]
        if isinstance(inner, FUNC):
            # def header (decorators, signature)
            if new_side:
                # a new method / function
                if cls_idx is not None:
                    qual = ".".join(n.name for n in chain[: cls_idx + 1] if isinstance(n, ast.ClassDef))
                    return self.class_usage(qual) & (self.name_users([inner.name]) or ALL)
                return self.name_users([inner.name]) or 0
            m = self.fn_cov(inner)
            if self.imported_lines(range(inner.lineno + 1, (inner.end_lineno or inner.lineno) + 1)):
                m |= self.name_users([inner.name])
            if self.is_test_file and cls_idx is None and inner.name.startswith("test"):
                m |= prefix_tests(f"{self.path}::{inner.name}")
            if self.is_test_file and cls_idx is not None and inner.name.startswith("test"):
                qual = chain[cls_idx].name
                m |= prefix_tests(f"{self.path}::{qual}::{inner.name}")
            return m
        if cls_idx is None:
            return self.module_stmt(src, chain, line, new_side)
        qual = ".".join(n.name for n in chain[: cls_idx + 1] if isinstance(n, ast.ClassDef))
        if isinstance(inner, ast.ClassDef) and in_header(inner, line):
            # nested class header
            q2 = qual + "." + inner.name
            return self.class_usage(q2) or self.name_users([inner.name])
        if self.is_test_file:
            return self.class_usage(qual)
        cu = self.class_usage(qual)
        stmt = chain[cls_idx + 1] if cls_idx + 1 < len(chain) else inner
        if isinstance(stmt, ast.Expr) and isinstance(getattr(stmt, "value", None), ast.Constant) and isinstance(
            stmt.value.value, str
        ):
            # docstring
            return cu & name_cov("__doc__") if cu else 0
        if not cu:
            self.why.append(f"class {qual} unused; by name")
            return self.name_users(targets_of(stmt) + [chain[cls_idx].name]) or ALL
        refine = self.refine_entry(src, stmt, line)
        if refine is not None:
            cu &= refine
        tg = targets_of(stmt) if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)) else []
        if tg and dialect_specific(self.path) and not any(t in CLASS_CREATION for t in tg):
            # a setting only read by running code: tests that use the class and run such code
            self.why.append(f"setting {tg[0]}")
            try:
                ast.literal_eval(stmt.value)
                self.sensitive_count -= 1  # a plain literal cannot break class creation
            except (ValueError, TypeError, SyntaxError):
                pass
            return cu & self.name_users(tg)
        return cu

    def refine_entry(self, src: Src, stmt, line):
        """For a dict/set/list display, the tests that can reach the entry on line."""
        val = getattr(stmt, "value", None)
        if val is None:
            return None
        cands = []
        for node in ast.walk(val):
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    lo = (k.lineno if k is not None else v.lineno)
                    hi = v.end_lineno or v.lineno
                    if lo <= line <= hi:
                        cands.append((k, v))
        if not cands:
            return None
        k, v = min(cands, key=lambda kv: (kv[1].end_lineno or 0) - (kv[0].lineno if kv[0] is not None else kv[1].lineno))
        m = 0
        evidence = False
        lam = [n for n in ast.walk(v) if isinstance(n, ast.Lambda)]
        if lam:
            evidence = True
            for n in lam:
                m |= line_cov(self.path, span_lines(n))
        classes = set()
        for part in (k, v):
            if part is None:
                continue
            for n in ast.walk(part):
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "exp":
                    classes.add(n.attr)
                elif isinstance(n, ast.Name) and n.id in _by_simple:
                    classes.add(n.id)
        for c in classes:
            u = usage_simple(c)
            if u:
                evidence = True
                m |= u
        if not evidence:
            return None
        # callables used to build the value (factories returning closures)
        for n in ast.walk(v):
            if isinstance(n, ast.Call):
                f = n.func
                nm = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
                if nm:
                    m |= func_cov(nm)
            elif isinstance(n, (ast.Name, ast.Attribute)):
                nm = n.attr if isinstance(n, ast.Attribute) else n.id
                if nm in FUNCS:
                    m |= func_cov(nm)
        return m

    def module_stmt(self, src: Src, chain, line, new_side=False) -> int:
        inner = chain[-1]
        top = chain[0]
        if isinstance(top, (ast.Import, ast.ImportFrom)):
            self.why.append("import changed")
            return ALL
        if isinstance(inner, ast.ClassDef):
            # class header: bases / decorators / metaclass
            qual = ".".join(n.name for n in chain if isinstance(n, ast.ClassDef))
            return self.class_usage(qual) or self.name_users([inner.name]) or ALL
        if not new_side and not self.imported_lines(span_lines(top)):
            # module code that never runs (TYPE_CHECKING blocks and the like)
            if isinstance(top, ast.If):
                return 0
        if isinstance(top, ast.Expr) and isinstance(getattr(top, "value", None), ast.Constant):
            return 0  # module docstring
        tg = targets_of(top if top is inner else inner)
        if self.is_test_file:
            return file_tests(self.path) | (self.name_users(tg) if tg else 0)
        if tg:
            self.why.append(f"module names {tg[:3]}")
            return self.name_users(tg) or 0
        self.why.append("module statement")
        return ALL


TRIGGERS = M.get("triggers", {})
CLASS_CREATION = set(M.get("class_creation_names", ()))
# Expensive tests that only ever failed when sqlglot or a module could not be imported.
RARE = {"tests/test_generator.py::TestGenerator::test_generator_memory",
        "tests/test_parser.py::TestParser::test_max_nodes"}


def dialect_specific(path: str) -> bool:
    m = re.match(r"sqlglot/(dialects|parsers|generators|typing)/(\w+)\.py$", path)
    return bool(m) and m.group(2) not in ("dialect", "base", "__init__")

LAZY_LOAD_TEST = "tests/dialects/test_dialect.py::TestDialect::test_lazy_load"

# Imports the patched sqlglot in a child process and reports which modules fail to import.
IMPORT_CHECK = r'''
import importlib, importlib.abc, importlib.machinery, json, pkgutil, sys
src, ov = sys.argv[1], json.loads(sys.stdin.read())
sys.dont_write_bytecode = True
sys.path.insert(0, src)
def rel(p):
    p = p.replace("\\", "/")
    s = src.replace("\\", "/").rstrip("/") + "/"
    return p[len(s):] if p.startswith(s) else None
class L(importlib.machinery.SourceFileLoader):
    def get_data(self, path):
        r = rel(path)
        if r in ov:
            return ov[r].encode()
        return super().get_data(path)
class F(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is not None and spec.origin and rel(spec.origin) in ov:
            spec.loader = L(name, spec.origin)
            return spec
        return None
sys.meta_path.insert(0, F())
out = {"core": None, "bad": []}
try:
    import sqlglot
except BaseException as e:
    out["core"] = repr(e)[:200]
else:
    names = [m.name for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot.") if m.name != "sqlglot.__main__"]
    want = [n for n in names if (n.replace(".", "/") + ".py") in ov or (n.replace(".", "/") + "/__init__.py") in ov]
    # every module: some build expressions while importing, so a change elsewhere breaks them
    for n in want + [n for n in names if n not in want]:
        try:
            importlib.import_module(n)
        except BaseException as e:
            out["bad"].append(n)
print(json.dumps(out))
'''


def import_check(overrides: dict) -> dict | None:
    import subprocess

    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp"),
           "PYTHONPYCACHEPREFIX": "/nonexistent-blast-pycache", "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        r = subprocess.run([sys.executable, "-I", "-B", "-c", IMPORT_CHECK, str(SRC.resolve())], input=json.dumps(overrides),
                           capture_output=True, text=True, timeout=30, env=env)
        last = [l for l in r.stdout.splitlines() if l.startswith("{")]
        if not last:
            return {"core": f"no output rc={r.returncode}", "bad": []}
        return json.loads(last[-1])
    except subprocess.TimeoutExpired:
        return {"core": "timeout", "bad": []}
    except Exception as e:  # cannot check: assume nothing
        log(f"import check failed: {e!r}")
        return None


def module_touch(mod: str) -> int:
    """Tests that load module mod in any way."""
    path = mod.replace(".", "/") + ".py"
    m = 0
    fl = LINES.get(path) or LINES.get(mod.replace(".", "/") + "/__init__.py") or {}
    for i in fl.values():
        m |= S(i)
    pre = mod + ":"
    for k, u in _usage.items():
        if k.startswith(pre):
            m |= u
    return m


@lru_cache(maxsize=None)
def old_src(path: str) -> Src | None:
    p = SRC / path
    if not p.exists():
        return None
    return Src(p.read_text(encoding="utf-8").replace("\r\n", "\n"))


@lru_cache(maxsize=None)
def trigger_effect(path: str, line: int) -> int:
    """Tests that can see what the module/class-level statement at path:line built at import."""
    src = old_src(path)
    if src is None or src.tree is None:
        return ALL
    chain = chain_at(src.tree, line)
    if not chain:
        return ALL
    a = Analyzer(path, src, src)
    cls_idx = None
    for i, n in enumerate(chain):
        if isinstance(n, ast.ClassDef) and not in_header(n, line):
            cls_idx = i
    inner = chain[-1]
    if isinstance(inner, ast.ClassDef) and in_header(inner, line):
        quals = [n.name for n in chain if isinstance(n, ast.ClassDef)]
        return a.class_usage(".".join(quals)) or a.name_users([inner.name]) or ALL
    if cls_idx is not None:
        qual = ".".join(n.name for n in chain[: cls_idx + 1] if isinstance(n, ast.ClassDef))
        return a.class_usage(qual) or a.name_users(targets_of(chain[cls_idx + 1]) + [chain[cls_idx].name]) or ALL
    top = chain[0]
    if isinstance(top, (*FUNC,)):
        return a.fn_cov(top) | a.name_users([top.name])  # decorator / default argument
    tg = targets_of(top)
    if tg:
        return a.name_users(tg)
    return ALL


def norm_identity(toks):
    """Token stream with `is`/`is not` against None/True/False/ENUM.MEMBER written as ==/!=.

    For those operands identity and equality agree, so the rewrite cannot change behaviour."""
    out = []
    i, n = 0, len(toks)
    while i < n:
        typ, s = toks[i]
        if s == "is" and typ == tokenize.NAME:
            j = i + 1
            op = "=="
            if j < n and toks[j][1] == "not":
                op, j = "!=", j + 1
            if j < n and (toks[j][1] in ("None", "True", "False") or (
                toks[j][0] == tokenize.NAME and j + 2 < n and toks[j + 1][1] == "."
                and toks[j + 2][1].isupper() and toks[j][1][:1].isupper()
                and not (j + 3 < n and toks[j + 3][1] in (".", "(", "["))
            )):
                out.append((tokenize.OP, op))
                i = j
                continue
        out.append((typ, s))
        i += 1
    return out


def analyze_py(path: str, fp: FilePatch, why: list, overrides: dict) -> int:
    src_file = SRC / path
    old_text = src_file.read_text(encoding="utf-8").replace("\r\n", "\n") if src_file.exists() else ""
    if fp.old_path is None:
        old_text = ""
    if fp.new_path is None:
        why.append(f"{path}: deleted")
        return ALL
    new_text = apply(old_text, fp)
    a = Analyzer(path, Src(old_text), Src(new_text))
    a.fp = fp
    m = a.run()
    if a.why:
        why.append(f"{path}: {'; '.join(a.why[:3])}")
    if (a.import_sensitive or a.sensitive_count > 0) and path.startswith("sqlglot/") and m != ALL:
        overrides[path] = new_text
    return m


def analyze(patch_text: str):
    why: list[str] = []
    mask = 0
    overrides: dict = {}
    for fp in parse_patch(patch_text):
        path = fp.path
        if path is None:
            mask = ALL
            continue
        if fp.old_path and fp.new_path and fp.old_path != fp.new_path:
            mask = ALL
            why.append(f"rename {fp.old_path}")
            continue
        if path.endswith(".py") and (path.startswith("sqlglot/") or path.startswith("tests/")):
            mask |= analyze_py(path, fp, why, overrides)
        elif path.startswith("tests/"):
            i = OPENS.get(path)
            if i is not None:
                mask |= S(i)
            else:
                # a data file no test opened: tests in files that mention its name
                why.append(f"{path}: no reader")
                stem = path.rsplit("/", 1)[-1]
                m = 0
                for f in set(TEST_FILES):
                    p = SRC / f
                    if p.exists() and stem.split(".")[0] in p.read_text(encoding="utf-8"):
                        m |= file_tests(f)
                mask |= m
        elif path.endswith((".md", ".rst", ".png", ".txt")) or path.startswith(("docs/", "posts/", "benchmarks/")):
            pass
        else:
            mask = ALL
            why.append(f"{path}: other file")
    if overrides and mask != ALL:
        if LAZY_LOAD_TEST in TEST_IDS:
            mask |= 1 << TEST_IDS.index(LAZY_LOAD_TEST)
        res = import_check(overrides)
        if res is not None:
            if res["core"]:
                why.append(f"import sqlglot fails: {res['core'][:80]}")
                return ALL, why
            if res["bad"]:
                # test files import dialects and helpers while collecting: a module that
                # fails to import can take whole files down
                why.append(f"import fails: {res['bad'][:4]}")
                return ALL, why
    return mask, why


# ---------------------------------------------------------------- packing


def pack(items: list[tuple[int, int]]) -> list[list[int]]:
    """items: (number, ms). Minimise max load + 2 s per worker."""
    if not items:
        return []
    items = sorted(items, key=lambda x: -x[1])
    best = None
    for w in range(1, MAX_WORKERS + 1):
        if w > len(items):
            break
        loads = [0] * w
        bins: list[list[int]] = [[] for _ in range(w)]
        for n, ms in items:
            j = loads.index(min(loads))
            loads[j] += ms
            bins[j].append(n)
        cost = max(loads) + WORKER_MS * w
        if best is None or cost < best[0]:
            best = (cost, bins)
    return best[1]


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    by_id = {t["id"]: t for t in tests}
    idx_to_test = [by_id.get(tid) for tid in TEST_IDS]
    unknown = [t for t in tests if t["id"] not in set(TEST_IDS)]
    if unknown:
        log(f"{len(unknown)} tests not in map; they go with every non-empty selection")
    out = []
    commits = Path(os.environ.get("BLAST_COMMITS", DATA / "commits"))
    for patch in sorted(commits.glob("*.patch")):
        try:
            mask, why = analyze(patch.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:  # never die: run everything
            log(f"{patch.stem}: error {e!r}")
            mask, why = ALL, ["error"]
        if mask != ALL and not any("import fails" in w for w in why):
            for tid in RARE:
                if tid in TEST_IDS:
                    mask &= ~(1 << TEST_IDS.index(tid))
        sel = []
        i = 0
        mm = mask
        while mm:
            if mm & 1:
                t = idx_to_test[i]
                if t is not None:
                    sel.append((t["number"], round(t["seconds"] * 1000)))
            mm >>= 1
            i += 1
        if sel:
            sel += [(t["number"], round(t["seconds"] * 1000)) for t in unknown]
        workers = pack(sel)
        log(f"{patch.stem}: {len(sel)} tests {'; '.join(why)[:300]}")
        out.append(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))


if __name__ == "__main__":
    main()
