"""Original offline insert/reaction artwork on the shared 1920 × 1080 artboard.

``subject`` and ``emotion`` are required shot fields. Anchor/reason are editorial
metadata, never quoted as dialogue. Scene 11 opens the envelope; scene 14 repairs
the apron. An explicit sealed/torn anchor can show the preceding state.
Optional ``detail`` selects a validated subject-specific action from DETAILS.
"""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw

from .illustrations import (
    CLAY, CREAM, GOLD, GREY, INK, PALE, SKIN, TAGS, TEAL,
    _art, _font, _person, _save, _text, _window,
)

SUBJECTS = (
    "wide", "mother", "son", "hands", "keys", "phone", "schedule",
    "payslip", "envelope", "sewing", "apron", "room", "bus", "calendar",
)
EMOTIONS = ("concerned", "neutral", "softened")
DETAILS = {
    "hands": ("clasped_hands", "calendar_point", "bag_handle", "unpick_seam",
              "order_note", "key_handover", "order_complete"),
    "phone": ("message", "timetable", "face_down", "call"),
    "room": ("damp", "dry"),
    "apron": ("fitting", "repaired_flat", "repaired_worn"),
}
CAPTIONS = dict(zip(SUBJECTS, (
    "풍경", "어머니", "아들", "손", "열쇠", "전화", "근무표",
    "근무 내역", "봉투", "바느질", "앞치마", "방 한쪽", "버스 안", "달력",
)))
SHADOW, PAPER, METAL = "#aa987f", "#fff4db", "#bdc5bf"


def _number(scene):
    digits = re.findall(r"\d+", str(scene.get("id", "")))
    return int(digits[-1]) if digits else 0


def _stage(d, tabletop=True):
    d.rectangle((0, 164, 1920, 842), fill="#d7d0bd")
    d.polygon([(0, 164), (730, 164), (1640, 842), (0, 842)], fill="#e9dec5")
    d.polygon([(1490, 164), (1920, 164), (1920, 842), (1680, 842)], fill="#b8b8aa")
    if tabletop:
        d.polygon([(0, 590), (1920, 540), (1920, 842), (0, 842)], fill="#bc9d79")
        for y in (646, 731, 815):
            d.line((0, y, 1920, y - 42), fill="#b39472", width=3)


def _paper(d, box):
    x, y, r, b = box
    d.polygon([(x + 18, y + 20), (r + 25, y + 12),
               (r + 33, b + 22), (x + 8, b + 26)], fill=SHADOW)
    d.rectangle(box, fill=PAPER)
    d.line((x + 4, y + 3, r - 4, y + 3), fill=CREAM, width=4)


def _key(d, x, y, tilt=0):
    """A bored metal bow, shaft and asymmetrical cut teeth, not a key icon."""
    d.ellipse((x + 10, y + 16, x + 150, y + 156), fill=SHADOW)
    d.ellipse((x, y, x + 140, y + 140), fill=METAL, outline=INK, width=5)
    d.ellipse((x + 36, y + 30, x + 104, y + 98), fill="#b99d7c",
              outline="#7b8581", width=6)
    points = [(x + 99, y + 109), (x + 128, y + 88),
              (x + 348, y + 306 + tilt), (x + 321, y + 335 + tilt),
              (x + 287, y + 304 + tilt), (x + 264, y + 324 + tilt),
              (x + 240, y + 298 + tilt), (x + 264, y + 278 + tilt),
              (x + 230, y + 242 + tilt), (x + 209, y + 260 + tilt),
              (x + 190, y + 239 + tilt), (x + 211, y + 218 + tilt)]
    d.polygon(points, fill=METAL, outline=INK)
    d.line((x + 119, y + 115, x + 315, y + 310 + tilt), fill=CREAM, width=5)
    d.line((x + 105, y + 124, x + 288, y + 298 + tilt), fill="#7b8581", width=3)


