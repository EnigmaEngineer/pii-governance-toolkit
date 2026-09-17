"""The check collector.

A check is a module level function whose name starts with `check_`. No decorators, no
classes, no discovery magic. The loop is in its own module because a second runner will
stand in for the first one later, and a runner that imports the first to borrow its loop
imports itself the moment it is copied over the top. That cost me an afternoon on an
earlier project of mine.
"""

from __future__ import annotations

import importlib
import traceback
from typing import Callable, List, Tuple


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
