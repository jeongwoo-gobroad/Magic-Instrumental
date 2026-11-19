# SPDX-License-Identifier: GPL-3.0-only
import json
import ssl
from urllib.error import HTTPError, URLError

import pytest

from practice_player import release_check


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
