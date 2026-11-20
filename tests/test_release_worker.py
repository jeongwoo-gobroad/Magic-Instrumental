# SPDX-License-Identifier: GPL-3.0-only
import json
import ssl
from urllib.error import HTTPError, URLError

import pytest

from practice_player import release_check


@pytest.mark.parametrize("actions,endpoint,authorized", [
    ("true", release_check.API, True),
    ("true", "http://localhost/test", False),
    ("false", release_check.API, False),
])
def test_ci_release_authorization(monkeypatch, actions, endpoint, authorized):
    from urllib import request

    class Capture:
        def open(self, req, timeout):
            assert req.get_header("Authorization") == ("Bearer fixture" if authorized else None)
            assert "Authorization" not in req.headers
            raise OSError("captured request")

    monkeypatch.setenv("GITHUB_ACTIONS", actions)
    monkeypatch.setenv("MAGIC_CI_RELEASE_TOKEN", "fixture")
    monkeypatch.setattr(request, "build_opener", lambda *_: Capture())
    with pytest.raises(OSError, match="captured request"):
        release_check.fetch_release(endpoint, "2.3.4")


@pytest.mark.parametrize("error,expected", [
    (HTTPError("https://private.invalid", 403, "private data", None, None), "http403"),
    (HTTPError("https://private.invalid", 429, "private data", None, None), "http429"),
    (URLError(ssl.SSLCertVerificationError("private data")), "certificate"),
    (URLError("private data"), "network"),
    (ValueError("private data"), "metadata"),
    (RuntimeError("private data"), "worker"),
])
def test_worker_only_reports_safe_failure_category(monkeypatch, capsys, error, expected):
    def fail(*_):
        raise error

    monkeypatch.setattr(release_check, "fetch_release", fail)
    monkeypatch.setattr("sys.argv", ["release_check.py", "--current", "2.3.1"])
    assert release_check.main() == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"error": expected}
    assert "private" not in captured.out and not captured.err
