# SPDX-License-Identifier: GPL-3.0-only
import io
import sys
from contextlib import nullcontext
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from practice_player.config import MODELS, Paths, Settings
from practice_player.engine import separate_stems
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


@pytest.mark.parametrize("model", ["hyperace", "mega"])
def test_model_bad_download_is_not_published(tmp_path, monkeypatch, model):
    class Response(io.BytesIO):
        headers = {}

    monkeypatch.setattr("practice_player.models.urlopen", lambda *_, **__: Response(b"bad"))
    with pytest.raises(RuntimeError, match="SHA-256"):
        ensure_model(MODELS[model], tmp_path, lambda *_: None)
    assert not list(tmp_path.rglob("*.ckpt"))
    assert not list(tmp_path.rglob("*.download"))


def test_keyboard_model_uses_fixed_official_release_and_verified_cache(tmp_path, monkeypatch):
    original = MODELS["mega"]
    content = {original.weights: b"authored-checkpoint", original.config: b"authored-config"}
    model = replace(
        original,
        weights_sha256=sha256(content[original.weights]).hexdigest(),
        config_sha256=sha256(content[original.config]).hexdigest(),
    )
    urls = []

    class Response(io.BytesIO):
        def __init__(self, value):
            super().__init__(value)
            self.headers = {"Content-Length": str(len(value))}

    def download(request, **_):
        urls.append(request.full_url)
        return Response(content[request.full_url.rsplit("/", 1)[-1]])

    monkeypatch.setattr("practice_player.models.urlopen", download)
    downloaded = ensure_model(model, tmp_path, lambda *_: None)
    assert urls == [f"{original.download_base_url}/{name}"
                    for name in (original.weights, original.config)]
    assert original.download_base_url == (
        "https://github.com/ZFTurbo/Music-Source-Separation-Training/releases/download/v1.0.21"
    )
    assert all(path.parent == tmp_path / "mega" / "v1.0.21" for path in downloaded)
    assert ensure_model(model, tmp_path, lambda *_: None, offline=True) == downloaded
    assert len(urls) == 2
    downloaded[0].write_bytes(b"corrupt")
    with pytest.raises(RuntimeError, match="오프라인"):
        ensure_model(model, tmp_path, lambda *_: None, offline=True)


@pytest.mark.parametrize("model", ["leap", "mega"])
def test_model_download_size_bound_precedes_payload_read(tmp_path, monkeypatch, model):
    class Oversized(io.BytesIO):
        headers = {"Content-Length": str(2 * 1024**3 + 1)}

        def read(self, *_):
            raise AssertionError("Oversized model download must be rejected before reading")

    monkeypatch.setattr("practice_player.models.urlopen", lambda *_, **__: Oversized())
    with pytest.raises(RuntimeError, match="크기"):
        ensure_model(MODELS[model], tmp_path, lambda *_: None)
    assert not list(tmp_path.rglob("*.ckpt"))
    assert not list(tmp_path.rglob("*.download"))


@pytest.mark.parametrize("model_key", ["mega", "sw6"])
def test_mps_keyboard_model_requests_only_selected_torch_heads(
    tone, tmp_path, monkeypatch, model_key,
):
    parameters = {}

    class Separator:
        def __init__(self, **kwargs):
            parameters.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def process_folder(self, source):
            for stem, directory in parameters["store_dirs"].items():
                (Path(directory) / f"{Path(source).stem}_{stem}.wav").write_bytes(b"test-output")
            return 1

    monkeypatch.setattr("practice_player.engine.select_device", lambda *_: "mps")
    monkeypatch.setattr("practice_player.engine.ensure_model", lambda *_args, **_kwargs:
                        (tmp_path / "weights.ckpt", tmp_path / "config.yaml"))
    monkeypatch.setattr("practice_player.engine.mps_mel_band_fix", lambda *_: nullcontext())
    monkeypatch.setitem(sys.modules, "pymss", SimpleNamespace(MSSeparator=Separator))
    settings = Settings(model=model_key, device="mps", low_memory=True)
    device, stems = separate_stems(tone, tmp_path / "outputs", settings, Paths(tmp_path),
                                  lambda *_: None, offline=True)
    assert device == "mps" and set(stems) == set(dict(MODELS[model_key].stems))
    assert parameters["inference_params"]["chunk_size"] == 352800
    if model_key == "mega":
        assert parameters["inference_params"]["mps_model_backend"] == "torch"
        assert set(parameters["store_dirs"]) == {"keys", "synth"}
        assert settings.signature()["model"]["mps_model_backend"] == "torch"
    else:
        assert "mps_model_backend" not in parameters["inference_params"]
        assert "mps_model_backend" not in settings.signature()["model"]
