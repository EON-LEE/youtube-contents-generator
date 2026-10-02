"""Original, offline editorial artwork; all coordinates use a 1920 × 1080 artboard.

Scene dictionaries accept ``visual_tag`` (or ``visual``), ``id``, ``title`` and
``viewpoint``/``pov`` (mother/son or 어머니/아들). Narration belongs to the media renderer.
"""
from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CREAM, INK, CLAY, TEAL = "#f3e7cf", "#172b3a", "#bc684e", "#467c79"
GOLD, PALE, SKIN, GREY = "#d6ad68", "#dfd2b5", "#dcaa89", "#b4b6af"
TAGS = ("door", "kitchen", "letter", "bus", "workshop", "rain", "table",
        "garden", "station", "window")


def _font(size: int, bold: bool = False):
    root = Path(os.sep)
    candidates = [
        Path(r"C:\Windows\Fonts") / ("malgunbd.ttf" if bold else "malgun.ttf"),
        Path(r"C:\Windows\Fonts") / "malgun.ttf",
        root / "usr" / "share" / "fonts" / "opentype" / "noto" / (
            "NotoSansCJK-Bold.ttc" if bold else "NotoSansCJK-Regular.ttc"),
        root / "usr" / "share" / "fonts" / "truetype" / "nanum" / (
            "NanumGothicBold.ttf" if bold else "NanumGothic.ttf"),
        root / "usr" / "share" / "fonts" / "truetype" / "noto" / "NotoSansKR-Regular.ttf",
    ]
    for path in candidates:
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    raise RuntimeError(
        "Korean illustration font missing. Install Malgun Gothic, "
        "Noto Sans CJK, Noto Sans KR, or Nanum Gothic in a supported font path."
    )


def _wrap(draw, text, font, width):
    """Wrap every character (including Korean) without losing title content."""
    lines = []
    for paragraph in str(text).split("\n"):
        line = ""
        for char in paragraph:
            if draw.textbbox((0, 0), char, font=font)[2] > width:
                raise ValueError("Text area is narrower than one character.")
            trial = line + char
            bounds = draw.textbbox((0, 0), trial, font=font)
            if bounds[2] - bounds[0] > width:
                lines.append(line)
                line = char
            else:
                line = trial
        lines.append(line)
    return lines


def _text(draw, text, xy, width, max_lines=2, size=48, fill=INK):
    for points in range(size, 39, -2):
        font = _font(points, True)
        lines = _wrap(draw, text, font, width)
        if len(lines) <= max_lines:
            for index, line in enumerate(lines):
                draw.text((xy[0], xy[1] + index * (points + 14)), line,
                          font=font, fill=fill, anchor="lt")
            return
    raise ValueError("Title cannot fit without truncation; use a shorter title.")


