# SPDX-License-Identifier: GPL-3.0-only
"""Lazy display adapter for the verified PNG pages carried by a .magic file."""

from pathlib import Path


class PageImages:
    def __init__(self, pages):
        if not isinstance(pages, list) or not 1 <= len(pages) <= 200:
            raise ValueError("1~200페이지의 악보가 필요합니다.")
        self.pages = tuple(dict(page) for page in pages)
        for page in self.pages:
            if (
                not isinstance(page.get("path"), str)
                or Path(page["path"]).suffix.lower() != ".png"
                or any(type(page.get(key)) is not int or not 1 <= page[key] <= 2201
                       for key in ("width", "height"))
            ):
                raise ValueError("저장된 악보 페이지 정보가 올바르지 않습니다.")

    def __len__(self):
        return len(self.pages)

    def __getitem__(self, index):
        return _Page(self.pages[index])

    def close(self):
        pass


class _Page:
    def __init__(self, info):
        self.info = info

    def get_size(self):
        return self.info["width"], self.info["height"]

    def render(self, *, scale):
        from PIL import Image

        size = self.get_size()
        target = tuple(max(1, min(4096, round(value * scale))) for value in size)
        with Image.open(self.info["path"]) as source:
            if source.format != "PNG" or source.size != size:
                raise ValueError("악보 그림이 변경되었습니다. .magic 파일을 다시 여세요.")
            picture = source.convert("RGB")
        if picture.size != target:
            resized = picture.resize(target, Image.Resampling.LANCZOS)
            picture.close()
            picture = resized
        return _Bitmap(picture)

    def close(self):
        pass


class _Bitmap:
    def __init__(self, image):
        self.image = image

    def to_pil(self):
        return self.image

    def close(self):
        self.image.close()
