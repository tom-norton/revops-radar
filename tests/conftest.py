"""Shared pytest setup. Not needed when a test file is run on its own with python.

Several tests stub a function by assigning over it on the module (test_apply.py's harness
replaces scan.load_profile, cvbuild.render and others), and the whole suite runs in one
process. Without a restore, every file that runs later sees the stub instead of the real
function: test_scoring.py's check that profile.md names every market was reading
"(profile)" and failing in the full run while passing on its own.

So each test gets the module-level names of the shared modules back as they were before it
ran. Only rebinding is undone; a test that mutates a list or dict in place still has to
clean up after itself.
"""

import sys

import pytest

SHARED_MODULES = ("scan", "cvbuild", "applyq")


@pytest.fixture(autouse=True)
def restore_module_globals():
    saved = {name: dict(vars(sys.modules[name]))
             for name in SHARED_MODULES if name in sys.modules}
    yield
    for name, before in saved.items():
        ns = vars(sys.modules[name])
        for key, value in before.items():
            if ns.get(key, value) is not value:
                ns[key] = value
