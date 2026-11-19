# SPDX-License-Identifier: GPL-3.0-only
"""DDNS confirmation, retry and credential-leak regressions; no real requests."""

import json
from types import SimpleNamespace

import pytest

from relay import duckdns_update as ddns

TOKEN = "test-token-not-a-real-secret"


@pytest.mark.parametrize("body", ["KO", "OK", "OK\n127.0.0.1\n\nUPDATED",
                                    "OK\nnot-an-ip\n\nUPDATED",
                                    "OK\n8.8.8.8\n\nUNCONFIRMED"])
def test_unconfirmed_or_nonpublic_updates_fail(body):
    with pytest.raises(ddns.UpdateError):
        ddns.parse_response(body)


def test_transient_failure_retries_without_secret_in_arguments(monkeypatch):
    calls, delays = [], []

    def request(command, **kwargs):
        calls.append(command)
        assert TOKEN not in str(command)
        assert TOKEN in kwargs["input"]
        assert "--ipv4" in command and "--noproxy" in command
        assert "--insecure" not in command and "--location" not in command
        assert 'data-urlencode = "ip="' in kwargs["input"]
        assert 'ipv6=' not in kwargs["input"] and 'clear=' not in kwargs["input"]
        return SimpleNamespace(returncode=7 if len(calls) == 1 else 0,
                               stdout="OK\n8.8.8.8\n\nNOCHANGE\n", stderr=TOKEN)

    monkeypatch.setattr(ddns.subprocess, "run", request)
    monkeypatch.setattr(ddns.time, "sleep", delays.append)
    assert ddns.update("practice", TOKEN) == {"ipv4": "8.8.8.8", "result": "NOCHANGE"}
    assert len(calls) == 2 and delays == [5]


def test_failure_does_not_leak_or_replace_last_success(tmp_path, monkeypatch, capsys):
    (tmp_path / "duckdns-token").write_text(TOKEN)
    status = tmp_path / "status.json"
    status.write_text('{"last_success_utc":"previous"}')
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("DUCKDNS_DOMAIN", "practice")
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        return SimpleNamespace(returncode=22, stdout=TOKEN, stderr=TOKEN)

    monkeypatch.setattr(ddns.subprocess, "run", fail)
    monkeypatch.setattr(ddns.time, "sleep", lambda _: None)
    assert ddns.main() == 1
    assert len(calls) == 3
    assert json.loads(status.read_text())["last_success_utc"] == "previous"
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err


def test_success_persists_confirmation_without_credential(tmp_path, monkeypatch, capsys):
    (tmp_path / "duckdns-token").write_text(TOKEN)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("DUCKDNS_DOMAIN", "practice")
    monkeypatch.setattr(ddns.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout="OK\n8.8.8.8\n\nUPDATED\n", stderr=""))
    assert ddns.main() == 0
    status = (tmp_path / "status.json").read_text()
    assert json.loads(status)["result"] == "UPDATED"
    output = capsys.readouterr()
    assert TOKEN not in status + output.out + output.err


@pytest.mark.parametrize("domain,token", [("practice\nurl=bad", TOKEN),
                                           ("practice", TOKEN + '\"\nurl=bad')])
def test_reject_config_injection(domain, token):
    with pytest.raises(ddns.UpdateError):
        ddns.request_config(domain, token)