def _hand(d, left=True):
    if left:
        d.polygon([(0, 480), (470, 500), (730, 594), (691, 724), (0, 750)], fill=CLAY)
        d.polygon([(651, 581), (777, 574), (916, 524), (943, 544),
                   (858, 609), (1104, 624), (1130, 650), (1105, 676),
                   (865, 715), (722, 710), (679, 681)], fill=SKIN)
        for y in (649, 670, 688):
            d.line((865, y, 1070, y - 9), fill="#bd886c", width=3)
        d.line((642, 579, 688, 716), fill=GOLD, width=5)
    else:
        d.polygon([(1920, 437), (1420, 496), (1235, 595), (1328, 713),
                   (1920, 661)], fill=INK)
        d.polygon([(1289, 568), (1160, 550), (1050, 574), (1037, 601),
                   (1118, 608), (989, 650), (979, 680), (1009, 699),
                   (1199, 661), (1331, 667)], fill=SKIN)
        for x in (1045, 1090, 1135):
            d.line((x, 652, x + 11, 679), fill="#bd886c", width=3)
        d.line((1302, 556, 1350, 665), fill=CREAM, width=8)


def _documents(d, subject):
    _paper(d, (600, 225, 1470, 770))
    if subject == "payslip":
        d.text((664, 269), "근무 내역", font=_font(44, True), fill=INK)
        d.line((660, 342, 1408, 342), fill=INK, width=3)
        for i in range(4):
            y = 391 + i * 77
            d.text((671, y), "근무일", font=_font(28), fill=INK)
            d.line((841, y + 22, 1130 - i * 38, y + 22), fill=SHADOW, width=7)
            d.line((1230, y + 22, 1382, y + 22), fill=PALE, width=7)
            d.line((662, y + 53, 1404, y + 53), fill=PALE, width=2)
        return
    d.text((655, 253), "근무표" if subject == "schedule" else "달력",
           font=_font(40, True), fill=INK)
    x, y, w, h = 653, 380, 108, 74
    for i, day in enumerate("월화수목금토일"):
        d.text((x + i * w + 35, 337), day, font=_font(25), fill=INK)
    for row in range(4):
        for col in range(7):
            box = (x + col * w, y + row * h, x + (col + 1) * w, y + (row + 1) * h)
            d.rectangle(box, outline=SHADOW, width=2)
    if subject == "schedule":
        # Faded five-day previous week versus the three retained workdays.
        d.text((604, y + 23), "이전", font=_font(20), fill=SHADOW)
        d.text((604, y + h + 23), "이번", font=_font(20), fill=TEAL)
        for col in range(5):
            for dx in range(12, 90, 18):
                d.line((x + col * w + dx, y + 33, x + col * w + dx + 9, y + 33),
                       fill=SHADOW, width=5)
        for col in (0, 2, 4):
            d.rectangle((x + col * w + 10, y + h + 10,
                         x + (col + 1) * w - 10, y + 2 * h - 10), fill=TEAL)
            d.line((x + col * w + 27, y + h + 38,
                    x + col * w + 80, y + h + 38), fill=CREAM, width=5)
    else:
        d.ellipse((x + 2 * w + 9, y + h + 7, x + 3 * w - 8, y + 2 * h - 7),
                  outline=CLAY, width=7)
        d.arc((x + 2 * w + 4, y + h + 4, x + 3 * w - 5, y + 2 * h - 9),
              50, 260, fill=CLAY, width=3)
        d.line((1444, 706, 1680, 422), fill=INK, width=15)
        d.polygon([(1444, 706), (1432, 730), (1457, 715)], fill=GOLD)


def _stitched_panel(d, box=(865, 630, 1130, 720)):
    x, y, r, b = box
    d.rectangle(box, fill="#71938a", outline=PALE, width=3)
    for px in range(x + 9, r - 8, 17):
        d.line((px, y + 8, px + 7, y + 8), fill=CREAM, width=3)
        d.line((px, b - 8, px + 7, b - 8), fill=CREAM, width=3)
    for py in range(y + 16, b - 12, 17):
        for px in (x + 8, r - 8):
            d.line((px, py, px, py + 7), fill=CREAM, width=3)


