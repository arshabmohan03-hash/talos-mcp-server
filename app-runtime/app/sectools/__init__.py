"""Aggregates the security-tool category modules into one registry + dispatcher.

Each category module (encoding.py, network.py, …) exposes:
  * tool functions (sync or async) returning JSON-serializable dicts
  * a module-level SPECS list describing each tool for the UI

This package imports them defensively (a module that fails to build won't break
the rest), builds a name→callable registry, and exposes catalog() + run().
"""
from __future__ import annotations

import asyncio
import inspect

_MODULE_NAMES = ["encoding", "network", "webapp", "osint", "forensics", "defensive", "binary", "extra", "vulnintel", "osvdev"]
_modules = {}
for _n in _MODULE_NAMES:
    try:
        _modules[_n] = __import__(f"app.sectools.{_n}", fromlist=[_n])
    except Exception as e:  # noqa: BLE001 — one broken module shouldn't sink the others
        print(f"[sectools] skipped {_n}: {type(e).__name__}: {e}")

REGISTRY: dict[str, tuple] = {}   # tool name -> (module, spec)
CATALOG: list[dict] = []          # JSON-serializable specs for the UI

for _mod in _modules.values():
    for _spec in getattr(_mod, "SPECS", []):
        _name = _spec.get("name")
        if _name and hasattr(_mod, _name):
            REGISTRY[_name] = (_mod, _spec)
            CATALOG.append(_spec)


def catalog() -> list[dict]:
    """All tool specs, for the Tools page."""
    return CATALOG


async def run(name: str, args: dict | None) -> dict:
    """Dispatch a tool by name with keyword args; always returns a dict."""
    entry = REGISTRY.get(name)
    if not entry:
        return {"error": f"Unknown tool: {name}"}
    mod, _spec = entry
    fn = getattr(mod, name)
    kwargs = dict(args or {})
    try:
        if inspect.iscoroutinefunction(fn):
            return await fn(**kwargs)
        return await asyncio.to_thread(lambda: fn(**kwargs))
    except TypeError as e:
        return {"error": f"Bad arguments: {e}"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
