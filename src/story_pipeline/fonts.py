"""Portable Korean font resolution for artwork and burned-in subtitles."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from .models import PipelineError

WINDOWS = Path(r"C:\Windows\Fonts")
NOTO_CJK = Path("/usr/share/fonts/opentype/noto")
NANUM = Path("/usr/share/fonts/truetype/nanum")
NOTO_KR = Path("/usr/share/fonts/truetype/noto")


def font_candidates(bold: bool = False, environ: dict[str, str] | None = None) -> list[Path]:
    environ = os.environ if environ is None else environ
    weight = "Bold" if bold else "Regular"
    candidates = [
        WINDOWS / ("malgunbd.ttf" if bold else "malgun.ttf"), WINDOWS / "malgun.ttf",
        NOTO_CJK / f"NotoSansCJK-{weight}.ttc", NOTO_CJK / "NotoSansCJK-Regular.ttc",
        NANUM / ("NanumGothicBold.ttf" if bold else "NanumGothic.ttf"), NANUM / "NanumGothic.ttf",
        NOTO_KR / "NotoSansKR-Regular.ttf",
    ]
    configured = [environ.get(name) for name in (("STORY_FONT_BOLD", "STORY_FONT") if bold else ("STORY_FONT",))]
    return [Path(value) for value in configured if value] + candidates


def korean_font(bold: bool = False, *, environ: dict[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    for name in ("STORY_FONT_BOLD", "STORY_FONT") if bold else ("STORY_FONT",):
        if environ.get(name) and not Path(environ[name]).is_file():
            raise PipelineError(f"{name} points to a missing font file: {environ[name]}")
    for path in font_candidates(bold, environ):
        if path.is_file():
            return path
    raise PipelineError(
        "Korean font missing. Install Malgun Gothic, Noto Sans CJK KR or Nanum Gothic, "
        "or set STORY_FONT (and optionally STORY_FONT_BOLD) to a .ttf/.ttc file."
    )


@lru_cache(maxsize=8)
def font_family(path: Path) -> str:
    """Family name libass matches against the ASS Fontname field."""
    try:
        from PIL import ImageFont
    except ImportError as error:
        raise PipelineError("Install the media extra (Pillow) to read font names.") from error
    return ImageFont.truetype(str(path), 12).getname()[0]