def _hands(d, detail):
    if detail == "key_handover":
        _hand(d)
        _key(d, 878, 355)
        _key(d, 1015, 346, -24)
        d.arc((875, 303, 1142, 443), 150, 365, fill=METAL, width=9)
        _hand(d, False)
    elif detail == "clasped_hands":
        d.polygon([(493, 842), (561, 279), (1340, 279), (1450, 842)], fill=CLAY)
        d.line((674, 439, 768, 670), fill="#a75643", width=112)
        d.line((1230, 446, 1108, 671), fill="#a75643", width=112)
        d.ellipse((747, 580, 1042, 741), fill=SKIN)
        d.ellipse((900, 569, 1170, 718), fill=SKIN)
        for i in range(4):
            d.arc((872 + i * 29, 594, 937 + i * 29, 717), 210, 350,
                  fill="#bd886c", width=4)
        d.line((903, 591, 1035, 646), fill="#bd886c", width=4)
    elif detail == "calendar_point":
        _documents(d, "calendar")
        d.text((893, 454), "?", font=_font(46, True), fill=CLAY)
        d.polygon([(1920, 633), (1380, 605), (1310, 725), (1920, 841)], fill=CLAY)
        d.polygon([(1340, 614), (1190, 571), (946, 505), (924, 519),
                   (941, 540), (1111, 596), (1098, 658), (1240, 720),
                   (1354, 701)], fill=SKIN)
        for y in (618, 639, 660):
            d.line((1148, y, 1250, y + 31), fill="#bd886c", width=3)
    elif detail == "bag_handle":
        d.rounded_rectangle((710, 482, 1390, 811), 38, fill="#887a63")
        d.polygon([(796, 434), (1244, 451), (1290, 651), (783, 630)], fill=TEAL)
        d.line((805, 457, 1226, 478), fill=CREAM, width=13)
        d.rectangle((927, 502, 1140, 609), outline=PALE, width=4)
        d.arc((880, 331, 1210, 716), 180, 360, fill=INK, width=24)
        d.polygon([(590, 164), (755, 164), (1060, 332), (1000, 429)], fill=INK)
        d.rounded_rectangle((973, 321, 1110, 426), 30, fill=SKIN)
        for x in range(991, 1080, 24):
            d.line((x, 349, x + 13, 401), fill="#bd886c", width=3)
    elif detail == "unpick_seam":
        d.polygon([(478, 480), (1320, 348), (1510, 810), (620, 831)], fill=TEAL)
        d.line((660, 691, 1230, 490), fill="#315c5a", width=5)
        for x in range(725, 1050, 29):
            y = 691 - (x - 660) * .35
            d.line((x - 4, y - 12, x + 6, y + 13), fill=CREAM, width=3)
        d.arc((1037, 480, 1105, 565), 30, 260, fill=CREAM, width=3)
        _hand(d, False)
        d.line((1280, 447, 1150, 509), fill=CLAY, width=34)
        d.line((1150, 509, 1080, 536), fill=METAL, width=9)
        d.line([(1080, 536), (1053, 518), (1067, 547), (1094, 550)],
               fill=METAL, width=5)
        d.ellipse((1060, 540, 1073, 553), fill=CLAY)
        d.polygon([(1430, 299), (1300, 400), (1260, 488), (1200, 487),
                   (1180, 457), (1235, 395), (1390, 252)], fill=SKIN)
    else:
        _paper(d, (641, 285, 1355, 789))
        d.text((707, 322), "수선 접수", font=_font(42, True), fill=INK)
        for y in (437, 520, 603, 686):
            d.line((710, y, 1279, y), fill=PALE, width=3)
        if detail == "order_complete":
            for y in (399, 482, 565):
                d.rectangle((728, y, 763, y + 29), outline=TEAL, width=3)
                d.line([(733, y + 12), (745, y + 23), (773, y - 7)], fill=TEAL, width=5)
            d.line([(1085, 661), (1135, 711), (1250, 574)], fill=TEAL, width=14)
        else:
            _hand(d, False)
            d.line((1228, 541, 968, 704), fill=INK, width=13)
            d.polygon([(968, 704), (951, 722), (979, 714)], fill=GOLD)
            d.line((851, 719, 946, 719), fill=INK, width=3)


