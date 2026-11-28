"""The extracted policy modules can be imported without the decision layer."""

import sys
from importlib import import_module
from unittest.mock import patch

import tbhbdev


def admin_modules() -> set[str]:
    return {name for name in sys.modules if name.startswith("tbhbdev.admin.")}


def test_policy_module_imports() -> None:
    with patch.dict(sys.modules), patch.object(tbhbdev, "admin", None, create=True):
        for name in admin_modules() | {"tbhbdev.admin"}:
            sys.modules.pop(name, None)
        paths = import_module("tbhbdev.admin.policy_paths")
        assert admin_modules() == {paths.__name__}

        ruleset = import_module("tbhbdev.admin.policy_ruleset")
        assert admin_modules() == {paths.__name__, ruleset.__name__}
