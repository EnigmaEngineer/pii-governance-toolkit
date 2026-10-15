"""The check collector.

A check is a module level function whose name starts with `check_`. No decorators, no
classes, no discovery magic. The loop is in its own module because a second runner will
stand in for the first one later, and a runner that imports the first to borrow its loop
imports itself the moment it is copied over the top. That cost me an afternoon on an
earlier project of mine.
"""

from __future__ import annotations

import importlib
import os
import traceback
from typing import Callable, List, Tuple

# Words that make a figure this machine's rather than the measurement's. A committed
# number has to re-derive somewhere else and a millisecond does not. The list lives here
# because it was written out twice, in tests/test_roundtwo_warehouse.py and in
# tests/test_mask_applied.py, word for word, and two copies of one rule are edited in one
# place and not the other.
WALL_TIME_WORDS: Tuple[str, ...] = ("_ms", "seconds", "millis")


def assert_no_wall_time(path: str) -> int:
    """Refuse a duration anywhere in a manifest, and return its size so a caller can tell
    the file was actually read.

    Reading the raw text rather than the parsed JSON on purpose. A duration can arrive as
    a key, as a string value or inside a nested list, and a walk over the structure is a
    second description of the shape of a file whose shape is nobody's decision.
    """
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    assert text.strip(), "{} is empty, so this checked nothing".format(path)
    for word in WALL_TIME_WORDS:
        assert word not in text, (
            "{} carries {}, which is a wall time, and the manifest is counts only"
            .format(os.path.basename(path), word))
    return len(text)


def collect(module_names: List[str]) -> List[Tuple[str, Callable]]:
    found = []
    for name in module_names:
        mod = importlib.import_module(name)
        for attr in sorted(dir(mod)):
            if attr.startswith("check_"):
                found.append(("{}.{}".format(name, attr), getattr(mod, attr)))
    return found


def run_checks(module_names: List[str], verbose: bool = False) -> int:
    checks = collect(module_names)

    # A collector that finds nothing prints "0 failed" and exits 0, which reads as a pass
    # and is how a suite pointed at the wrong package reports success.
    if not checks:
        print("FAIL: collected zero checks from {}".format(module_names))
        return 1

    failures = []
    for name, fn in checks:
        try:
            fn()
            if verbose:
                print("  ok   {}".format(name))
        except Exception:
            failures.append((name, traceback.format_exc()))
            print("  FAIL {}".format(name))

    for name, tb in failures:
        print("\n--- {} ---\n{}".format(name, tb))

    print("{} passed, {} failed, {} checks".format(
        len(checks) - len(failures), len(failures), len(checks)))
    return 1 if failures else 0