def _apron(d, detail):
    worn = detail != "repaired_flat"
    if worn:
        _stage(d, False)
        _person(d, 1010, 64, mother=False, scale=3.4)
    cloth = [(624, 398), (807, 339), (1194, 359), (1420, 792), (576, 809)]
    d.polygon(cloth, fill=TEAL)
    d.line(cloth + [cloth[0]], fill="#315c5a", width=10)
    d.line([(807, 346), (845, 239), (1150, 239), (1194, 359)], fill=CREAM, width=24)
    d.line((621, 469, 1280, 440), fill=CREAM, width=12)
    d.rectangle((856, 479, 1170, 623), outline=PALE, width=5)
    for x in range(700, 1250, 95):
        d.line((x, 471, x + 48, 740), fill="#598783", width=3)
    if detail == "fitting":
        d.line([(875, 684), (928, 669), (967, 686), (1041, 662)], fill="#315c5a", width=13)
        d.ellipse((628, 490, 758, 567), fill=SKIN)
        d.ellipse((1210, 476, 1338, 553), fill=SKIN)
    else:
        _stitched_panel(d)
    if detail == "repaired_worn":
        d.polygon([(1080, 522), (1480, 495), (1590, 560), (1190, 596)], fill="#d1b180")
        d.polygon([(1080, 522), (1190, 596), (1190, 810), (1080, 732)], fill="#a7855d")
        d.polygon([(1190, 596), (1590, 560), (1590, 773), (1190, 810)], fill="#bc9a6d")
        d.line((1265, 510, 1370, 580, 1370, 790), fill=PALE, width=18)
        d.ellipse((1041, 631, 1145, 709), fill=SKIN)
        d.ellipse((1538, 624, 1642, 701), fill=SKIN)


def _interior(d, son, expression, bus=False):
    _stage(d, False)
    _window(d, (150, 214, 1020, 660), night=False)
    if not bus:
        for x in range(200, 1010, 110):
            d.line((x, 267, x - 30, 340), fill=PALE, width=3)
            d.line((x + 20, 456, x - 10, 529), fill=PALE, width=3)
    _person(d, 1260, 300, mother=not son, pose="hold", scale=1.16, expression=expression)
    if bus:
        d.rounded_rectangle((960, 602, 1560, 842), 44, fill=TEAL)
        d.line((1610, 166, 1610, 842), fill=GOLD, width=18)
        d.line((150, 185, 1770, 185), fill=INK, width=17)
        for x in (500, 800):
            d.line((x, 185, x, 235), fill=INK, width=6)
            d.rounded_rectangle((x - 34, 225, x + 34, 295), 18, outline=INK, width=8)
        d.polygon([(0, 730), (490, 720), (620, 842), (0, 842)], fill=INK)
    else:
        d.polygon([(740, 692), (1800, 654), (1920, 842), (670, 842)], fill="#b39876")
        _paper(d, (1030, 701, 1290, 796))
        for y in (727, 749, 771):
            d.line((1060, y, 1245, y), fill=SHADOW, width=3)


