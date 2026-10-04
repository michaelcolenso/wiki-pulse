#!/usr/bin/env python3
"""Push WikiPulse dashboard assets to Cloudflare KV.

Replaces the local static file server (dashboard/server.py) as the
publishing mechanism. After collect.py / analyze.py / realtime_monitor.py
regenerate the dashboard/*.json files (or dashboard/index.html changes),
call this script to push the updated content live — no deploy needed,
no local port, no tunnel.

Usage:
    python3 push_to_kv.py                  # push all known assets
    python3 push_to_kv.py realtime.json    # push just one file

Reads the Cloudflare API token from ~/.hermes/config.yaml (the same
Bearer token the Cloudflare MCP uses). Uses stdlib urllib only.
"""

import os
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path

ACCOUNT_ID = "4e921a01da1f55b0ddb32bb38a5524ce"
NAMESPACE_ID = "d4d6baa7f7f34eb8bed82c6ffded14db"
PROJECT_DIR = Path(__file__).parent
DASHBOARD_DIR = PROJECT_DIR / "dashboard"

ASSETS = [
    "index.html",
    "realtime.json",
    "spikes.json",
    "history.json",
    "context-data.json",
]

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}


def read_token() -> str:
    cfg_path = os.path.expanduser("~/.hermes/config.yaml")
    with open(cfg_path, encoding="utf-8") as fh:
        for line in fh:
            m = re.search(r"Authorization:\s*Bearer\s+(\S+)", line)
            if m:
                return m.group(1).strip().strip('"').strip("'")
    raise SystemExit("ERROR: no CF Bearer token found in ~/.hermes/config.yaml")


def push_asset(token: str, name: str) -> None:
    path = DASHBOARD_DIR / name
    if not path.exists():
        print(f"  skip {name}: not found on disk")
        return
    data = path.read_bytes()
    ext = path.suffix
    content_type = CONTENT_TYPES.get(ext, "application/octet-stream")

    url = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/storage/kv/namespaces/{NAMESPACE_ID}/values/{name}"
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
            if resp.status == 200:
                print(f"  ✓ {name} ({len(data):,} bytes)")
            else:
                print(f"  ✗ {name}: HTTP {resp.status} — {body[:200]}")
    except urllib.error.HTTPError as e:
        print(f"  ✗ {name}: HTTP {e.code} — {e.read().decode()[:200]}")


def main() -> None:
    token = read_token()
    targets = sys.argv[1:] if len(sys.argv) > 1 else ASSETS
    unknown = [t for t in targets if t not in ASSETS]
    if unknown:
        print(f"WARNING: unknown asset(s) {unknown}, pushing anyway")
    print(f"Pushing {len(targets)} asset(s) to KV namespace {NAMESPACE_ID}...")
    for name in targets:
        push_asset(token, name)


if __name__ == "__main__":
    main()