def _window(d, box=(170, 190, 740, 640), night=False):
    x, y, r, b = box
    d.rectangle((x - 18, y - 18, r + 18, b + 20), fill=INK)
    d.rectangle(box, fill=TEAL if night else "#b6cfcb")
    d.ellipse((r - 150, y + 35, r - 50, y + 135), fill=CREAM if night else GOLD)
    d.polygon([(x, b), (x + 120, b - 135), (x + 250, b - 70),
               (r - 80, b - 170), (r, b - 110), (r, b)], fill="#779b91")
    d.line((x, y + (b - y) // 2, r, y + (b - y) // 2), fill=CREAM, width=14)
    d.line(((x + r) // 2, y, (x + r) // 2, b), fill=CREAM, width=14)
    d.polygon([(x, b + 20), (r, b + 20), (r + 310, 835), (x + 70, 835)],
              fill="#ebd9b5")


def _envelope(d, x, y, scale=1):
    w, h = int(210 * scale), int(130 * scale)
    d.rounded_rectangle((x + 8, y + 10, x + w + 8, y + h + 10), 6, fill="#ad9276")
    d.rectangle((x, y, x + w, y + h), fill=CREAM, outline=INK, width=3)
    d.line([(x, y), (x + w // 2, y + h * .58), (x + w, y)], fill=CLAY, width=4)
    d.ellipse((x + w * .46, y + h * .46, x + w * .56, y + h * .62), fill=CLAY)


def _key(d, x, y):
    d.ellipse((x, y, x + 36, y + 36), outline=GOLD, width=9)
    d.line((x + 28, y + 29, x + 79, y + 77), fill=GOLD, width=10)
    d.line((x + 56, y + 56, x + 43, y + 69), fill=GOLD, width=9)
    d.line((x + 71, y + 70, x + 58, y + 83), fill=GOLD, width=9)


def _person(d, x, y, mother=True, pose="rest", scale=1, expression="neutral"):
    """Soft silhouettes, shaped hair, expressive hands, and consistent faces."""
    def box(a, b, c, e):
        return (x + a * scale, y + b * scale, x + c * scale, y + e * scale)
    def pts(values):
        return [(x + a * scale, y + b * scale) for a, b in values]
    coat, hair = (CLAY, GREY) if mother else (INK, "#29383c")
    d.ellipse(box(-120, 438, 135, 470), fill="#b8a78a")
    d.polygon(pts([(-72, 245), (69, 245), (86, 432), (23, 432),
                   (0, 321), (-17, 432), (-78, 432)]), fill=INK)
    d.rounded_rectangle(box(-88, 117, 89, 302), int(40 * scale), fill=coat)
    d.polygon(pts([(-29, 117), (30, 117), (17, 268), (-10, 268)]), fill=CREAM)
    d.rounded_rectangle(box(-22, 80, 25, 139), int(12 * scale), fill=SKIN)
    # Hair stops at the temples; the lower face and chin remain uncovered.
    d.ellipse(box(-56, -20, 55, 72), fill=hair)
    d.ellipse(box(-53, 45, -36, 73), fill=SKIN)
    d.ellipse(box(36, 45, 53, 73), fill=SKIN)
    d.ellipse(box(-46, 1, 47, 117), fill=SKIN)
    d.pieslice(box(-55, -22, 54, 66), 180, 360, fill=hair)
    if mother:
        for a, b in [(-51, 5), (-43, -12), (-24, -20), (0, -21), (24, -11)]:
            d.ellipse(box(a, b, a + 32, b + 32), fill=hair)
        d.arc(box(-35, 59, -10, 77), 10, 130, fill=CLAY, width=2)
        d.arc(box(13, 59, 38, 77), 45, 160, fill=CLAY, width=2)
    else:
        d.polygon(pts([(-52, 20), (-31, -20), (39, -13), (52, 23),
                       (7, 7), (-27, 29)]), fill=hair)
    stroke = max(1, int(2 * scale))
    brows = ([(-34, 43), (-14, 37)], [(15, 37), (35, 43)]) if expression == "concerned" else (
        [(-34, 40), (-14, 40)], [(15, 40), (35, 40)])
    for brow in brows:
        d.line(pts(brow), fill=INK, width=stroke)
    for eye in (-23, 24):
        d.ellipse(box(eye - 3, 50, eye + 3, 57), fill=INK)
    d.line(pts([(4, 58), (9, 77), (1, 79)]), fill="#b97f63", width=stroke)
    if expression == "smile":
        d.arc(box(-13, 83, 17, 96), 10, 165, fill=INK, width=stroke)
    elif expression == "concerned":
        d.arc(box(-13, 91, 17, 104), 195, 345, fill=INK, width=stroke)
    else:
        d.line(pts([(-12, 92), (3, 91), (16, 92)]), fill=INK, width=stroke)
    hand = {"reach": (163, 183), "hold": (52, 240), "stop": (134, 102),
            "work": (156, 256)}.get(pose, (90, 309))
    d.line(pts([(65, 154), (110, 214), hand]), fill=coat, width=int(43 * scale))
    d.ellipse(box(hand[0] - 20, hand[1] - 26, hand[0] + 22, hand[1] + 20), fill=SKIN)
    d.ellipse(box(hand[0] - 25, hand[1] - 6, hand[0] - 9, hand[1] + 18), fill=SKIN)
    d.line(pts([(-65, 156), (-94, 247), (-69, 301)]), fill=coat, width=int(36 * scale))
    d.ellipse(box(-89, 281, -49, 326), fill=SKIN)
    for button in (182, 226, 270):
        d.ellipse(box(26, button, 33, button + 7), fill=GOLD)
    d.rectangle(box(-67, 235, -27, 264), outline=GOLD, width=max(1, int(2 * scale)))


def _plant(d, x, y, height=200):
    d.polygon([(x - 42, y - 62), (x + 43, y - 62), (x + 30, y), (x - 30, y)], fill=CLAY)
    d.line((x, y - 60, x, y - height), fill=INK, width=6)
    for i in range(4):
        v = y - height + i * 36
        side = 1 if i % 2 else -1
        d.ellipse((x + min(0, side * 80), v, x + max(0, side * 80), v + 40), fill=TEAL)


def _table(d, y=646):
    d.rectangle((700, y + 24, 730, 838), fill=INK)
    d.rectangle((1580, y + 24, 1610, 838), fill=INK)
    d.rounded_rectangle((650, y, 1680, y + 35), 12, fill=GOLD)


def _sewing_machine(d):
    d.polygon([(1120, 608), (1355, 608), (1430, 700), (1170, 724)], fill="#99b6ae")
    for y in (636, 652):
        d.line((1160, y, 1370, y), fill=CREAM, width=3)
    d.rounded_rectangle((1190, 624, 1620, 650), 10, fill=INK)
    d.rounded_rectangle((1480, 425, 1560, 629), 18, fill=TEAL)
    d.rounded_rectangle((1210, 416, 1530, 490), 22, fill=TEAL)
    d.rounded_rectangle((1205, 440, 1290, 555), 14, fill=TEAL)
    d.line((1240, 545, 1240, 617), fill=INK, width=5)
    d.line((1225, 619, 1265, 619), fill=INK, width=6)
    d.ellipse((1525, 445, 1615, 535), fill=INK)
    d.ellipse((1544, 464, 1596, 516), fill=CREAM)
    d.line((1415, 380, 1415, 419), fill=INK, width=5)
    d.rounded_rectangle((1400, 382, 1430, 407), 3, fill=CLAY)
    d.line([(1415, 407), (1260, 430), (1240, 545)], fill=GOLD, width=3)
    d.ellipse((1320, 436, 1351, 467), fill=CREAM, outline=INK, width=3)


def _art(d, tag):
    d.rectangle((0, 0, 1920, 845), fill=CREAM)
    d.rectangle((0, 700, 1920, 845), fill=PALE)
    if tag == "door":
        _window(d, (140, 235, 510, 610))
        d.rectangle((1050, 160, 1530, 837), fill=INK)
        d.polygon([(1080, 186), (1460, 227), (1460, 837), (1080, 837)], fill=TEAL)
        d.rectangle((1115, 268, 1420, 565), outline=PALE, width=7)
        d.ellipse((1400, 580, 1440, 620), fill=GOLD)
        _person(d, 855, 336, pose="reach", expression="concerned")
        _key(d, 1005, 509)
        _person(d, 1628, 326, False, "stop", expression="concerned")
    elif tag in ("kitchen", "table", "letter", "workshop"):
        _window(d, (135, 213, 645, 585))
        if tag == "kitchen":
            d.rectangle((760, 230, 1740, 358), fill=TEAL)
            for x in (770, 1090, 1410):
                d.rectangle((x, 240, x + 310, 349), outline=CREAM, width=3)
            d.rectangle((760, 620, 1770, 830), fill=TEAL)
            _person(d, 1130, 303, pose="work")
            _table(d)
            d.rounded_rectangle((1420, 558, 1550, 644), 24, fill=INK)
            d.arc((1520, 575, 1580, 627), 270, 90, fill=INK, width=12)
            d.arc((1450, 475, 1490, 565), 90, 260, fill=PALE, width=7)
        elif tag == "letter":
            _person(d, 900, 290, pose="hold", expression="concerned")
            _table(d, 708)
            _envelope(d, 1140, 453, 1.8)
            _key(d, 1510, 739)
            _plant(d, 400, 811)
        elif tag == "workshop":
            d.rectangle((1180, 200, 1730, 427), fill=PALE)
            for x in range(1220, 1700, 82):
                d.line((x, 228, x, 304), fill=INK, width=5)
                d.rounded_rectangle((x - 22, 243, x + 22, 289), 5, fill=CLAY if x % 164 else TEAL)
                for y in (251, 262, 273, 284):
                    d.line((x - 20, y, x + 20, y), fill=CREAM, width=2)
            _person(d, 954, 290, pose="work", expression="smile")
            _table(d)
            _sewing_machine(d)
            _envelope(d, 750, 684, .7)
        else:
            _person(d, 778, 292, pose="hold")
            _person(d, 1490, 287, False, "hold")
            _table(d)
            _envelope(d, 1060, 668, .9)
            for x in (884, 1392):
                d.ellipse((x - 30, 625, x + 44, 646), fill=TEAL)
                d.rounded_rectangle((x - 20, 581, x + 33, 635), 9, fill=CREAM, outline=TEAL, width=4)
    elif tag in ("bus", "station", "rain"):
        d.rectangle((0, 170, 1920, 845), fill="#9bb5b1")
        for x, h in ((80, 265), (330, 350), (740, 225), (1390, 380), (1680, 290)):
            d.rectangle((x, h, x + 180, 680), fill=TEAL)
        if tag == "bus":
            d.rounded_rectangle((90, 220, 1820, 662), 50, fill=INK)
            for x in (140, 700, 1260):
                _window(d, (x, 252, x + 490, 615))
            _person(d, 890, 320, pose="hold")
            d.rounded_rectangle((500, 654, 1260, 832), 35, fill=CLAY)
            d.line((1560, 200, 1560, 835), fill=GOLD, width=18)
            _envelope(d, 918, 555, .7)
        elif tag == "station":
            d.rectangle((120, 205, 1800, 246), fill=INK)
            for x in (210, 1670):
                d.rectangle((x, 235, x + 26, 837), fill=INK)
            d.rectangle((200, 725, 1720, 747), fill=GOLD)
            d.ellipse((1370, 270, 1510, 410), fill=CREAM, outline=INK, width=8)
            d.line([(1440, 294), (1440, 340), (1473, 355)], fill=INK, width=6)
            _person(d, 688, 344)
            _person(d, 1130, 320, False)
            d.rounded_rectangle((811, 670, 931, 806), 12, fill=CLAY)
            d.arc((840, 635, 900, 700), 180, 360, fill=INK, width=9)
        else:
            for x in range(70, 1900, 75):
                for y in range(220 + x % 110, 810, 125):
                    d.line((x, y, x - 24, y + 60), fill=PALE, width=3)
            d.ellipse((440, 760, 1270, 820), fill=TEAL)
            _person(d, 926, 364, pose="hold", scale=.92, expression="concerned")
            d.line((1020, 320, 1020, 630), fill=INK, width=9)
            d.pieslice((635, 156, 1405, 692), 180, 360, fill=CLAY)
            d.arc((635, 156, 1405, 692), 180, 360, fill=GOLD, width=5)
    elif tag == "garden":
        d.ellipse((1180, 193, 1400, 413), fill=GOLD)
        d.polygon([(0, 544), (370, 428), (850, 570), (1510, 441), (1920, 519), (1920, 845), (0, 845)], fill="#9eafa0")
        d.polygon([(740, 845), (1080, 540), (1200, 540), (1260, 845)], fill=CREAM)
        for x, y in ((235, 765), (460, 640), (1510, 725), (1700, 825)):
            _plant(d, x, y, 240)
        _person(d, 872, 322, pose="work", expression="smile")
        d.rounded_rectangle((1000, 590, 1120, 680), 18, fill=TEAL)
        d.polygon([(1100, 610), (1210, 566), (1120, 646)], fill=TEAL)
    else:
        _window(d, (210, 185, 1190, 648), night=True)
        _person(d, 1410, 315, pose="hold", expression="concerned")
        _plant(d, 300, 830, 190)
        _envelope(d, 930, 704, .8)
        _key(d, 1160, 745)


def _save(image, output_path, size):
    if len(size) != 2 or any(not isinstance(v, int) or v <= 0 for v in size):
        raise ValueError("Image size must contain two positive integers.")
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.resize(size, Image.Resampling.LANCZOS).save(path, format="PNG", optimize=True)


def render_scene(scene: dict, episode: dict, output_path: Path,
                 size: tuple[int, int] = (1920, 1080)) -> None:
    """Render a scene, reserving the lower 22% exclusively for subtitles."""
    tag = scene.get("visual_tag", scene.get("visual", "window"))
    if isinstance(tag, dict):
        tag = tag.get("tag", "window")
    if tag not in TAGS:
        raise ValueError(f"Unknown illustration tag: {tag}")
    image = Image.new("RGB", (1920, 1080), CREAM)
    d = ImageDraw.Draw(image)
    _art(d, tag)
    d.rectangle((0, 0, 1920, 164), fill=CREAM)
    _text(d, f"{scene.get('id', '')}  {scene.get('title', '')}".strip(),
          (74, 42), 1320, size=46)
    son = str(scene.get("viewpoint", scene.get("pov", "mother"))).lower() in ("son", "아들")
    d.rounded_rectangle((1480, 36, 1850, 113), 38, fill=TEAL if son else CLAY)
    _text(d, "아들의 기억" if son else "어머니의 기억", (1516, 53), 310, 1, 44, CREAM)
    d.rectangle((0, 842, 1920, 1080), fill=INK)
    _save(image, output_path, size)


def render_cover(episode: dict, output_path: Path,
                 size: tuple[int, int] = (1920, 1080)) -> None:
    """Render a high-contrast cover with a complete, two/three-line Korean title."""
    image = Image.new("RGB", (1920, 1080), INK)
    d = ImageDraw.Draw(image)
    d.polygon([(1170, 120), (1800, 190), (1800, 950), (1170, 950)], fill=TEAL)
    d.polygon([(1210, 171), (1670, 217), (1670, 950), (1210, 950)], fill=CLAY)
    d.rectangle((1240, 262, 1630, 678), outline=GOLD, width=7)
    d.ellipse((1580, 706, 1623, 749), fill=GOLD)
    d.polygon([(1210, 950), (1668, 950), (1920, 1000), (1060, 1000)], fill=GOLD)
    _envelope(d, 1200, 658, 1.8)
    _key(d, 1620, 838)
    _text(d, "두 사람의 기억", (94, 78), 940, 1, 44, GOLD)
    title = str(episode.get("title") or "바뀐 문 앞에서\n다시 찾은 나")
    _text(d, title, (90, 276), 1010, 3, 112, CREAM)
    d.line((94, 796, 310, 796), fill=CLAY, width=8)
    _text(d, "창작 오디오 드라마", (94, 935), 970, 1, 44, CREAM)
    _save(image, output_path, size)