def _insert(d, scene, shot):
    subject, anchor = shot["subject"], str(shot.get("anchor", "")).lower()
    detail = shot.get("detail")
    _stage(d)
    if subject in ("mother", "son"):
        _stage(d, False)
        _window(d, (40, 190, 620, 739), night=False)
        expression = {"softened": "smile"}.get(shot["emotion"], shot["emotion"])
        _person(d, 1140, 267, mother=subject == "mother", scale=3.6, expression=expression)
        d.polygon([(0, 774), (490, 728), (672, 842), (0, 842)], fill="#8e9b90")
    elif subject in ("schedule", "payslip", "calendar"):
        _documents(d, subject)
    elif subject == "keys":
        d.ellipse((666, 648, 1435, 751), fill=SHADOW)
        _key(d, 768, 324)
        if _number(scene) == 12:
            _key(d, 1096, 351, -38)
            d.arc((766, 254, 1217, 454), 150, 365, fill=INK, width=12)
            d.arc((769, 250, 1220, 450), 150, 365, fill=METAL, width=7)
    elif subject == "hands":
        defaults = {5: "calendar_point", 6: "bag_handle", 8: "unpick_seam",
                    11: "order_note", 12: "key_handover", 14: "order_complete"}
        _hands(d, detail or defaults.get(_number(scene), "clasped_hands"))
    elif subject == "phone":
        d.rounded_rectangle((793, 242, 1161, 816), 48, fill=SHADOW)
        d.rounded_rectangle((763, 211, 1131, 790), 48, fill=INK)
        d.rounded_rectangle((780, 231, 1114, 766), 35, fill="#9cbbb3")
        d.rounded_rectangle((875, 245, 1013, 263), 9, fill=INK)
        if detail == "face_down":
            d.rounded_rectangle((780, 231, 1114, 766), 35, fill="#33484b")
            d.rounded_rectangle((804, 253, 879, 360), 17, fill=INK)
            for y in (279, 330):
                d.ellipse((822, y - 14, 854, y + 18), fill=METAL)
                d.ellipse((830, y - 6, 847, y + 11), fill=INK)
            _paper(d, (1204, 475, 1517, 715))
            d.polygon([(1204, 475), (1360, 541), (1517, 475)], fill=CREAM)
            d.line((1360, 541, 1360, 715), fill=PALE, width=3)
            return
        if detail == "message":
            d.rounded_rectangle((821, 348, 1072, 517), 22, fill=CREAM)
            d.polygon([(847, 510), (847, 553), (893, 510)], fill=CREAM)
            for x in (879, 947, 1015):
                d.ellipse((x - 9, 426, x + 9, 444), fill=TEAL)
            return
        if detail == "timetable":
            for y in (338, 540):
                d.rounded_rectangle((805, y, 1090, y + 157), 13, fill=CREAM)
                d.rounded_rectangle((827, y + 30, 902, y + 113), 9, fill=TEAL)
                d.rectangle((837, y + 43, 892, y + 79), fill=CREAM)
                for x in (836, 879):
                    d.ellipse((x, y + 101, x + 13, y + 119), fill=INK)
                d.line((936, y + 58, 1061, y + 58), fill=SHADOW, width=5)
                d.line((936, y + 93, 1021, y + 93), fill=SHADOW, width=5)
            return
        d.ellipse((877, 329, 1013, 465), fill=CREAM)
        d.arc((910, 364, 980, 430), 25, 155, fill=TEAL, width=17)
        for x, y in ((908, 376), (962, 376)):
            d.rounded_rectangle((x, y, x + 19, y + 32), 6, fill=TEAL)
        for margin in (22, 42):
            d.arc((877 - margin, 329 - margin, 1013 + margin, 465 + margin),
                  310, 45, fill=CREAM, width=3)
        d.ellipse((828, 649, 896, 717), fill=CLAY)
        d.ellipse((995, 649, 1063, 717), fill=TEAL)
        d.line((845, 682, 879, 682), fill=CREAM, width=5)
        d.line([(1013, 681), (1025, 694), (1049, 673)], fill=CREAM, width=5)
    elif subject == "envelope":
        opened = _number(scene) == 11 and not any(s in anchor for s in ("sealed", "봉인"))
        if "unsealed" in anchor or "개봉" in anchor:
            opened = True
        if opened:
            d.polygon([(619, 444), (1000, 204), (1390, 444)], fill=CREAM, outline=SHADOW)
            _paper(d, (697, 329, 1320, 656))
        _paper(d, (610, 453 if opened else 348, 1400, 752))
        y = 453 if opened else 348
        d.line([(610, 752), (1000, 524), (1400, 752)], fill=SHADOW, width=4)
        d.polygon([(610, y), (1000, 654), (1400, y)], fill=CREAM, outline=SHADOW)
        if not opened:
            d.rounded_rectangle((970, 628, 1030, 671), 4, fill=CLAY)
    elif subject == "apron":
        repaired = _number(scene) == 14 and "torn" not in anchor and "찢" not in anchor
        _apron(d, detail or ("repaired_worn" if repaired else "fitting"))
    elif subject == "sewing":
        cloth = [(624, 398), (807, 339), (1194, 359), (1420, 752), (576, 809)]
        d.polygon(cloth, fill=TEAL)
        d.line(cloth + [cloth[0]], fill="#315c5a", width=10)
        for x in range(700, 1250, 95):
            d.line((x, 471, x + 48, 740), fill="#598783", width=3)
        seam = [(860, 686), (910, 667), (938, 687), (982, 662), (1017, 679), (1100, 644)]
        d.line(seam, fill=INK, width=6)
        for x, y in seam:
            d.line((x - 8, y - 15, x + 9, y + 15), fill=CREAM, width=3)
        d.rounded_rectangle((936, 190, 1549, 289), 25, fill=TEAL)
        d.rounded_rectangle((1324, 233, 1498, 651), 20, fill=TEAL)
        d.rounded_rectangle((839, 202, 1016, 465), 24, fill=INK)
        d.rectangle((930, 440, 948, 658), fill=METAL)
        d.line((939, 585, 939, 684), fill=CREAM, width=3)
        d.line((905, 665, 981, 665), fill=INK, width=12)
        d.line([(1070, 174), (875, 243), (939, 635)], fill=GOLD, width=3)
        d.ellipse((1370, 305, 1558, 493), fill=INK, outline=METAL, width=9)
    elif subject == "room":
        _stage(d, False)
        d.polygon([(1250, 164), (1920, 164), (1920, 842), (1250, 718)], fill="#b7b6a8")
        d.line((1250, 164, 1250, 718, 1920, 842), fill="#989c90", width=4)
        if detail == "damp" or (detail is None and _number(scene) != 13):
            for x, y, r in ((1215, 630, 40), (1230, 666, 65), (1275, 700, 55), (1194, 708, 28)):
                d.ellipse((x - r, y - r, x + r, y + r), fill="#a7ae9e")
        _window(d, (207, 234, 702, 569))
        d.polygon([(140, 701), (918, 656), (1060, 841), (100, 841)], fill=INK)
        d.polygon([(167, 689), (900, 652), (964, 772), (142, 800)], fill=PALE)
        d.rounded_rectangle((250, 670, 485, 745), 24, fill=CREAM)
    elif subject == "bus":
        son = str(scene.get("viewpoint", scene.get("pov", "mother"))).lower() in ("son", "아들")
        _interior(d, son, {"softened": "smile"}.get(shot["emotion"], shot["emotion"]), bus=True)


