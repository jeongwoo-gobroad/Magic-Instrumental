# SPDX-License-Identifier: GPL-3.0-only
"""Verify actual GP-to-PDF numeric glyphs on both macOS and Windows."""

import os
import sys
from pathlib import Path

import numpy as np
import pypdfium2 as pdfium
from defusedxml import ElementTree as XML
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

from practice_player.config import Paths
from practice_player.gp_render import SVG, prepare_gp
from tests.test_guitar_pro import gp_song


def test_gp_pdf_numbers_have_glyph_shapes_not_solid_boxes(tmp_path):
    app = QApplication.instance() or QApplication([])

    def digits(song):
        song.title = "Numeric Glyph Study"
        song.tracks[0].name = "Guitar"
        for i, measure in enumerate(song.tracks[0].measures):
            measure.voices[0].beats[0].notes[0].value = [0, 8, 10, 12][i % 4]

    source = gp_song(tmp_path / "numeric.gp5", count=12, configure=digits)
    result = prepare_gp(source, Paths(tmp_path))
    root = Path(result["pdf"]).parent
    svg = (root / "page-1.svg").read_bytes()
    tree = XML.fromstring(svg)
    assert not list(tree.iter(SVG + "text"))
    assert not list(tree.iter(SVG + "tspan"))
    graphic = QSvgRenderer(svg)
    labels = [e for e in tree.iter(SVG + "g") if e.get("class") == "outlined-text"]
    assert {"0", "8", "10", "12"} <= {e.get("aria-label") for e in labels}
    with pdfium.PdfDocument(result["pdf"]) as document:
        page = document[0]
        bitmap = page.render(scale=4)
        image = bitmap.to_pil().convert("RGB")
        samples = np.asarray(image)
        view = graphic.viewBoxF()
        for label in [e for e in labels if e.get("aria-label") in {"0", "8"}]:
            name = label.get("id")
            box = graphic.transformForElement(name).mapRect(graphic.boundsOnElement(name))
            left, right = round(box.left() / view.width() * image.width), round(
                box.right() / view.width() * image.width)
            top, bottom = round(box.top() / view.height() * image.height), round(
                box.bottom() / view.height() * image.height)
            crop = samples[top:bottom, left:right]
            assert crop.size > 100
            # A missing-font black block fills the box; 0/8 must retain counters.
            ink = (crop.mean(axis=2) < 128).mean()
            assert 0.12 < ink < 0.75, (label.get("aria-label"), ink)
        destination = os.environ.get("MAGIC_RENDER_CHECK_DIR")
        if destination:
            folder = Path(destination)
            folder.mkdir(parents=True, exist_ok=True)
            image.save(folder / f"gp-numeric-{sys.platform}.png")
        bitmap.close()
        page.close()
    assert prepare_gp(source, Paths(tmp_path))["cached"]
    app.processEvents()
