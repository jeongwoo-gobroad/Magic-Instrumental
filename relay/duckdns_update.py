# SPDX-License-Identifier: GPL-3.0-only
"""Private-credential IPv4 DDNS updater; independent of the playback service."""

import ipaddress
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


class UpdateError(Exception):
    """Only fixed, credential-free messages may leave the updater."""


def request_config(domain: str, token: str) -> str:
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", domain):
        raise UpdateError("Invalid DuckDNS subdomain configuration.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", token):
        raise UpdateError("Missing or invalid DuckDNS credential.")
    return '\n'.join([
        'url = "https://www.duckdns.org/update"',
        'get',
        f'data-urlencode = "domains={domain}"',
        f'data-urlencode = "token={token}"',
        'data-urlencode = "ip="',
        'data-urlencode = "verbose=true"',
        '',
    ])


def parse_response(body: str) -> dict:
    lines = body.strip().splitlines()
    if len(lines) != 4 or lines[0] != "OK" or lines[3] not in {"UPDATED", "NOCHANGE"}:
        raise UpdateError("DuckDNS did not confirm the update.")
    try:
        address = ipaddress.IPv4Address(lines[1])
    except ipaddress.AddressValueError:
        raise UpdateError("DuckDNS returned an invalid IPv4 address.") from None
    if not address.is_global:
        raise UpdateError("DuckDNS returned a non-public IPv4 address.")
    return {"ipv4": str(address), "result": lines[3]}


def update(domain: str, token: str) -> dict:
    config = request_config(domain, token)
    # The secret stays on stdin, never in argv, environment, stderr or a URL log.
    # Force direct IPv4 so DuckDNS observes this host's public IPv4, not a proxy.
    command = [
        "/usr/bin/curl", "--disable", "--ipv4", "--noproxy", "*",
        "--proto", "=https", "--tlsv1.2", "--fail", "--silent", "--show-error",
        "--connect-timeout", "10", "--max-time", "30", "--config", "-",
    ]
    for attempt in range(3):
        try:
            response = subprocess.run(
                command, input=config, text=True, capture_output=True, timeout=32,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            response = None
        if response is not None and response.returncode == 0:
            return parse_response(response.stdout)
        if attempt < 2:
            time.sleep(5 if attempt == 0 else 15)
    raise UpdateError("DuckDNS could not be reached over HTTPS; the next timer will retry.")


def main() -> int:
    try:
        credential_dir = Path(os.environ["CREDENTIALS_DIRECTORY"])
        state_dir = Path(os.environ["STATE_DIRECTORY"])
        with (credential_dir / "duckdns-token").open() as stream:
            token = stream.read(257).strip()
        result = update(os.environ["DUCKDNS_DOMAIN"], token)
        result["last_success_utc"] = datetime.now(timezone.utc).isoformat()
        temporary = state_dir / "status.json.tmp"
        temporary.write_text(json.dumps(result) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(state_dir / "status.json")
    except UpdateError as error:
        print(str(error), flush=True)
        return 1
    except Exception:
        # OS/subprocess errors can embed request data; never print a traceback.
        print("DuckDNS updater could not read credentials or save its status.", flush=True)
        return 1
    print("DuckDNS IPv4 update confirmed: " + result["result"], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
