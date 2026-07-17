"""JSON bridge from NitroStack TypeScript tools to the existing Talos Python tools."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import sys
import traceback
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def _repo_root() -> Path:
    env_root = os.environ.get("TALOS_APP_ROOT")
    if env_root:
        return Path(env_root).resolve()
    return Path(__file__).resolve().parents[2]


def _json_default(value: Any) -> str:
    return str(value)


async def _run() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {"error": "No JSON request was provided to the Talos bridge."}

    request = json.loads(raw)
    tool = str(request.get("tool") or "")
    args = request.get("args") or {}
    if not tool:
        return {"error": "Missing tool name."}
    if not isinstance(args, dict):
        return {"error": "Tool args must be a JSON object."}

    root = _repo_root()
    os.chdir(root)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    # Keep stdout clean for the JSON response. Any accidental prints from the
    # Python app go to stderr so the Node side can still parse stdout.
    with contextlib.redirect_stdout(sys.stderr):
        from app.ai.tools import dispatch  # noqa: PLC0415

        result = await dispatch(tool, args)
    return result if isinstance(result, dict) else {"result": result}


def main() -> int:
    try:
        result = asyncio.run(_run())
        sys.stdout.write(json.dumps(result, ensure_ascii=False, default=_json_default))
        return 0
    except Exception as exc:  # noqa: BLE001
        payload = {
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(limit=8),
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
