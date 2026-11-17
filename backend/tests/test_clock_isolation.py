"""Tests must not leak a frozen clock into later tests (see conftest.py).

A module that copies market_time's clock at import time keeps whatever function
it copied. If its first import happens inside a test that has monkeypatched
market_time, the fake outlives the test. conftest imports every such module
before any test runs; these checks keep that list complete.
"""
import os
import re
import sys
from pathlib import Path

import market_time
from conftest import CLOCK_COPYING_MODULES

BACKEND = Path(__file__).resolve().parents[1]
_SKIP_DIRS = {"tests", "venv", ".venv", "__pycache__", "node_modules"}
_TOP_LEVEL_IMPORT = re.compile(r"^from market_time import (\([^)]*\)|.*)$", re.M)
_CLOCK_NAME = re.compile(r"\bet_(?:today|now)\b")


def _modules_copying_the_clock():
    found = set()
    for root, dirs, files in os.walk(BACKEND):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if not name.endswith(".py"):
                continue
            path = Path(root, name)
            source = path.read_text(encoding="utf-8")
            if any(_CLOCK_NAME.search(m.group(1)) for m in _TOP_LEVEL_IMPORT.finditer(source)):
                found.add(".".join(path.relative_to(BACKEND).with_suffix("").parts))
    return found


def test_every_module_that_copies_the_clock_is_imported_before_any_test():
    copying = _modules_copying_the_clock()
    assert "signals.iv_executor" in copying          # the scan itself works
    missing = sorted(copying - set(CLOCK_COPYING_MODULES))
    assert not missing, f"add to CLOCK_COPYING_MODULES in tests/conftest.py: {missing}"


def test_no_module_still_holds_a_patched_clock():
    stale = []
    for name, module in list(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if not path:
            continue
        parts = Path(path).resolve().parts
        if (parts[:len(BACKEND.parts)] != BACKEND.parts
                or _SKIP_DIRS.intersection(parts[len(BACKEND.parts):])
                or "site-packages" in parts):
            continue
        for attr in ("et_today", "et_now"):
            value = module.__dict__.get(attr)
            if value is not None and value is not getattr(market_time, attr):
                stale.append(f"{name}.{attr}")
    assert not stale, f"a frozen clock leaked out of an earlier test: {stale}"
