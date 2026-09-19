"""Minimal JSON client for the OpsForge API, standard library only, so the
scripts run with any Python 3.11+ and nothing to install.

The base URL comes from ``BASE_URL`` (default http://localhost:8000).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

BASE = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")


class ApiError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


def call(method: str, path: str, body: Any = None, params: dict | None = None, timeout: float = 120) -> Any:
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise ApiError(exc.code, exc.read().decode(errors="replace")) from None
    return json.loads(raw) if raw else None


def get(path: str, **params: Any) -> Any:
    return call("GET", path, params=params or None)


def post(path: str, body: Any = None, **params: Any) -> Any:
    return call("POST", path, body={} if body is None else body, params=params or None)
