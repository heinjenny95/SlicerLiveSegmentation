"""Loopback-only SSH fixture for real Slicer integration tests (not deployment)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "tests"))
from test_ssh_transport import LocalSSHServer  # noqa: E402


def main():
    folder = Path(sys.argv[1]).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    server = LocalSSHServer(folder)
    known_hosts = folder / "test-known-hosts"
    server.pin(known_hosts)
    client = server.client("fixture", known_hosts)
    print(json.dumps({"location": client.location, "known_hosts": str(known_hosts), "username": "test-user", "password": "test-password"}), flush=True)
    try:
        sys.stdin.readline()
    finally:
        server.close()


if __name__ == "__main__":
    main()
