#!/usr/bin/env python3
"""Run the Music-Assistant-gated tests inside the Music Assistant container.

`tests/test_ma_provider.py` skips most of itself unless `music_assistant`,
`music_assistant_models` and `alexapy` are importable. On a laptop they are
not, so those tests report as skipped and prove nothing. The only place they
can actually run is inside the MA container, which has the packages but has no
pytest and no intention of growing one.

So this is a very small runner: it puts a stub `pytest` in `sys.modules`,
imports the test module, and calls every `test_*` function in it. That covers
the four things these tests use (`raises`, `importorskip`, `mark.parametrize`,
`fixture`) and nothing else.

    tools/provider_tests_in_container.sh

is the wrapper that copies this and the tests into a running container and runs
them there. Without it a change to the provider is only ever checked by the
tests that skip.
"""

from __future__ import annotations

import pathlib
import sys
import traceback
import types


def _install_pytest_stub() -> None:
    stub = types.ModuleType("pytest")

    class Skipped(Exception):
        """Raised by importorskip when a dependency really is absent."""

    class _Raises:
        def __init__(self, expected):
            self.expected = expected

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            if exc_type is None:
                raise AssertionError(f"expected {self.expected.__name__}")
            return issubclass(exc_type, self.expected)

    def importorskip(name, *_args, **_kwargs):
        try:
            return __import__(name)
        except ImportError as err:
            raise Skipped(name) from err

    def fixture(*args, **_kwargs):
        # Nothing in the gated tests takes a fixture argument; this exists so a
        # module-level decorator does not blow up at import.
        if args and callable(args[0]):
            return args[0]
        return lambda fn: fn

    def parametrize(_argnames, argvalues, **_kwargs):
        def wrap(fn):
            fn._parametrized = list(argvalues)
            return fn

        return wrap

    stub.Skipped = Skipped
    stub.raises = _Raises
    stub.importorskip = importorskip
    stub.fixture = fixture
    stub.approx = lambda value, rel=None, abs=None: value  # noqa: A002
    stub.mark = types.SimpleNamespace(parametrize=parametrize, skip=fixture)
    sys.modules["pytest"] = stub


def _cases(fn):
    """A test function and its arguments, once per parametrize case."""
    values = getattr(fn, "_parametrized", None)
    if values is None:
        return [((), "")]
    out = []
    for value in values:
        args = tuple(value) if isinstance(value, (tuple, list)) else (value,)
        out.append((args, f"[{value!r}]"))
    return out


def main(paths: list[str]) -> int:
    _install_pytest_stub()
    import pytest  # the stub

    root = pathlib.Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))

    passed = skipped = 0
    failures: list[str] = []

    for path in paths:
        module_path = pathlib.Path(path).resolve()
        sys.path.insert(0, str(module_path.parent))
        module = __import__(module_path.stem)

        for name in sorted(dir(module)):
            if not name.startswith("test_"):
                continue
            fn = getattr(module, name)
            if not callable(fn):
                continue
            for args, label in _cases(fn):
                try:
                    fn(*args)
                except pytest.Skipped as err:
                    skipped += 1
                    print(f"s {name}{label} (no {err})")
                except Exception:
                    failures.append(f"{name}{label}\n{traceback.format_exc()}")
                    print(f"F {name}{label}")
                else:
                    passed += 1
                    print(f". {name}{label}")

    for failure in failures:
        print("\n" + "=" * 70 + f"\n{failure}")

    print(f"\n{passed} passed, {skipped} skipped, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    args = sys.argv[1:] or ["tests/test_ma_provider.py"]
    raise SystemExit(main(args))
