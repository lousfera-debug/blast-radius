# Blast Radius entry

Static test selection for patches against sqlglot `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`.
At run time it runs no tests. It reads each patch, works out what the patch can reach,
and packs the selected tests onto workers to minimise `busiest worker + 2 s × workers`.

## How it decides

`map.xz` was built once at the pinned commit by tracing every test (`mapper/`):

* **lines**: function-body lines each test executed (`sys.monitoring`), including work
  the test sends to `ProcessPoolExecutor` children, and argument/result pickling, which
  runs sqlglot's `serde`.
* **types**: sqlglot classes whose `__init__` ran for each test (Dialect, Tokenizer,
  Parser, Generator and Expression subclasses). This gives "tests that use class C".
* **opens**: repository files each test opened (fixtures).
* **names / funcs**: tests running code that refers to a name or a function.
* Setup and collection work is shared with every test of the class or module, and each
  test was also traced in its own process, so lazily built caches count for every test
  that relies on them.

For each changed file, `select.py`:

* **Fixture or data file**: selects the tests that opened it.
* **Python file, only comments changed** (same token stream): selects nothing.
* **Line in a function body**: selects the tests that executed the enclosing statement.
  If the line runs while sqlglot is imported, it selects the closures' tests for a
  factory, and everything otherwise.
* **Class-level statement**: selects the tests that instantiated the class or a
  subclass. A dict entry keyed by an expression class (`TRANSFORMS`, …) narrows that to
  tests that also built that expression or ran the entry's lambda or factory.
* **Module-level assignment**: selects the tests running code that refers to the name,
  following module- and class-level reuse.
* **Imports, syntax errors, unknown files**: selects everything.

## Layout

| Path | What |
| --- | --- |
| `Dockerfile` | the submission image |
| `select.py` | the selector, stdlib only |
| `map.xz` | the coverage map (lzma JSON) |
| `src.tar.xz` | pinned `sqlglot/` and `tests/` Python sources, for patch context |
| `mapper/` | `tracer.py` (pytest plugin), `trace_isolated.py`, `build_map.py` |
| `harness/` | synthetic commit generator, local labeler and diff report |

## Rebuilding the map

```sh
git clone https://github.com/tobymao/sqlglot && cd sqlglot && git checkout eb11aec4be8cb2122afe713a4816662c3f0fb2fa
pip install -r ../kit/requirements.lock
SKIP_INTEGRATION=0 PYTHONPATH=.:../mapper BLAST_OUT=../work/trace_full python -m pytest tests -p tracer -n 8 --dist loadfile
python ../mapper/trace_isolated.py . ../kit/sample/tests.json ../work/trace_iso 8
python ../mapper/build_map.py . ../kit/sample/tests.json ../map.xz ../work/trace_full ../work/trace_iso
```
