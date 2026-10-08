from __future__ import annotations

import importlib.util
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from story_pipeline import fonts
from story_pipeline.media import write_ass
from story_pipeline.models import PipelineError


class FontTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent / f".fonts-{uuid4().hex}"
        self.root.mkdir()
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))

    def test_environment_override_wins_and_bold_prefers_bold_variable(self):
        regular, bold = self.root / "regular.ttf", self.root / "bold.ttf"
        regular.write_bytes(b"x")
        bold.write_bytes(b"x")
        environ = {"STORY_FONT": str(regular), "STORY_FONT_BOLD": str(bold)}
        self.assertEqual(fonts.korean_font(environ=environ), regular)
        self.assertEqual(fonts.korean_font(True, environ=environ), bold)
        self.assertEqual(fonts.korean_font(True, environ={"STORY_FONT": str(regular)}), regular)

    def test_missing_configured_font_is_explicit(self):
        with self.assertRaisesRegex(PipelineError, "STORY_FONT"):
            fonts.korean_font(environ={"STORY_FONT": str(self.root / "absent.ttf")})

    def test_no_font_anywhere_raises(self):
        with patch.object(Path, "is_file", return_value=False):
            with self.assertRaisesRegex(PipelineError, "Korean font missing"):
                fonts.korean_font(environ={})

    def test_candidate_order_is_windows_then_noto_then_nanum(self):
        names = [path.name for path in fonts.font_candidates(False, {})]
        self.assertEqual(names[0], "malgun.ttf")
        self.assertLess(names.index("NotoSansCJK-Regular.ttc"), names.index("NanumGothic.ttf"))
        self.assertEqual(fonts.font_candidates(True, {})[0].name, "malgunbd.ttf")

    @unittest.skipUnless(Path(r"C:\Windows\Fonts\malgun.ttf").is_file() and importlib.util.find_spec("PIL"),
                         "Malgun Gothic and Pillow required")
    def test_windows_family_matches_previous_ass_style(self):
        self.assertEqual(fonts.font_family(fonts.korean_font(environ={})), "Malgun Gothic")

    def test_ass_font_name_is_configurable_and_defaults_to_malgun(self):
        cues = [{"start": 0, "end": 1, "text": "안녕"}]
        write_ass(self.root / "a.ass", cues)
        write_ass(self.root / "b.ass", cues, font_name="Noto Sans CJK KR")
        self.assertIn("Style: Narration,Malgun Gothic,40", (self.root / "a.ass").read_text(encoding="utf-8-sig"))
        self.assertIn("Style: Notice,Noto Sans CJK KR,18", (self.root / "b.ass").read_text(encoding="utf-8-sig"))


if __name__ == "__main__":
    unittest.main()