def render_shot(scene: dict, shot: dict, episode: dict, output_path: Path,
                size: tuple[int, int] = (1920, 1080)) -> None:
    """Render a distinct shot; the lower 22% remains free of artwork/text."""
    subject, emotion = shot.get("subject"), shot.get("emotion")
    if subject not in SUBJECTS:
        raise ValueError(f"Unknown shot subject: {subject!r}")
    if emotion not in EMOTIONS:
        raise ValueError(f"Unknown shot emotion: {emotion!r}")
    if "detail" in shot and shot["detail"] not in DETAILS.get(subject, ()):
        raise ValueError(f"Unknown shot detail for {subject}: {shot['detail']!r}")
    image = Image.new("RGB", (1920, 1080), CREAM)
    d = ImageDraw.Draw(image)
    son = str(scene.get("viewpoint", scene.get("pov", "mother"))).lower() in ("son", "아들")
    if subject == "wide":
        tag = scene.get("visual_tag", scene.get("visual", "window"))
        if isinstance(tag, dict):
            tag = tag.get("tag", "window")
        if tag not in TAGS:
            raise ValueError(f"Unknown illustration tag: {tag}")
        if son and tag in ("rain", "window"):
            _interior(d, True, "concerned")
        else:
            _art(d, tag)
    else:
        _insert(d, scene, shot)
    d.rectangle((0, 0, 1920, 164), fill=CREAM)
    _text(d, f"{scene.get('id', '')}  {scene.get('title', '')}".strip(),
          (74, 42), 1320, size=46)
    d.rounded_rectangle((1480, 36, 1850, 113), 38, fill=TEAL if son else CLAY)
    _text(d, "아들의 기억" if son else "어머니의 기억", (1516, 53), 310, 1, 44, CREAM)
    d.text((76, 194), CAPTIONS[subject], font=_font(25), fill=INK,
           stroke_width=1, stroke_fill=CREAM)
    d.rectangle((0, 842, 1920, 1080), fill=INK)
    _save(image, output_path, size)
