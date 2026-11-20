# SPDX-License-Identifier: GPL-3.0-only
"""Standard-library-only release lookup, run in an isolated child interpreter.

Never import Qt or site packages here: DNS/proxy/TLS failures must stay outside
of the media GUI process. The parent enforces a separate wall-clock deadline.
"""

import argparse
import json
import re
from urllib.parse import urlsplit

REPO = "jeongwoo-gobroad/Magic-Instrumental"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES = f"https://github.com/{REPO}/releases/"
MAX_RESPONSE = 1024 * 1024


def numeric_version(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,4}\.\d{1,4}\.\d{1,4}", value):
        raise ValueError("정식 버전 번호가 아닙니다.")
    return tuple(map(int, value.split(".")))


def release_info(data: bytes, current):
    release = json.loads(data)
    tag = release["tag_name"]
    if (
        release.get("draft") is not False
        or release.get("prerelease") is not False
        or not isinstance(tag, str)
        or not tag.startswith("v")
    ):
        raise ValueError("공개 정식 버전이 아닙니다.")
    latest = tag[1:]
    available = numeric_version(latest) > numeric_version(current)
    # A newly created release may not yet have its updater inputs uploaded.
    assets = {item["name"]: item for item in release["assets"]}
    for name in (f"Magic-Instrumental-{latest}.zip", "PUBLIC_FILES.json", "SHA256SUMS.txt"):
        asset = assets[name]
        if (
            asset.get("state") != "uploaded"
            or asset.get("size", 0) <= 0
            or asset.get("browser_download_url") != RELEASES + f"download/{tag}/{name}"
        ):
            raise ValueError("업데이트 파일이 아직 준비되지 않았습니다.")
    # Never display release-body HTML or follow a URL supplied by the response.
    return {"latest": latest, "available": available, "url": RELEASES + f"tag/{tag}"}


def fetch_release(endpoint, current):
    # Imported here only: importing metadata helpers in the GUI must not load TLS.
    import os
    import urllib.request

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    url = urlsplit(endpoint)
    # The GUI uses API only. HTTP loopback injection is for regression fixtures.
    if endpoint != API and not (
        url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost", "::1"}
        and not url.username and not url.password
    ):
        raise ValueError("Unexpected release endpoint")
    request = urllib.request.Request(endpoint, headers={
        "User-Agent": "Magic-Instrumental", "Accept": "application/vnd.github+json",
    })
    if endpoint == API and os.environ.get("GITHUB_ACTIONS") == "true":
        token = os.environ.get("MAGIC_CI_RELEASE_TOKEN")
        if token:
            request.add_unredirected_header("Authorization", f"Bearer {token}")
    # Default HTTPS context validates both certificate chain and host name.
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=5) as response:
        if response.status != 200:
            raise ValueError("Unexpected release status")
        data = response.read(MAX_RESPONSE + 1)
    if len(data) > MAX_RESPONSE:
        raise ValueError("Oversized release response")
    return release_info(data, current)


def error_code(error):
    # Only static categories cross the process boundary, never exception text.
    import ssl
    from urllib.error import HTTPError, URLError

    if isinstance(error, HTTPError):
        return f"http{error.code}" if error.code in {403, 429} else "network"
    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "certificate"
    if isinstance(error, (URLError, OSError)):
        return "network"
    if isinstance(error, (ValueError, KeyError, TypeError)):
        return "metadata"
    return "worker"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default=API)
    parser.add_argument("--current", required=True)
    args = parser.parse_args()
    try:
        info = fetch_release(args.endpoint, args.current)
    except Exception as error:
        # Never send proxy credentials, remote bodies or local paths to the UI.
        print(json.dumps({"error": error_code(error)}), flush=True)
        return 2
    print(json.dumps(info), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
