# SPDX-License-Identifier: GPL-3.0-only
"""Worker-only OMR input preparation. Never alter the user's PDF."""

from pathlib import Path

import numpy as np

from .score_geometry import find_staves, ink_mask, page_geometry, vector_verticals

OMR_VERSION = 2


def prepare_omr_input(pdf: Path, output: Path, report):
    """Mask verified six-line TAB companions in a private, native-resolution TIFF.

    This recognizes the five-line notation, not TAB fret pitches. Original pages
    remain the source of display geometry. Mixed/ambiguous layouts are rejected
    rather than silently removing a potentially musical staff.
    """
    import pypdfium2
    from PIL import Image

    output.mkdir(parents=True, exist_ok=True)
    plans = []
    with pypdfium2.PdfDocument(str(pdf)) as document:
        for number in range(len(document)):
            report(f"악보 구조 확인 · {number + 1}/{len(document)}페이지", None)
            page = document[number]
            bitmap = page.render(scale=min(3, 3200 / max(page.get_size())))
            try:
                gray = np.array(bitmap.to_pil().convert("L"))
                try:
                    staves = find_staves(ink_mask(gray))
                except ValueError:
                    staves = []
                counts = [len(s["lines"]) for s in staves]
                mixed = bool(counts and counts == [5, 6] * (len(counts) // 2))
                plans.append(mixed)
                if mixed:
                    # Do not strip a second instrument with unrelated barlines.
                    page_geometry(gray, [5, 6], vector_verticals(page, bitmap))
                    for index in range(1, len(staves), 2):
                        tab, standard = staves[index], staves[index - 1]
                        gap = tab["lines"][0] - standard["lines"][-1]
                        margin = min(1.5 * tab["spacing"], gap - 2.75 * standard["spacing"])
                        top = round(tab["lines"][0] - margin)
                        bottom = round(tab["lines"][-1] + 1.5 * tab["spacing"])
                        if margin < 0.75 * tab["spacing"]:
                            raise ValueError(
                                "오선과 타브 영역이 너무 가까워 안전하게 분리할 수 없습니다."
                            )
                        gray[max(0, top):min(len(gray), bottom)] = 255
                    Image.fromarray(gray).save(output / f"page-{number + 1}.png")
            finally:
                bitmap.close()
                page.close()
    if not any(plans):
        return pdf, {"method": "original-pdf", "version": OMR_VERSION}
    if not all(plans):
        raise ValueError(
            "페이지마다 오선·타브 구성이 달라 자동 분리를 중단했습니다. "
            "같은 판본의 MusicXML/GP5를 선택하거나 오선만 인쇄한 PDF를 사용하세요."
        )
    report("오선·타브 분리 완료 · 인식용 사본을 준비합니다.", None)
    images = [Image.open(output / f"page-{n + 1}.png") for n in range(len(plans))]
    target = output / "standard-staves.tiff"
    try:
        images[0].save(target, save_all=True, append_images=images[1:], compression="tiff_deflate")
    finally:
        for image in images:
            image.close()
    return target, {
        "method": "standard-staves-from-mixed-pdf", "version": OMR_VERSION,
        "pages": len(plans), "tab_pitch_recognition": False,
    }
