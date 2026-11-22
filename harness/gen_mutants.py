"""Generate synthetic commits in the style of the sample set.

usage: python gen_mutants.py <sqlglot checkout> <out dir> <count> [seed]
"""

import difflib
import io
import json
import random
import re
import sys
import tokenize
from pathlib import Path

root, out, count = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
rng = random.Random(int(sys.argv[4]) if len(sys.argv) > 4 else 1)
out.mkdir(parents=True, exist_ok=True)

PY = sorted(p for p in (root / "sqlglot").rglob("*.py") if "__pycache__" not in p.parts)
FIX = sorted(p for p in (root / "tests" / "fixtures").rglob("*.sql"))
PHRASES = ["Cheap check first", "Keep in sync with the generator", "Kept for backwards compatibility",
           "Intentionally not cached", "TODO: revisit", "Order matters here", "See dialect docs"]


def code_sites(text):
    """(line, col, old, new) replacement candidates on code tokens."""
    sites = []
    toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    for i, tok in enumerate(toks):
        s, (l, c) = tok.string, tok.start
        if tok.type == tokenize.NAME:
            if s == "and":
                sites.append((l, c, s, "or"))
            elif s == "or":
                sites.append((l, c, s, "and"))
            elif s == "True":
                sites.append((l, c, s, "False"))
            elif s == "False":
                sites.append((l, c, s, "True"))
            elif s == "not" and i + 1 < len(toks) and toks[i + 1].string != "in":
                sites.append((l, c, "not ", ""))
            elif s == "is" and i + 1 < len(toks) and toks[i + 1].string != "not":
                sites.append((l, c, s, "=="))
        elif tok.type == tokenize.OP:
            sw = {"==": "!=", "!=": "==", "<": "<=", ">": ">=", "<=": "<", ">=": ">", "+": "-", "-": "+"}
            if s in sw:
                sites.append((l, c, s, sw[s]))
        elif tok.type == tokenize.NUMBER and re.fullmatch(r"\d+", s):
            sites.append((l, c, s, str(int(s) + rng.choice([1, -1]) if int(s) > 0 else 1)))
    return sites


def mutate_code(path):
    text = path.read_text(encoding="utf-8")
    sites = code_sites(text)
    if not sites:
        return None
    l, c, old, new = rng.choice(sites)
    lines = text.splitlines(keepends=True)
    line = lines[l - 1]
    assert line[c:c + len(old)] == old, (path, l, c, old)
    lines[l - 1] = line[:c] + new + line[c + len(old):]
    return "".join(lines)


def mutate_comment(path):
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    for _ in range(50):
        i = rng.randrange(1, len(lines))
        nxt = lines[i]
        if not nxt.strip():
            continue
        indent = nxt[: len(nxt) - len(nxt.lstrip())]
        lines.insert(i, f"{indent}# {rng.choice(PHRASES)}\n")
        return "".join(lines)
    return None


TOKS = ["1", "42", "x", "xa", "foo", ",", "(", "AS", "SELECT", "''"]


def mutate_fixture(path):
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    cands = [i for i, s in enumerate(lines) if s.strip() and not s.lstrip().startswith(("#", "--"))]
    if not cands:
        return None
    i = rng.choice(cands)
    words = [m.start() for m in re.finditer(r" ", lines[i])]
    if not words:
        return None
    p = rng.choice(words)
    tok = rng.choice(TOKS)
    lines[i] = lines[i][:p] + " " + tok + lines[i][p:]
    return "".join(lines)


def diff(path, old, new):
    rel = path.relative_to(root).as_posix()
    body = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                f"a/{rel}", f"b/{rel}", n=3)
    return f"diff --git a/{rel} b/{rel}\nindex 1111111..2222222 100644\n" + "".join(body)


KINDS = ["code"] * 45 + ["comment"] * 15 + ["fixture"] * 25 + ["multi"] * 15
meta = {}
n = 0
while n < count:
    kind = rng.choice(KINDS)
    parts = [kind] if kind != "multi" else rng.sample(["code", "code", "comment", "fixture"], rng.choice([2, 3]))
    files, chunks = set(), []
    for part in parts:
        for _ in range(20):
            path = rng.choice(FIX if part == "fixture" else PY)
            if path in files:
                continue
            new = {"code": mutate_code, "comment": mutate_comment, "fixture": mutate_fixture}[part](path)
            if new is None:
                continue
            files.add(path)
            chunks.append((path, diff(path, path.read_text(encoding="utf-8"), new)))
            break
    if not chunks:
        continue
    chunks.sort(key=lambda c: str(c[0]))
    cid = f"m{n:04d}"
    (out / f"{cid}.patch").write_text("".join(c for _, c in chunks), encoding="utf-8", newline="\n")
    meta[cid] = kind
    n += 1
(out / "kinds.json").write_text(json.dumps(meta, indent=0))
