# SPDX-License-Identifier: GPL-3.0-only
"""A prepared option must still belong to its verified recording when exported."""

from pathlib import Path

import pytest

from practice_player.cache import hash_file
from practice_player.config import Settings
from practice_player.magic_bundle import export_magic
from practice_player.pipeline import process
from tests.test_audio_variants import SixStemEngine
from tests.test_magic_bundle import portable_study


@pytest.fixture
def provenance_request(tmp_path):
    request, _, paths = portable_study(tmp_path / "project", kind="pdf", count=4)
    result = process(Path(request["audio"]), Settings("sw6"), paths,
                     stem_runner=SixStemEngine(), variant_id="guitar")
    prepared = result.variants["guitar"].to_dict()
    option = {key: prepared[key] for key in ("id", "label", "mode", "target", "path")}
    option.update(
        expected_sha256=prepared["sha256"], expected_source_sha256=prepared["source_hash"],
        expected_frames=prepared["frames"],
    )
    request.update(audio_variants=[option], default_audio_variant="guitar", audio_format="wav",
                   pitch_semitones=2, overwrite=True)
    return request


@pytest.mark.parametrize("field,value", [
    ("expected_source_sha256", "f" * 64),
    ("expected_sha256", "f" * 64),
    ("expected_frames", 8 * 44100 + 1),
])
def test_wrong_prepared_provenance_preserves_previous_export_and_originals(
    provenance_request, monkeypatch, field, value,
):
    from practice_player import transpose

    request = provenance_request
    output = Path(request["output"])
    output.write_bytes(b"previous user export")
    files = [Path(request[key]) for key in ("audio", "pdf", "timeline")]
    files.append(Path(request["audio_variants"][0]["path"]))
    protected = {path: hash_file(path) for path in files}
    request["audio_variants"][0][field] = value
    prepared, prepare_pitch = [], transpose.prepare_pitch

    def record(*args, **kwargs):
        prepared.append(args[0])
        return prepare_pitch(*args, **kwargs)

    monkeypatch.setattr(transpose, "prepare_pitch", record)
    with pytest.raises(ValueError):
        export_magic(request)
    assert len(prepared) == (1 if field == "expected_frames" else 0)
    assert output.read_bytes() == b"previous user export"
    assert not list(output.parent.glob(".magic-*"))
    assert all(hash_file(path) == digest for path, digest in protected.items())
