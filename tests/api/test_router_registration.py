"""Guard: every router under api/endpoints is wired into api.routers.

The registration tuple in api/__init__.py is maintained by hand (explicit over
auto-discovery magic), so this test catches the one easy mistake — adding an
endpoint module but forgetting to register its router, which would otherwise
silently 404.
"""

import importlib
import pkgutil

from expense_analyzer import api
from expense_analyzer.api import endpoints


def test_all_endpoint_routers_are_registered() -> None:
    registered = {id(r) for r in api.routers}

    missing = []
    for mod in pkgutil.iter_modules(endpoints.__path__):
        module = importlib.import_module(f"{endpoints.__name__}.{mod.name}")
        router = getattr(module, "router", None)
        if router is not None and id(router) not in registered:
            missing.append(mod.name)

    assert not missing, f"endpoint routers not registered in api.routers: {missing}"
