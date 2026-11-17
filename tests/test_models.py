# SPDX-License-Identifier: GPL-3.0-only
import io
from dataclasses import replace
from hashlib import sha256

import pytest

from practice_player.config import MODELS
from practice_player.models import ensure_model


def test_model_download_checksum_and_offline_reuse(tmp_path, monkeypatch):
    content = b"fake-model"
    checksum = sha256(content).hexdigest()
    model = replace(MODELS["hyperace"], weights_sha256=checksum, config_sha256=checksum)

    class Response(io.BytesIO):
        headers = {"Content-Length": str(len(content))}

    calls = []

    def download(request, **_):
        calls.append(request.full_url)
        return Response(content)

    monkeypatch.setattr("practice_player.models.urlopen", download)
    first = ensure_model(model, tmp_path, lambda *_: None)
    second = ensure_model(model, tmp_path, lambda *_: None, offline=True)
    assert first == second and len(calls) == 2
    assert all(model.revision in url for url in calls)
    first[0].write_bytes(b"corrupt")
    with pytest.raises(RuntimeError, match="오프라인"):
        ensure_model(model, tmp_path, lambda *_: None, offline=True)


def test_model_bad_download_is_not_published(tmp_path, monkeypatch):
    class Response(io.BytesIO):
        headers = {}

    monkeypatch.setattr("practice_player.models.urlopen", lambda *_, **__: Response(b"bad"))
    with pytest.raises(RuntimeError, match="SHA-256"):
        ensure_model(MODELS["hyperace"], tmp_path, lambda *_: None)
    assert not list(tmp_path.rglob("*.ckpt"))
    assert not list(tmp_path.rglob("*.download"))
