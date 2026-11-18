# SPDX-License-Identifier: GPL-3.0-only
"""Render actual Qt widgets using project-authored synthetic media, never user songs.

Run from the external venv: python tools/capture_screenshots.py
Uses already-downloaded, licensed-by-their-authors model weights for real separation.
Only PNG screenshots are published; generated audio/PDF/XML/caches live in a temporary folder.
"""

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")
os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
from PySide6.QtCore import QRectF  # noqa: E402
from PySide6.QtGui import QColor, QFont, QPainter, QPdfWriter, QPen  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from practice_player.config import Paths, Settings  # noqa: E402
from practice_player.gui import STYLE, Window  # noqa: E402
from practice_player.pipeline import process  # noqa: E402
from practice_player.score_analysis import analyze_score  # noqa: E402
from practice_player.score_layout import analyze_scroll, detect_layout  # noqa: E402


def pump(app, seconds=0.15):
    stop = time.monotonic() + seconds
    while time.monotonic() < stop:
        app.processEvents()
        time.sleep(0.01)


def link_or_copy(source, destination):
    try:
        return os.link(source, destination)
    except OSError:
        return shutil.copy2(source, destination)


def make_demo(paths):
    """Compose a simple 16-bar etude with an eight-bar repeat, at quarter = 120."""
    audio = paths.targets / "Practice Etude.wav"
    pdf = paths.score_targets / "Practice Etude.pdf"
    xml = pdf.with_suffix(".musicxml")
    patterns = [(60, 64, 67, 64), (62, 65, 69, 65), (64, 67, 71, 67), (67, 65, 62, 60)]
    route = list(range(8)) * 2 + list(range(8, 16))
    rate = 44100
    beat = np.arange(rate // 2) / rate
    notes = []
    for bar in route:
        for midi in patterns[bar % 4]:
            frequency = 440 * 2 ** ((midi - 69) / 12)
            envelope = (1 - np.exp(-beat * 100)) * np.exp(-beat * 6)
            melody = sum(np.sin(2 * np.pi * frequency * n * beat) / n**2 for n in (1, 2, 3))
            notes.append(0.18 * melody * envelope)
    samples = np.concatenate(notes)
    sf.write(audio, np.column_stack([samples, samples]), rate, subtype="PCM_16")

    writer = QPdfWriter(str(pdf))
    writer.setResolution(72)
    writer.setTitle("Practice Etude - original project demo")
    writer.setCreator("Magic-Instrumental")
    painter = QPainter(writer)
    painter.setPen(QColor("#172538"))
    painter.setFont(QFont("Helvetica", 25, QFont.Weight.Bold))
    painter.drawText(QRectF(35, 40, 500, 40), "Practice Etude")
    painter.setFont(QFont("Helvetica", 10))
    painter.drawText(35, 96, "Original demonstration score | C major | 4/4 | Quarter = 120")
    painter.drawText(
        35, 114, "Play bars 1-8 twice, then continue to bar 9. No commercial music used."
    )
    measures = []
    names = {60: ("C", 4), 62: ("D", 4), 64: ("E", 4), 65: ("F", 4),
             67: ("G", 4), 69: ("A", 4), 71: ("B", 4)}
    offsets = {60: 36, 62: 33, 64: 30, 65: 27, 67: 24, 69: 21, 71: 18}
    for system in range(4):
        y = 180 + system * 145
        painter.setPen(QPen(QColor("#182434"), 0.8))
        for line in range(5):
            painter.drawLine(35, y + line * 6, 535, y + line * 6)
        for boundary in range(5):
            x = 35 + boundary * 125
            painter.drawLine(x, y, x, y + 24)
        for column in range(4):
            bar = system * 4 + column
            x = 35 + column * 125
            painter.setFont(QFont("Helvetica", 9))
            painter.drawText(x + 3, y - 12, str(bar + 1))
            painter.setFont(QFont("Helvetica", 12, QFont.Weight.Bold))
            painter.drawText(x + 45, y - 28, ["C", "Dm", "Em", "G"][bar % 4])
            body = ""
            if bar == 0:
                body += (
                    "<attributes><divisions>1</divisions><key><fifths>0</fifths></key>"
                    "<time><beats>4</beats><beat-type>4</beat-type></time>"
                    "<clef><sign>G</sign><line>2</line></clef></attributes>"
                    '<barline location="left"><repeat direction="forward"/></barline>'
                )
            elif column == 0:
                body += '<print new-system="yes"/>'
            if bar == 7:
                body += '<barline location="right"><repeat direction="backward"/></barline>'
            for index, midi in enumerate(patterns[bar % 4]):
                nx, ny = x + 22 + index * 27, y + offsets[midi] - 6
                painter.setBrush(QColor("#182434"))
                painter.drawEllipse(QRectF(nx - 3, ny - 2, 6, 4))
                painter.drawLine(nx + 3, ny, nx + 3, ny - 25)
                if midi == 60:
                    painter.drawLine(nx - 6, y + 30, nx + 6, y + 30)
                step, octave = names[midi]
                body += (
                    f"<note><pitch><step>{step}</step><octave>{octave}</octave></pitch>"
                    "<duration>1</duration><type>quarter</type></note>"
                )
            measures.append(f'<measure number="{bar + 1}" width="125">{body}</measure>')
        if system in (0, 1):
            x = 42 if system == 0 else 528
            for yy in (y + 9, y + 15):
                painter.drawEllipse(QRectF(x - 1, yy - 1, 2, 2))
            painter.drawLine(x - 3 if system == 0 else x + 3, y,
                             x - 3 if system == 0 else x + 3, y + 24)
    painter.setFont(QFont("Helvetica", 9))
    painter.drawText(35, 770, "Magic-Instrumental | Project-authored demo | GPL-3.0-only")
    painter.end()
    xml.write_text(
        '<score-partwise version="4.0"><part-list><score-part id="P1">'
        '<part-name>Demo</part-name></score-part></part-list><part id="P1">'
        + "".join(measures) + "</part></score-partwise>", encoding="utf-8",
    )
    return audio, pdf


def main():
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    destination = ROOT / "docs/images"
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="magic-instrumental-demo-") as temporary:
        paths = Paths(Path(temporary))
        paths.create()
        audio, pdf = make_demo(paths)
        detect_layout(pdf, pdf.with_suffix(".musicxml"))
        # Reuse verified public-model downloads without touching user media or processed files.
        shutil.copytree(
            ROOT / "models", paths.models, dirs_exist_ok=True, copy_function=link_or_copy
        )
        result = process(audio, Settings(low_memory=True), paths, offline=True)
        base = analyze_score(audio, pdf, paths, mode="tempo", bpm=120)
        window = Window(paths)
        window.resize(1600, 1030)
        window.main_splitter.setSizes([620, 950])
        window.show()
        window.pending_source, window.source_name = audio, audio.name
        window.load_result(result.to_dict())
        window.progress.setValue(100)
        window.audio_output.setVolume(0)
        window.metronome.mute.setChecked(True)
        window.metronome.linked.setChecked(True)
        window.metronome.bpm.setValue(120)
        window.speed.setValue(75)
        window.measure_step.setValue(4)
        window.low_memory.setChecked(True)
        pump(app, 1)
        for filename, zoom, position in [
            ("player-overview.png", "page", 6200),
            ("score-zoom.png", 2.0, 21600),
        ]:
            panel = window.score_panel
            panel.zoom.setCurrentIndex(panel.zoom.findData(zoom))
            pump(app)
            timeline = analyze_scroll(audio, pdf, paths, base, panel.view.profile())
            panel.load_timeline(timeline)
            panel.follow.setChecked(True)
            window.player.setPosition(position)
            window.metronome.toggle.setChecked(True)
            window.player.play()
            pump(app, 0.5)
            window.grab().save(str(destination / filename))
            window.player.pause()
        window.seek_to(0)
        window.count_in.enabled.setChecked(True)
        window.toggle_play()
        pump(app, 0.8)
        if not window.count_in.active or window.count_in.clock is None:
            raise RuntimeError("Expected active drum count-in for the screenshot")
        window.grab().save(str(destination / "drum-count-in.png"))
        window.count_in.cancel()
        window.close()
        pump(app)
    print("Saved three actual GUI screenshots with synthetic demo media.")


if __name__ == "__main__":
    main()
