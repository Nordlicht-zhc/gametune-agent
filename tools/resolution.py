from __future__ import annotations

import re


RESOLUTION_PRESETS: tuple[str, ...] = (
    "3840 × 2160", "2560 × 1440", "1920 × 1080", "1600 × 900", "1280 × 720",
    "2560 × 1600", "1920 × 1200", "1680 × 1050", "1440 × 900", "1280 × 800",
    "2048 × 1536", "1600 × 1200", "1440 × 1080", "1280 × 960", "1024 × 768",
)


def parse_resolution(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d{3,5})\s*[×xX*]\s*(\d{3,5})\s*", value)
    if not match:
        raise ValueError("分辨率格式应为“宽 × 高”，例如 1920 × 1200")
    width, height = int(match.group(1)), int(match.group(2))
    if not 640 <= width <= 7680 or not 480 <= height <= 4320:
        raise ValueError("分辨率范围应在 640×480 到 7680×4320 之间")
    return width, height


def normalize_resolution(value: str) -> str:
    width, height = parse_resolution(value)
    return f"{width} × {height}"


def aspect_ratio_name(value: str) -> str:
    width, height = parse_resolution(value)
    ratio = width / height
    known = ((16 / 9, "16:9"), (16 / 10, "16:10"), (4 / 3, "4:3"), (5 / 4, "5:4"), (21 / 9, "21:9"))
    match = min(known, key=lambda item: abs(item[0] - ratio))
    return match[1] if abs(match[0] - ratio) <= 0.025 else f"{ratio:.2f}:1"


def lower_resolution_same_aspect(value: str) -> str:
    width, height = parse_resolution(value)
    ratio = width / height
    candidates: list[tuple[int, int]] = []
    for preset in RESOLUTION_PRESETS:
        candidate_width, candidate_height = parse_resolution(preset)
        if abs(candidate_width / candidate_height - ratio) <= 0.025 and candidate_width * candidate_height < width * height:
            candidates.append((candidate_width, candidate_height))
    if candidates:
        target = max(candidates, key=lambda size: size[0] * size[1])
    else:
        scale = max(0.75, 640 / width, 480 / height)
        if scale >= 1:
            raise ValueError("当前分辨率已经没有适合的较低诊断档位")
        target = (round(width * scale / 2) * 2, round(height * scale / 2) * 2)
    if target == (width, height):
        raise ValueError("当前分辨率已经没有适合的较低诊断档位")
    return f"{target[0]} × {target[1]}"
