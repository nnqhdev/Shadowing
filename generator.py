import asyncio
import base64
import io
import json
import logging
import math
import os
import random
import re
import shutil
import subprocess
import threading
import time
import wave
import weakref
import zlib
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx
import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

logger = logging.getLogger(__name__)

_KEY_PARAM_RE = re.compile(r"(key=)[^&\s'\"]+")


def safe_err(exc: BaseException) -> str:
    """Format an exception for logs with API keys in URLs redacted."""
    return f"{type(exc).__name__}: {_KEY_PARAM_RE.sub(r'\1***', str(exc))}"


def _load_default_gemini_key() -> str:
    env_val = os.environ.get("GEMINI_API_KEY", "").strip()
    if env_val:
        return env_val
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip().startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
SAMPLE_RATE = 24000
DEFAULT_GEMINI_KEY = _load_default_gemini_key()
IMAGE_CONCURRENCY = 2
IMAGE_KEY_COOLDOWN_SEC = 600.0

_IMAGE_SEMAPHORES: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)
_image_key_cooldown_until: Dict[str, float] = {}


def _image_semaphore() -> asyncio.Semaphore:
    """One semaphore per running event loop (a module-level Semaphore breaks across loops)."""
    loop = asyncio.get_running_loop()
    sem = _IMAGE_SEMAPHORES.get(loop)
    if sem is None:
        sem = _IMAGE_SEMAPHORES[loop] = asyncio.Semaphore(IMAGE_CONCURRENCY)
    return sem


def _image_key_available(api_key: str) -> bool:
    return time.monotonic() >= _image_key_cooldown_until.get(api_key, 0.0)


def _cool_down_image_key(api_key: str) -> None:
    """Pause Gemini image calls for a key that hit its rate limit (429) instead of disabling it forever."""
    _image_key_cooldown_until[api_key] = time.monotonic() + IMAGE_KEY_COOLDOWN_SEC


def get_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    """Load a system font (Windows / macOS) that supports full Vietnamese diacritics."""
    win_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    win_candidates = (
        ["segoeuib.ttf", "arialbd.ttf", "tahomabd.ttf", "calibrib.ttf"]
        if bold
        else ["segoeui.ttf", "arial.ttf", "tahoma.ttf", "calibri.ttf"]
    )
    for name in win_candidates:
        p = win_fonts / name
        if p.exists():
            return ImageFont.truetype(str(p), size=size)

    mac_font_dirs = [
        Path("/System/Library/Fonts/Supplemental"),
        Path("/Library/Fonts"),
        Path("/System/Library/Fonts"),
    ]
    mac_candidates = (
        ["Arial Bold.ttf", "Tahoma Bold.ttf", "Verdana Bold.ttf", "Arial Unicode.ttf"]
        if bold
        else ["Arial.ttf", "Tahoma.ttf", "Verdana.ttf", "Arial Unicode.ttf"]
    )
    for d in mac_font_dirs:
        for name in mac_candidates:
            p = d / name
            if p.exists():
                return ImageFont.truetype(str(p), size=size)

    return ImageFont.load_default()


def draw_text_with_stroke(
    draw: ImageDraw.ImageDraw,
    pos: tuple[float, float],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: str,
    stroke_fill: str = "#000000",
    stroke_width: int = 4,
    anchor: str = "mm",
    shadow_offset: int = 4,
) -> None:
    x, y = pos
    if shadow_offset > 0:
        draw.text(
            (x + shadow_offset, y + shadow_offset),
            text,
            font=font,
            fill="#000000",
            stroke_width=stroke_width,
            stroke_fill="#000000",
            anchor=anchor,
        )
    draw.text(
        (x, y),
        text,
        font=font,
        fill=fill,
        stroke_width=stroke_width,
        stroke_fill=stroke_fill,
        anchor=anchor,
    )


def wrap_text_lines(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> List[str]:
    words = text.split()
    if not words:
        return [""]
    lines: List[str] = []
    current = words[0]
    for w in words[1:]:
        test = f"{current} {w}"
        bbox = draw.textbbox((0, 0), test, font=font)
        if (bbox[2] - bbox[0]) <= max_width:
            current = test
        else:
            lines.append(current)
            current = w
    lines.append(current)
    if len(lines) >= 2 and len(lines[-1]) < 12:
        prev_words = lines[-2].split()
        if len(prev_words) >= 4:
            move_n = 2 if len(prev_words) >= 5 and len(lines[-1]) <= 4 else 1
            lines[-2] = " ".join(prev_words[:-move_n])
            lines[-1] = " ".join(prev_words[-move_n:]) + " " + lines[-1]
    return lines


def draw_pumpkin_badge(img: Image.Image, center_x: int, center_y: int, radius: int = 34) -> None:
    """Draw a stylized pumpkin icon in top corners like @REACH-l5p."""
    draw = ImageDraw.Draw(img, "RGBA")
    # Stem
    draw.rounded_rectangle(
        [center_x - 5, center_y - radius - 10, center_x + 5, center_y - radius + 6],
        radius=3,
        fill=(67, 160, 71, 255),
        outline=(27, 94, 32, 255),
        width=2,
    )
    # Side ribs
    for dx in (-14, 14):
        draw.ellipse(
            [center_x + dx - 22, center_y - radius + 4, center_x + dx + 22, center_y + radius - 2],
            fill=(230, 108, 20, 255),
            outline=(140, 55, 5, 255),
            width=2,
        )
    # Center rib
    draw.ellipse(
        [center_x - 20, center_y - radius + 2, center_x + 20, center_y + radius],
        fill=(255, 138, 30, 255),
        outline=(140, 55, 5, 255),
        width=2,
    )
    # Eyes (triangles)
    draw.polygon(
        [(center_x - 16, center_y - 4), (center_x - 6, center_y - 4), (center_x - 11, center_y - 14)],
        fill=(255, 235, 59, 255),
    )
    draw.polygon(
        [(center_x + 6, center_y - 4), (center_x + 16, center_y - 4), (center_x + 11, center_y - 14)],
        fill=(255, 235, 59, 255),
    )
    # Smile
    draw.arc(
        [center_x - 16, center_y - 6, center_x + 16, center_y + 18],
        start=15,
        end=165,
        fill=(255, 235, 59, 255),
        width=4,
    )


PART_TRANSITION_LABELS = {
    1: ("PART 1", "English Subtitles"),
    2: ("PART 2", "Vietnamese Subtitles"),
    3: ("PART 3", "No Subtitles"),
}


def render_part_transition_card(
    part_num: int,
    out_path: Path,
    bg_img: Optional[Image.Image] = None,
    aspect: str = "16:9",
) -> None:
    """Render clean English part transition screen in 4K Ultra HD on a pure black background (3840x2160 or 2160x3840)."""
    w, h = (2160, 3840) if aspect == "9:16" else (3840, 2160)
    cx, cy = w // 2, h // 2
    img = Image.new("RGB", (w, h), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    title_en, sub_en = PART_TRANSITION_LABELS.get(part_num, (f"PART {part_num}", "English Subtitles"))
    f_part = get_font(186 if aspect == "9:16" else 222, bold=True)
    f_sub = get_font(132 if aspect == "9:16" else 162, bold=True)

    draw.text((cx, cy - 150), title_en, font=f_part, fill="#FFE600", anchor="mm")
    draw.text((cx, cy + 135), sub_en, font=f_sub, fill="#FFFFFF", anchor="mm")
    img.save(out_path, "PNG", compress_level=1)


def render_intro_card(out_path: Path) -> None:
    """Backward-compatible alias that renders the Part 1 English transition card."""
    render_part_transition_card(1, out_path)


def render_slide_card(
    bg_img: Optional[Image.Image],
    mode: str,
    en_text: str,
    vi_text: str,
    out_path: Path,
    speaker: str = "",
    aspect: str = "16:9",
    badge_text: str = "",
) -> None:
    """
    Render static subtitle card in 4K Ultra HD on a pure black background (#000000):
      - 'part1': pure black background + white English subtitle
      - 'part2': pure black background + white English + orange-yellow (#FFAE00) Vietnamese subtitle
      - 'part3': pure black background
    Supports both 16:9 4K (3840x2160) and 9:16 4K (2160x3840).
    """
    w, h = (2160, 3840) if aspect == "9:16" else (3840, 2160)
    cx, cy = w // 2, h // 2
    base = Image.new("RGB", (w, h), (0, 0, 0))

    if mode == "part3":
        base.save(out_path, "PNG", compress_level=1)
        return

    draw = ImageDraw.Draw(base)
    max_w = 1860 if aspect == "9:16" else 2880

    if badge_text:
        f_badge = get_font(72 if aspect == "9:16" else 78, bold=True)
        bb = draw.textbbox((0, 0), badge_text, font=f_badge)
        bw = (bb[2] - bb[0]) + 132
        bh = 144
        bx0 = cx - bw // 2
        by0 = 330 if aspect == "9:16" else 126
        draw.rounded_rectangle([bx0, by0, bx0 + bw, by0 + bh], radius=72, fill=(38, 166, 91))
        draw.text((cx, by0 + bh // 2), badge_text, font=f_badge, fill="#FFFFFF", anchor="mm")

    display_en = en_text

    if mode == "part1":
        font_en = get_font(132 if aspect == "9:16" else 162, bold=True)
        en_lines = wrap_text_lines(draw, display_en, font_en, max_w)
        line_h = 186 if aspect == "9:16" else 222
        total_h = len(en_lines) * line_h
        start_y = cy - total_h // 2 + line_h // 2
        for idx, line in enumerate(en_lines):
            draw.text((cx, start_y + idx * line_h), line, font=font_en, fill="#FFFFFF", anchor="mm")
    elif mode == "part2":
        font_en = get_font(132 if aspect == "9:16" else 168, bold=True)
        font_vi = get_font(120 if aspect == "9:16" else 156, bold=True)
        en_lines = wrap_text_lines(draw, display_en, font_en, max_w)
        vi_lines = wrap_text_lines(draw, vi_text, font_vi, max_w)
        line_h_en = 180 if aspect == "9:16" else 222
        line_h_vi = 168 if aspect == "9:16" else 210
        gap = 66 if aspect == "9:16" else 72
        total_h = len(en_lines) * line_h_en + gap + len(vi_lines) * line_h_vi
        curr_y = cy - total_h // 2 + line_h_en // 2
        for line in en_lines:
            draw.text((cx, curr_y), line, font=font_en, fill="#FFFFFF", anchor="mm")
            curr_y += line_h_en
        curr_y += gap
        for line in vi_lines:
            draw.text((cx, curr_y), line, font=font_vi, fill="#FFAE00", anchor="mm")
            curr_y += line_h_vi

    base.save(out_path, "PNG", compress_level=1)


def render_teleprompter_frame(
    bg_img: Image.Image,
    mode: str,
    flat_items: List[Dict[str, Any]],
    curr_idx: int,
    progress_ratio: float,
    y_shift: int,
    out_path: Path,
) -> None:
    """
    Render a scrolling Teleprompter + Word-by-Word Karaoke Highlight frame for Shadowing:
      - Top slot: Previous line (dimmed, scrolled up)
      - Center spotlight card: Active line with progressive word highlight + live rhythm bar (+ Vi sub in part2)
      - Bottom slots: Next 1-2 upcoming lines so the learner can read ahead for Shadowing
    """
    base = ImageOps.fit(
        bg_img.convert("RGB"), (1280, 720), method=Image.Resampling.LANCZOS, centering=(0.5, 0.28)
    )
    base = ImageEnhance.Brightness(base).enhance(0.15).convert("RGBA")

    overlay = Image.new("RGBA", (1280, 720), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Top header bar
    header_text = (
        "● PHẦN 1: SHADOWING — CHỮ CHẠY CUỘN (PHỤ ĐỀ ANH)"
        if mode == "part1"
        else "● PHẦN 2: SHADOWING — CHỮ CHẠY CUỘN (SONG NGỮ ANH - VIỆT)"
    )
    draw.rounded_rectangle(
        [290, 22, 990, 68],
        radius=23,
        fill=(12, 18, 32, 220),
        outline=(255, 255, 255, 65),
        width=2,
    )
    f_hdr = get_font(22, bold=True)
    draw.text((640, 45), header_text, font=f_hdr, fill="#FFE135", anchor="mm")

    curr = flat_items[curr_idx]
    sp = curr.get("speaker", "").strip()
    is_tom = sp.lower() in ("tom", "male", "nam", "b")
    accent_hex = "#FFD600" if is_tom else "#00F0FF"
    accent_rgba = (255, 214, 0, 235) if is_tom else (0, 240, 255, 235)

    # 1. Previous line (Top slot - dimmed, scrolling upward)
    if curr_idx > 0:
        prev = flat_items[curr_idx - 1]
        prev_sp = f"{prev['speaker']}: " if prev.get("speaker") else ""
        prev_txt = f"{prev_sp}{prev['en']}"
        f_prev = get_font(26, bold=True)
        prev_lines = wrap_text_lines(draw, prev_txt, f_prev, 1020)[:2]
        py = 118 + y_shift
        for pl in prev_lines:
            if 75 <= py <= 230:
                draw.text((640, py), pl, font=f_prev, fill=(138, 153, 178, 135), anchor="mm")
            py += 34

    # 2. Center Active Spotlight Card (Current turn for Shadowing)
    f_en = get_font(38 if mode == "part1" else 35, bold=True)
    f_vi = get_font(31, bold=True)
    max_w = 1030
    en_lines = wrap_text_lines(draw, curr["en"], f_en, max_w)
    vi_lines = wrap_text_lines(draw, curr["vi"], f_vi, max_w) if mode == "part2" else []

    line_h_en = 50 if mode == "part1" else 46
    line_h_vi = 42
    en_block_h = len(en_lines) * line_h_en
    vi_block_h = (len(vi_lines) * line_h_vi + 18) if vi_lines else 0
    card_h = max(150, en_block_h + vi_block_h + 78)

    center_y = (330 if mode == "part1" else 335) + int(y_shift * 0.65)
    card_top = center_y - card_h // 2
    card_bot = card_top + card_h

    # Glassmorphic card background
    draw.rounded_rectangle(
        [76, card_top, 1204, card_bot],
        radius=22,
        fill=(10, 16, 30, 230),
        outline=accent_rgba,
        width=3,
    )
    # Left glowing accent bar
    draw.rounded_rectangle(
        [76, card_top + 12, 88, card_bot - 12],
        radius=6,
        fill=accent_rgba,
    )

    # Speaker badge & turn counter at top of active card
    sp_label = f"● {sp.upper()}" if sp else "● SHADOWING"
    draw.rounded_rectangle(
        [104, card_top + 12, 265, card_top + 44],
        radius=16,
        fill=accent_rgba,
    )
    f_badge = get_font(20, bold=True)
    draw.text((184, card_top + 28), sp_label, font=f_badge, fill="#08101E", anchor="mm")

    f_hint = get_font(19, bold=True)
    draw.text(
        (1176, card_top + 28),
        f"Câu {curr_idx + 1}/{len(flat_items)} • Nói đuổi theo chữ đổi màu",
        font=f_hint,
        fill=(180, 198, 224, 220),
        anchor="rm",
    )

    # Word-by-word progressive highlight across wrapped English lines
    all_words = curr["en"].split()
    total_w = max(1, len(all_words))
    hi_count = max(1, int(round(total_w * progress_ratio)))

    word_cursor = 0
    curr_y = card_top + 56 + line_h_en // 2
    space_w = draw.textbbox((0, 0), " ", font=f_en)[2]

    for line_str in en_lines:
        l_words = line_str.split()
        # Measure full line width to center horizontally
        full_bbox = draw.textbbox((0, 0), line_str, font=f_en)
        line_w = full_bbox[2] - full_bbox[0]
        cur_x = 640 - line_w / 2.0

        for w_str in l_words:
            is_spoken = word_cursor < hi_count
            w_color = accent_hex if is_spoken else "#FFFFFF"
            w_bbox = draw.textbbox((0, 0), w_str, font=f_en)
            w_w = w_bbox[2] - w_bbox[0]
            draw_text_with_stroke(
                draw,
                (cur_x + w_w / 2.0, curr_y),
                w_str,
                f_en,
                fill=w_color,
                stroke_fill="#000000",
                stroke_width=3,
                anchor="mm",
                shadow_offset=2,
            )
            cur_x += w_w + space_w
            word_cursor += 1
        curr_y += line_h_en

    # Bilingual Vietnamese subtitle inside active card for Part 2
    if mode == "part2" and vi_lines:
        curr_y += 10
        for v_line in vi_lines:
            draw_text_with_stroke(
                draw,
                (640, curr_y),
                v_line,
                f_vi,
                fill="#FFF200",
                stroke_fill="#000000",
                stroke_width=3,
                anchor="mm",
                shadow_offset=2,
            )
            curr_y += line_h_vi

    # Live Shadowing Rhythm Progress Bar at bottom of active card
    bar_x1, bar_x2 = 108, 1172
    bar_y = card_bot - 12
    draw.rounded_rectangle([bar_x1, bar_y - 4, bar_x2, bar_y + 4], radius=4, fill=(255, 255, 255, 45))
    fill_x2 = bar_x1 + max(12, int((bar_x2 - bar_x1) * min(1.0, max(0.05, progress_ratio))))
    draw.rounded_rectangle([bar_x1, bar_y - 4, fill_x2, bar_y + 4], radius=4, fill=accent_rgba)

    # 3. Upcoming lines (Bottom slots - read ahead before scrolling up)
    next_y = max(card_bot + 26, 535) + int(y_shift * 0.85)
    if curr_idx + 1 < len(flat_items):
        nxt = flat_items[curr_idx + 1]
        nxt_sp = f"{nxt['speaker']}: " if nxt.get("speaker") else ""
        f_lbl = get_font(18, bold=True)
        draw.text(
            (640, next_y - 6),
            "▼ SẮP ĐỌC TIẾP (ĐỌC TRƯỚC ĐỂ SHADOWING):",
            font=f_lbl,
            fill=(56, 189, 248, 200),
            anchor="mm",
        )
        next_y += 26
        f_nxt = get_font(29, bold=True)
        nxt_lines = wrap_text_lines(draw, f"{nxt_sp}{nxt['en']}", f_nxt, 1040)[:2]
        for nl in nxt_lines:
            if next_y <= 692:
                draw_text_with_stroke(
                    draw,
                    (640, next_y),
                    nl,
                    f_nxt,
                    fill="#CBD5E1",
                    stroke_fill="#000000",
                    stroke_width=2,
                    anchor="mm",
                    shadow_offset=1,
                )
            next_y += 36
        if mode == "part2" and nxt.get("vi") and next_y <= 696:
            f_nxt_vi = get_font(24, bold=True)
            vi_prev = wrap_text_lines(draw, nxt["vi"], f_nxt_vi, 1020)[0]
            draw.text((640, next_y), vi_prev, font=f_nxt_vi, fill=(255, 242, 0, 155), anchor="mm")
            next_y += 32

    if mode == "part1" and curr_idx + 2 < len(flat_items) and next_y <= 685:
        nxt2 = flat_items[curr_idx + 2]
        nxt2_sp = f"{nxt2['speaker']}: " if nxt2.get("speaker") else ""
        f_nxt2 = get_font(24, bold=True)
        nl2 = wrap_text_lines(draw, f"{nxt2_sp}{nxt2['en']}", f_nxt2, 1020)[0]
        draw.text((640, next_y + 6), nl2, font=f_nxt2, fill=(100, 116, 139, 135), anchor="mm")

    composed = Image.alpha_composite(base, overlay).convert("RGB")
    composed.save(out_path, "PNG")


def split_into_scroll_lines(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int = 1230
) -> List[str]:
    """
    Split a sentence into natural short single-row phrases (breaking on clauses/commas when long,
    then balancing wrapped lines so single-word orphans never appear) matching @REACH-l5p's layout.
    """
    raw_parts = re.split(r"(?<=[,;—])\s+|(?<=[.?!])\s+", text.strip())
    merged_clauses: List[str] = []
    buf = ""
    for part in raw_parts:
        p = part.strip()
        if not p:
            continue
        candidate = f"{buf} {p}".strip() if buf else p
        if len(candidate) < 24:
            buf = candidate
        else:
            merged_clauses.append(candidate)
            buf = ""
    if buf:
        if merged_clauses and len(buf) < 14:
            merged_clauses[-1] = f"{merged_clauses[-1]} {buf}"
        else:
            merged_clauses.append(buf)

    out_lines: List[str] = []
    for clause in merged_clauses or [text.strip()]:
        wrapped = wrap_text_lines(draw, clause, font, max_w)
        # If wrapping created a short 1-2 word orphan on the second line, split the clause near the middle
        if len(wrapped) == 2 and len(wrapped[1]) < 16:
            words = clause.split()
            if len(words) >= 6:
                mid = len(words) // 2
                wrapped = [" ".join(words[:mid]), " ".join(words[mid:])]
        out_lines.extend(wrapped)
    return out_lines or [text.strip()]


def render_scrolling_section_mp4(
    flat_items: List[Dict[str, Any]],
    scene_imgs: Optional[List[Image.Image]],
    mode: str,
    audio_wav: Path,
    out_mp4: Path,
    fps: int = 20,
    aspect: str = "16:9",
) -> None:
    """
    Render continuous upward-scrolling text video in 4K Ultra HD on a pure black background (#000000).
    Supports both 16:9 4K (3840x2160) and 9:16 4K (2160x3840).
    Uses a pre-rendered tall 4K strip + zero-copy NumPy memoryview slicing for maximum sharpness & speed.
    """
    import numpy as np

    w, h = (2160, 3840) if aspect == "9:16" else (3840, 2160)
    cx = w // 2
    dummy_img = Image.new("L", (w, 60), 0)
    dummy_draw = ImageDraw.Draw(dummy_img)

    if aspect == "9:16":
        f_en = get_font(120 if mode == "part1" else 132, bold=True)
        f_vi = get_font(120, bold=True)
        max_w = 1860 if mode == "part1" else 1800
        line_step_p1 = 282
        line_h_en_p2 = 180
        line_h_vi_p2 = 168
        turn_gap_p2 = 420
    else:
        f_en = get_font(150 if mode == "part1" else 168, bold=True)
        f_vi = get_font(156, bold=True)
        max_w = 3240 if mode == "part1" else 2880
        line_step_p1 = 336
        line_h_en_p2 = 222
        line_h_vi_p2 = 210
        turn_gap_p2 = 525

    draw_ops: List[tuple[int, str, ImageFont.FreeTypeFont, str]] = []
    turn_meta: List[Dict[str, Any]] = []
    cur_y = 0
    cur_t = 0.0

    for idx, it in enumerate(flat_items):
        display_en = it["en"]
        dur = max(0.05, float(it.get("duration", 3.0)))
        turn_top = cur_y

        if mode == "part1":
            en_lines = split_into_scroll_lines(dummy_draw, display_en, f_en, max_w)
            for line_str in en_lines:
                draw_ops.append((cur_y, line_str, f_en, "#FFFFFF"))
                cur_y += line_step_p1
        else:
            en_lines = wrap_text_lines(dummy_draw, display_en, f_en, max_w)
            vi_lines = wrap_text_lines(dummy_draw, it.get("vi", ""), f_vi, max_w)
            for line_str in en_lines:
                draw_ops.append((cur_y, line_str, f_en, "#FFFFFF"))
                cur_y += line_h_en_p2
            if vi_lines:
                cur_y += 42
                for v_str in vi_lines:
                    draw_ops.append((cur_y, v_str, f_vi, "#FFAE00"))
                    cur_y += line_h_vi_p2
            cur_y += turn_gap_p2

        turn_meta.append(
            {
                "idx": idx,
                "item": it,
                "top": turn_top,
                "bot": cur_y,
                "t_start": cur_t,
                "t_end": cur_t + dur,
                "dur": dur,
            }
        )
        cur_t += dur

    end_sentinel_y = cur_y
    total_dur = max(0.1, cur_t)

    # Pre-render the entire scrolling transcript once onto a tall 4K strip image
    pad_top = h
    strip_h = pad_top + end_sentinel_y + h + 200
    is_mono_white = (mode == "part1")
    if is_mono_white:
        strip_img = Image.new("L", (w, strip_h), 0)
        strip_draw = ImageDraw.Draw(strip_img)
        for op_y, text_str, font_obj, _ in draw_ops:
            strip_draw.text((cx, pad_top + op_y), text_str, font=font_obj, fill=255, anchor="mm")
        strip_arr = np.asarray(strip_img, dtype=np.uint8)
        pix_fmt_in = "gray"
    else:
        strip_img = Image.new("RGB", (w, strip_h), (0, 0, 0))
        strip_draw = ImageDraw.Draw(strip_img)
        for op_y, text_str, font_obj, color_hex in draw_ops:
            strip_draw.text((cx, pad_top + op_y), text_str, font=font_obj, fill=color_hex, anchor="mm")
        strip_arr = np.asarray(strip_img, dtype=np.uint8)
        pix_fmt_in = "rgb24"

    cmd = [
        FFMPEG_EXE,
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        pix_fmt_in,
        "-s",
        f"{w}x{h}",
        "-r",
        str(fps),
        "-i",
        "-",
        "-i",
        str(audio_wav),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "16",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        "1",
        "-shortest",
        str(out_mp4),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    total_frames = max(1, int(round(total_dur * fps)))
    screen_read_y = (h // 2) if mode == "part1" else int(h * 0.34)
    n_turns = len(turn_meta)
    tm_idx = 0
    max_y0 = strip_h - h

    try:
        for f_idx in range(total_frames):
            t = f_idx / float(fps)
            while tm_idx + 1 < n_turns and t >= turn_meta[tm_idx]["t_end"]:
                tm_idx += 1

            tm = turn_meta[tm_idx]
            u = min(1.0, max(0.0, (t - tm["t_start"]) / tm["dur"]))

            c_top = tm["top"]
            next_top = turn_meta[tm_idx + 1]["top"] if (tm_idx + 1 < n_turns) else end_sentinel_y

            focus_y = c_top + u * (next_top - c_top)
            scroll_offset = focus_y - screen_read_y
            y0 = max(0, min(max_y0, int(round(pad_top + scroll_offset))))

            if proc.stdin:
                proc.stdin.write(memoryview(strip_arr[y0 : y0 + h]))
    finally:
        if proc.stdin:
            proc.stdin.close()
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"FFmpeg 4K scrolling encode failed with code {proc.returncode}")


def render_part3_audio_ring_mp4(
    scene_imgs: Optional[List[Image.Image]],
    scene_durations: List[float],
    audio_wav: Path,
    out_mp4: Path,
    fps: int = 20,
    style: str = "ripple",
    out_vertical_mp4: Optional[Path] = None,
) -> None:
    """
    Render Part 3 (No Subtitles) in 4K Ultra HD using reach_vong_song_am.py's circular audio-reactive
    waveform ring (#FFC531 -> #F26A1B) on a pure black background (#000000).
    Simultaneously outputs 16:9 4K (3840x2160) to out_mp4 and optionally 9:16 4K (2160x3840) to out_vertical_mp4.
    """
    import numpy as np
    import reach_vong_song_am as rvsa

    samples = rvsa.decode_audio(FFMPEG_EXE, str(audio_wav))
    levels, loud = rvsa.compute_levels(samples, fps)
    levels = rvsa.smooth(levels)
    n_frames = max(1, len(levels))

    ring_size = 720
    scaled_ring = 1680
    # Pass RGB tuples (#FFC531 -> #F26A1B) so draw_frame outputs RGBA directly
    color_map_rgb = rvsa.build_color_map(ring_size, (255, 197, 49), (242, 106, 27))

    def _make_cmd(target_mp4: Path, pad_w: int, pad_h: int) -> List[str]:
        return [
            FFMPEG_EXE,
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            f"{ring_size}x{ring_size}",
            "-r",
            str(fps),
            "-i",
            "-",
            "-i",
            str(audio_wav),
            "-vf",
            f"scale={scaled_ring}:{scaled_ring}:flags=bicubic,pad={pad_w}:{pad_h}:(ow-iw)/2:(oh-ih)/2:black",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "16",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "1",
            "-shortest",
            str(target_mp4),
        ]

    proc_h = subprocess.Popen(
        _make_cmd(out_mp4, 3840, 2160),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc_v = (
        subprocess.Popen(
            _make_cmd(out_vertical_mp4, 2160, 3840),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if out_vertical_mp4 is not None
        else None
    )

    try:
        for f_idx in range(n_frames):
            t = f_idx / float(fps)
            rgba = rvsa.draw_frame(
                ring_size,
                levels[f_idx],
                float(loud[f_idx]),
                color_map_rgb,
                style=style,
                t=t,
            )
            alpha = (rgba[:, :, 3:4].astype(np.float32)) / 255.0
            rgb_box = np.clip(rgba[:, :, :3].astype(np.float32) * alpha, 0, 255).astype(np.uint8)
            raw_view = memoryview(rgb_box)

            if proc_h.stdin:
                proc_h.stdin.write(raw_view)
            if proc_v is not None and proc_v.stdin:
                proc_v.stdin.write(raw_view)
    finally:
        if proc_h.stdin:
            proc_h.stdin.close()
        if proc_v is not None and proc_v.stdin:
            proc_v.stdin.close()
        proc_h.wait()
        if proc_v is not None:
            proc_v.wait()
        if proc_h.returncode != 0:
            raise RuntimeError(f"FFmpeg Part 3 16:9 4K audio ring encode failed with code {proc_h.returncode}")
        if proc_v is not None and proc_v.returncode != 0:
            raise RuntimeError(f"FFmpeg Part 3 9:16 4K audio ring encode failed with code {proc_v.returncode}")


def render_tiktok_cut_mp4(
    flat_items: List[Dict[str, Any]],
    level: str,
    work_dir: Path,
    out_vertical_mp4: Path,
    out_horizontal_mp4: Path,
) -> None:
    """
    Build standalone 4K TikTok climax cut videos in both Vertical (9:16, 2160x3840) and Horizontal (16:9, 3840x2160)
    on a pure black background (#000000) with bilingual English (#FFFFFF) + Vietnamese (#FFAE00) subtitles
    and a closing CTA card: 'Xem trọn bài nghe ở kênh YouTube REACH'.
    """
    n = len(flat_items)
    if n <= 5:
        climax_items = list(flat_items)
    else:
        start_i = max(1, (n // 2) - 1)
        climax_items = flat_items[start_i : min(n, start_i + 5)]

    cta_dur = 3.2
    cta_wav = work_dir / "tiktok_cta.wav"
    create_intro_wav(cta_dur, cta_wav, is_transition=True)

    tiktok_audio_wav = work_dir / "tiktok_cut_audio.wav"
    concat_wav_files([it["wav"] for it in climax_items] + [cta_wav], tiktok_audio_wav)

    badge_str = f"✓ Level {level.upper()} • REACH Podcast"
    slides_v: List[tuple[Path, float]] = []
    slides_h: List[tuple[Path, float]] = []

    for i, it in enumerate(climax_items):
        pv = work_dir / f"tk_v_{i:02d}.png"
        ph = work_dir / f"tk_h_{i:02d}.png"
        render_slide_card(
            None, "part2", it["en"], it["vi"], pv, speaker=it["speaker"], aspect="9:16", badge_text=badge_str
        )
        render_slide_card(
            None, "part2", it["en"], it["vi"], ph, speaker=it["speaker"], aspect="16:9", badge_text=badge_str
        )
        slides_v.append((pv, it["duration"]))
        slides_h.append((ph, it["duration"]))

    cta_en = "Watch the full listening lesson on YouTube: REACH"
    cta_vi = "Xem trọn bài nghe ở kênh YouTube REACH"
    cta_v_png = work_dir / "tk_v_cta.png"
    cta_h_png = work_dir / "tk_h_cta.png"
    render_slide_card(None, "part2", cta_en, cta_vi, cta_v_png, speaker="", aspect="9:16", badge_text=badge_str)
    render_slide_card(None, "part2", cta_en, cta_vi, cta_h_png, speaker="", aspect="16:9", badge_text=badge_str)
    slides_v.append((cta_v_png, cta_dur))
    slides_h.append((cta_h_png, cta_dur))

    render_section_mp4(slides_v, tiktok_audio_wav, out_vertical_mp4)
    render_section_mp4(slides_h, tiktok_audio_wav, out_horizontal_mp4)


UNI_PODCAST_THUMBNAIL_MASTER_PROMPT = """You are a YouTube thumbnail generator for the channel "UNI PODCAST" (Vietnamese English-listening podcasts). Each time the user sends ONLY a video title, create one thumbnail image. Do not ask questions.

STEP 1 — Hook: turn the title into a Vietnamese hook of 2–6 words, split into max 3 lines, ALL CAPS, with correct Vietnamese diacritics. Key words go in red, the rest in black.

STEP 2 — Scene: invent a scene that expresses the title's emotion or situation. For EVERY new title you MUST pick a different combination and never reuse the previous one:
- Character: vary gender, age (child to elder), body type, hair style and color, outfit, accessories (glasses, hat, headband...). Never default to the same young man in a blue shirt.
- Expression: describe it through specific facial muscles (eyebrows, eyelids, mouth, cheeks), e.g. one brow raised and mouth twisted, tearful eyes with trembling lips, forced grin through clenched teeth, eyes wide with pinprick pupils. Avoid the generic "surprised/angry/sad".
- Gesture: vary pose and hands (hands on cheeks, covering mouth, pointing at viewer, arms crossed, hugging head, hands on hips, thumbs up shaking...), with 1 prop that tells the story.
- Camera: vary the angle (close-up, 3/4, from below, from above, off-center).
- Small accents: sweat drops, hearts, anger vein, flying papers, flies, sparkles, depending on the emotion.

STEP 3 — Layout and style (always fixed):
YouTube thumbnail 16:9, pure white background, flat 2D cartoon / webtoon meme style, thick black outlines, flat solid colors, no gradients. Character on the left, cropped at chest or waist, large and filling the frame. Right side: huge bold sans-serif hook text, solid flat color, no shadow, no outline. A hand-drawn black curved arrow from the text pointing to the character. A green rounded badge with a thick black outline and white bold text "LISTENING {level}" below the text. High contrast, exaggerated emotion, clean composition.

Generate the image now. Render the Vietnamese text exactly as written in the hook."""

FIXED_CHANNEL_STYLE_TEMPLATE = (
    "YouTube thumbnail 16:9, pure white background, flat 2D cartoon / webtoon meme style, "
    "thick black outlines, flat solid colors, no gradients. Character on the left, cropped at chest or waist, "
    "large and filling the frame. Right side: huge bold sans-serif hook text, solid flat color, no shadow, no outline. "
    "A hand-drawn black curved arrow from the text pointing to the character. "
    'A green rounded badge with a thick black outline and white bold text "LISTENING {level}" below the text. '
    "High contrast, exaggerated emotion, clean composition."
)

VARIED_SCENE_PROFILES: List[Dict[str, Any]] = [
    {
        "id": "teen_girl_love_letter",
        "asset_file": "char_teen_girl_love_letter.jpg",
        "fallback_asset": "webtoon_char_crush.jpg",
        "keywords": ("thích thầm", "crush", "tỏ tình", "hẹn hò", "tình đầu", "lãng mạn"),
        "character": "a 17-year-old Vietnamese schoolgirl with long twin-braided pigtails, red ribbon headband, cream sweater vest over a white collared shirt",
        "expression": "eyes glancing sideways shyly with raised happy lower eyelids, rosy red blush across cheeks, small trembling smile",
        "gesture": "one hand pressed to her burning cheek, the other hand holding a pink love letter sealed with a heart sticker",
        "camera": "close-up 3/4 angle",
        "accents": "floating red hearts and sparkles",
    },
    {
        "id": "girl_broke_wallet",
        "asset_file": "char_girl_broke_wallet.jpg",
        "fallback_asset": "webtoon_char_shocked.jpg",
        "keywords": ("mượn tiền", "quỵt tiền", "đòi nợ", "hết tiền", "nghèo", "mua sắm", "tiền", "sang chảnh"),
        "character": "a chubby 20-year-old college girl with short black bob hair, a yellow star hairclip, and an oversized mint-green hoodie",
        "expression": "tearful glassy eyes, trembling lower lip, deeply drooping eyebrows, puffed cheeks",
        "gesture": "both hands holding an empty brown wallet upside down with a tiny fly buzzing out of it",
        "camera": "slight high angle (from above)",
        "accents": "tiny buzzing fly and blue sweat drops",
    },
    {
        "id": "auntie_drama_gossip",
        "asset_file": "char_auntie_drama_gossip.jpg",
        "fallback_asset": "webtoon_char_shocked.jpg",
        "keywords": ("drama", "trường giang", "ngoại tình", "quấy rối", "scandal", "bóc phốt", "nhìn trộm", "công sở"),
        "character": "a 42-year-old woman with wavy shoulder-length hair, cat-eye glasses sliding down her nose, gold hoop earrings, and a purple blazer",
        "expression": "one eyebrow arched high, wide eyes with pinprick pupils peeking over glasses, gasped O-shaped mouth",
        "gesture": "one hand half-covering her gasped mouth, the other hand holding up a glowing smartphone showing chat messages",
        "camera": "low angle (from below), off-center",
        "accents": "sparkles, sweat drops, and red exclamation marks",
    },
    {
        "id": "female_glasses_overload",
        "asset_file": "char_female_glasses_overload.jpg",
        "fallback_asset": "webtoon_student_panicked.jpg",
        "keywords": ("áp lực", "công việc", "deadline", "đi làm", "nỗ lực", "đồng trang lứa", "tuổi 25", "stress"),
        "character": "a 26-year-old office woman with round glasses, messy brown bun hair, yellow cardigan over a white collared shirt",
        "expression": "one eyebrow raised high, twitching eyelids, forced grin through clenched teeth with flushed cheeks",
        "gesture": "holding a spilling coffee mug in one hand and a chaotic stack of documents in the other",
        "camera": "3/4 medium close-up angle",
        "accents": "red anger vein symbol and flying white papers",
    },
    {
        "id": "sleepless_guy_alarm",
        "asset_file": "char_sleepless_guy_alarm.jpg",
        "fallback_asset": "webtoon_student_panicked.jpg",
        "keywords": ("mất ngủ", "đêm khuya", "thức khuya", "cuối tuần", "dậy sớm", "mệt mỏi", "overthinking"),
        "character": "a 30-year-old man with curly dyed-caramel hair, a green sleep mask pushed up on his forehead, wearing striped orange pajamas",
        "expression": "heavy dark bags under wide bloodshot eyes with pinprick pupils, mouth twisted open in an exhausted groan",
        "gesture": "one hand clutching his tousled hair, the other holding a ringing red twin-bell alarm clock showing 3:00 AM",
        "camera": "close-up frontal angle",
        "accents": "dizzy spiral lines and blue sweat drops",
    },
    {
        "id": "young_man_shy_crush",
        "asset_file": "webtoon_char_crush.jpg",
        "fallback_asset": "webtoon_char_crush.jpg",
        "keywords": ("yêu", "mập mờ", "người yêu", "tình cảm", "comeout", "rung động"),
        "character": "a 22-year-old young man with messy black hair, blue polo shirt",
        "expression": "blushing red cheeks, eyes glancing sideways shyly, raised lower eyelids, small nervous smile",
        "gesture": "both hands clasped tightly at his chest",
        "camera": "close-up chest-cropped angle",
        "accents": "small red hearts and dizzy lines floating around his head",
    },
    {
        "id": "shocked_boy_smartphone",
        "asset_file": "webtoon_char_shocked.jpg",
        "fallback_asset": "webtoon_char_shocked.jpg",
        "keywords": ("sốc", "bất ngờ", "phản bội", "bí mật", "bạn cùng phòng", "tin nhắn"),
        "character": "a 19-year-old male student with spiky black hair, casual collared shirt",
        "expression": "wide white eyes with tiny pinprick pupils, slanted worried eyebrows, jaw dropped in shock",
        "gesture": "holding a black smartphone in one hand and covering his open mouth with the other hand",
        "camera": "3/4 close-up angle",
        "accents": "flying blue sweat drops and red exclamation marks",
    },
    {
        "id": "panicked_student_exam",
        "asset_file": "webtoon_student_panicked.jpg",
        "fallback_asset": "webtoon_student_panicked.jpg",
        "keywords": ("2k9", "thi cử", "học", "điểm số", "sinh viên", "trường học"),
        "character": "a 16-year-old Vietnamese high school student with messy spiky black hair, school polo shirt",
        "expression": "wide white eyes with tiny pupils, worried slanted eyebrows, mouth wide open screaming, red blush marks on cheeks",
        "gesture": "both hands gripping his head in panic",
        "camera": "frontal close-up cropped at the chest",
        "accents": "blue sweat drops flying around and short black stress lines above the head",
    },
]

_LAST_SCENE_PROFILE_ID: Optional[str] = None
_SCENE_PROFILE_LOCK = threading.Lock()


def select_varied_scene_profile(topic: str, seed: int = 0) -> Dict[str, Any]:
    """
    Pick a scene profile for STEP 2 that matches the topic emotion/situation
    while guaranteeing we NEVER reuse the previous profile twice in a row.
    """
    global _LAST_SCENE_PROFILE_ID
    t_low = (topic or "").lower()

    matched: List[Dict[str, Any]] = []
    for prof in VARIED_SCENE_PROFILES:
        if any(k in t_low for k in prof["keywords"]):
            matched.append(prof)

    if not matched:
        matched = list(VARIED_SCENE_PROFILES)

    # Filter out the last used profile so consecutive thumbnails never repeat the same character
    # crc32 (not hash()) so the same topic+seed picks the same profile across processes
    digest = zlib.crc32(f"{topic or ''}{seed}".encode("utf-8"))
    with _SCENE_PROFILE_LOCK:
        non_repeating = [p for p in matched if p["id"] != _LAST_SCENE_PROFILE_ID]
        if not non_repeating:
            non_repeating = [p for p in VARIED_SCENE_PROFILES if p["id"] != _LAST_SCENE_PROFILE_ID]
        chosen = non_repeating[digest % len(non_repeating)]
        _LAST_SCENE_PROFILE_ID = chosen["id"]
    return chosen


def infer_thumbnail_character_en(topic: str, custom_char_en: str = "", seed: int = 0) -> str:
    """
    Return the English STEP 2 Scene description (Character + specific facial muscle Expression + Gesture & Prop + Camera + Accents),
    ensuring every new title gets a varied combination.
    """
    if custom_char_en and custom_char_en.strip():
        cleaned = re.sub(r"^Character\s*:\s*", "", custom_char_en.strip(), flags=re.IGNORECASE)
        if len(cleaned) > 20 and "blue polo shirt" not in cleaned.lower():
            return cleaned

    prof = select_varied_scene_profile(topic, seed=seed)
    return (
        f"{prof['character']}. "
        f"Expression: {prof['expression']}. "
        f"Gesture: {prof['gesture']}. "
        f"Camera: {prof['camera']}. "
        f"Small accents: {prof['accents']}."
    )


_FILLER_HOOK_WORDS = {
    "NHƯNG",
    "NGÀY",
    "NÀO",
    "CŨNG",
    "MÃI",
    "TRONG",
    "KHI",
    "CÁC",
    "THỨ",
    "GẦN",
    "ĐÂY",
    "ĐANG",
    "HOT",
    "VỀ",
    "CỦA",
    "NHỮNG",
    "MỘT",
    "CÁCH",
}


def condense_title_to_hook_words(clean_title: str) -> List[str]:
    """
    STEP 1 — Hook: turn the title into a Vietnamese hook of 2–6 words, ALL CAPS,
    with correct Vietnamese diacritics.
    """
    # Normalize common 7-word idioms so we never cut a 3-syllable Vietnamese idiom in half
    normalized = clean_title.replace("ĐỒNG TRANG LỨA", "ĐỒNG CHẤN").replace("ĐỒNG CHẤN", "TUỔI TRẺ")
    if "ÁP LỰC ĐỒNG TRANG LỨA" in clean_title:
        normalized = clean_title.replace("ĐỒNG TRANG LỨA ", "")
    words = [w.strip(",.;:!?\"'()") for w in normalized.split() if w.strip(",.;:!?\"'()")]
    if len(words) <= 6:
        return words if words else ["THÍCH", "THẦM", "MỘT", "NGƯỜI"]

    filtered = [w for w in words if w not in _FILLER_HOOK_WORDS]
    if 2 <= len(filtered) <= 6:
        return filtered
    if len(filtered) > 6:
        return filtered[:4] + filtered[-2:]
    return words[:6]


def split_thumbnail_title_lines(title_vi: str) -> tuple[List[str], str]:
    """
    STEP 1 — Hook: turn the title into a Vietnamese hook of 2–6 words, split into max 3 lines,
    ALL CAPS, with correct Vietnamese diacritics. Key words go in red, the rest in black.
    """
    short_title = re.sub(r"\s*\(.*?\)", "", (title_vi or "").strip()) or "THÍCH THẦM MỘT NGƯỜI"
    clean_title = re.sub(r"\s+", " ", short_title.upper()).strip()

    if "|" in clean_title:
        parts = [p.strip() for p in clean_title.split("|") if p.strip()]
        if len(parts) >= 3:
            black_lines = [parts[0], parts[1]]
            red_line = " ".join(parts[2:])
        elif len(parts) == 2:
            b_words = parts[0].split()
            if len(b_words) > 3:
                mid = (len(b_words) + 1) // 2
                black_lines = [" ".join(b_words[:mid]), " ".join(b_words[mid:])]
            else:
                black_lines = [parts[0]]
            red_line = parts[1]
        else:
            black_lines = [clean_title]
            red_line = "2K9?"
    else:
        words = condense_title_to_hook_words(clean_title)
        if len(words) <= 1:
            black_lines = [clean_title.rstrip("?!")]
            red_line = "SỰ THẬT?"
        elif len(words) == 2:
            black_lines = [words[0]]
            red_line = words[1]
        elif len(words) == 3:
            black_lines = [" ".join(words[:2])]
            red_line = words[2]
        elif len(words) == 4:
            black_lines = [" ".join(words[:2])]
            red_line = " ".join(words[2:])
        elif len(words) == 5:
            if words[4] in ("2K9", "2K8", "2K7", "2K6", "GENZ", "IELTS"):
                black_lines = [" ".join(words[:2]), " ".join(words[2:4])]
                red_line = words[4]
            elif len(" ".join(words[:3])) <= 12:
                black_lines = [" ".join(words[:3])]
                red_line = " ".join(words[3:5])
            elif words[0] in ("DRAMA", "VỤ", "CHUYỆN", "TIN", "BÓC"):
                black_lines = [words[0], " ".join(words[1:3])]
                red_line = " ".join(words[3:5])
            else:
                black_lines = [" ".join(words[:2]), words[2]]
                red_line = " ".join(words[3:5])
        else:
            # 6 words -> 3 lines of 2 words each (Line 1 & Line 2 in black, Line 3 in red)
            black_lines = [" ".join(words[:2]), " ".join(words[2:4])]
            red_line = " ".join(words[4:6])

    red_line = red_line.strip()
    if red_line and not red_line.endswith(("?", "!")):
        red_line = f"{red_line}?"
    return [b.strip() for b in black_lines if b.strip()][:2], red_line


def split_thumbnail_title_parts(title_vi: str) -> tuple[str, str]:
    """Compatibility helper returning (joined black text, red text)."""
    black_lines, red_line = split_thumbnail_title_lines(title_vi)
    return " ".join(black_lines), red_line


def build_thumbnail_prompts(
    title_vi: str,
    level: str = "B1",
    channel_tag: str = "UNI PODCAST",
    character_en: str = "",
    seed: int = 0,
) -> Dict[str, Any]:
    """
    Build the 3-step UNI PODCAST YouTube Thumbnail Generator prompt & scene breakdown:
      - STEP 1 — Hook (2-6 words, max 3 lines, ALL CAPS, key words in red, rest in black)
      - STEP 2 — Scene (varied Character, specific facial muscle Expression, Gesture + 1 Prop, Camera angle, Small accents)
      - STEP 3 — Layout and style (always fixed, 16:9 white background, badge "LISTENING {LEVEL}" below text)
    """
    lvl = (level or "B1").strip().upper()
    master_prompt = UNI_PODCAST_THUMBNAIL_MASTER_PROMPT.format(level=lvl)
    fixed_style = FIXED_CHANNEL_STYLE_TEMPLATE.format(level=lvl)
    prof = select_varied_scene_profile(title_vi, seed=seed)
    scene_desc = infer_thumbnail_character_en(title_vi, character_en, seed=seed)
    black_lines, red_line = split_thumbnail_title_lines(title_vi)

    black_quoted = ", ".join(f'"{line}"' for line in black_lines)
    text_line_spec = f'{black_quoted} in black, "{red_line}" in red'

    variable_part = (
        f"STEP 1 — Hook: {text_line_spec}\n"
        f"STEP 2 — Scene:\n"
        f"- Character: {prof['character']}\n"
        f"- Expression: {prof['expression']}\n"
        f"- Gesture & Prop: {prof['gesture']}\n"
        f"- Camera: {prof['camera']}\n"
        f"- Small accents: {prof['accents']}"
    )

    char_generation_prompt = (
        f"Flat 2D cartoon / webtoon meme style illustration on pure white background, thick black outlines, "
        f"flat solid colors, no gradients. Character: {scene_desc} "
        f"Cropped at chest or waist, large and filling the frame, high contrast, exaggerated emotion."
    )
    full_thumb_prompt = f"{master_prompt}\n\nTitle: {title_vi}\n{variable_part}"

    return {
        "fixed_style_prompt": master_prompt,
        "variable_video_prompt": variable_part,
        "character_en": scene_desc,
        "text_spec": text_line_spec,
        "character_prompt": char_generation_prompt,
        "thumbnail_prompt": full_thumb_prompt,
        "black_lines": black_lines,
        "red_text": red_line,
        "profile_id": prof["id"],
    }


def _draw_hand_drawn_comic_arrow(
    draw: ImageDraw.ImageDraw,
    start_xy: tuple[int, int],
    ctrl_xy: tuple[int, int],
    end_xy: tuple[int, int],
    width: int = 24,
) -> None:
    """Draw a bold curved hand-drawn black comic arrow pointing from the text on the right to the character on the left."""
    pts: List[tuple[float, float]] = []
    steps = 40
    x0, y0 = start_xy
    xc, yc = ctrl_xy
    x1, y1 = end_xy
    for i in range(steps + 1):
        t = i / float(steps)
        u = 1.0 - t
        px = u * u * x0 + 2 * u * t * xc + t * t * x1
        py = u * u * y0 + 2 * u * t * yc + t * t * y1
        pts.append((px, py))

    for i in range(len(pts) - 1):
        draw.line([pts[i], pts[i + 1]], fill="#FFFFFF", width=width + 14, joint="curve")
    for i in range(len(pts) - 1):
        draw.line([pts[i], pts[i + 1]], fill="#111111", width=width, joint="curve")
        r = width // 2
        cx_i, cy_i = int(pts[i][0]), int(pts[i][1])
        draw.ellipse([cx_i - r, cy_i - r, cx_i + r, cy_i + r], fill="#111111")

    prev_x, prev_y = pts[-5]
    dx = x1 - prev_x
    dy = y1 - prev_y
    angle = math.atan2(dy, dx)
    head_len = 92
    wing_angle = 0.58

    wx1 = x1 - head_len * math.cos(angle - wing_angle)
    wy1 = y1 - head_len * math.sin(angle - wing_angle)
    wx2 = x1 - head_len * math.cos(angle + wing_angle)
    wy2 = y1 - head_len * math.sin(angle + wing_angle)

    draw.line([(x1, y1), (wx1, wy1)], fill="#111111", width=width)
    draw.line([(x1, y1), (wx2, wy2)], fill="#111111", width=width)
    for px_end, py_end in ((x1, y1), (wx1, wy1), (wx2, wy2)):
        r = width // 2
        draw.ellipse([int(px_end) - r, int(py_end) - r, int(px_end) + r, int(py_end) + r], fill="#111111")


def render_thumbnail(
    bg_img: Image.Image,
    level: str,
    title_vi: str,
    category: str,
    channel_tag: str,
    out_path: Path,
) -> None:
    """
    Compose 4K Ultra HD (3840x2160) YouTube Thumbnail following STEP 3 — Layout and style:
      - Pure white background (#FFFFFF), flat 2D cartoon / webtoon meme style
      - Character on the left, cropped at chest or waist, large and filling the frame
      - Right side: huge bold sans-serif hook text (2-6 words, max 3 lines), solid flat color, NO shadow, NO outline
      - Hand-drawn black curved arrow from the text pointing to the character
      - Green rounded badge with thick black outline and white bold text "LISTENING {LEVEL}" below the text
    """
    import numpy as np

    w, h = 3840, 2160
    canvas = Image.new("RGB", (w, h), (255, 255, 255))

    # 1. Place the Flat 2D Cartoon Character on the Left side over pure white background
    char_src = bg_img.convert("RGB")
    arr = np.array(char_src, dtype=np.uint8)
    near_white = (arr[:, :, 0] > 236) & (arr[:, :, 1] > 236) & (arr[:, :, 2] > 236)
    arr[near_white] = [255, 255, 255]
    char_clean = Image.fromarray(arr, mode="RGB")

    char_target_h = 2080
    char_target_w = int(round(char_clean.width * (char_target_h / float(max(1, char_clean.height)))))
    if char_target_w > 2150:
        char_target_w = 2150
        char_target_h = int(round(char_clean.height * (char_target_w / float(max(1, char_clean.width)))))
    char_resized = char_clean.resize((char_target_w, char_target_h), resample=Image.Resampling.LANCZOS)
    char_x = max(-60, 960 - char_target_w // 2)
    char_y = h - char_target_h
    canvas.paste(char_resized, (char_x, char_y))

    draw = ImageDraw.Draw(canvas)

    # 2. Right Side: Huge Bold Sans-Serif Hook Text (2-6 words, max 3 lines, solid flat color, NO shadow, NO outline)
    black_lines, red_line = split_thumbnail_title_lines(title_vi)
    all_lines = [(line, "#111111") for line in black_lines[:2]] + [(red_line, "#E51C23")]
    right_cx = 2830
    max_text_w = 1780

    chosen_sz = 250
    for sz in (310, 285, 265, 245, 225, 205, 188, 172, 156, 142):
        f_test = get_font(sz, bold=True)
        fits_all = True
        for txt_line, _ in all_lines:
            bb_line = draw.textbbox((0, 0), txt_line, font=f_test)
            if (bb_line[2] - bb_line[0]) > max_text_w:
                fits_all = False
                break
        if fits_all:
            chosen_sz = sz
            break

    f_title = get_font(chosen_sz, bold=True)
    line_h = int(chosen_sz * 1.24)
    total_text_h = len(all_lines) * line_h

    text_block_center_y = 740
    curr_y = text_block_center_y - total_text_h // 2 + line_h // 2
    last_line_y = curr_y

    for txt_line, flat_color in all_lines:
        draw.text(
            (right_cx, curr_y),
            txt_line,
            font=f_title,
            fill=flat_color,
            anchor="mm",
        )
        last_line_y = curr_y
        curr_y += line_h

    # 3. Hand-drawn black curved arrow from the text pointing to the character
    arrow_start = (2320, min(1480, last_line_y + int(line_h * 0.58)))
    arrow_ctrl = (2000, min(1620, arrow_start[1] + 135))
    arrow_end = (1675, max(1140, arrow_start[1] - 115))
    _draw_hand_drawn_comic_arrow(draw, arrow_start, arrow_ctrl, arrow_end, width=22)

    # 4. Green rounded badge with thick black outline and white bold text "LISTENING B1" below the text
    lvl_str = (level or "B1").strip().upper()
    badge_label = f"LISTENING {lvl_str}"
    f_badge = get_font(86, bold=True)
    bb = draw.textbbox((0, 0), badge_label, font=f_badge)
    badge_w = (bb[2] - bb[0]) + 160
    badge_h = 158

    badge_x2 = 3660
    badge_x1 = badge_x2 - badge_w
    badge_y1 = 1760
    badge_y2 = badge_y1 + badge_h

    draw.rounded_rectangle(
        [badge_x1, badge_y1, badge_x2, badge_y2],
        radius=79,
        fill=(34, 197, 94),
        outline="#111111",
        width=14,
    )
    draw.text(
        ((badge_x1 + badge_x2) // 2, (badge_y1 + badge_y2) // 2),
        badge_label,
        font=f_badge,
        fill="#FFFFFF",
        anchor="mm",
    )

    canvas.save(out_path, "JPEG", quality=96)


def make_fallback_anime_bg(seed: int = 0, topic: str = "") -> Image.Image:
    """
    Load a varied Flat 2D Cartoon / Webtoon Meme character illustration on pure white background
    following STEP 2 (never reusing the same character twice in a row).
    """
    assets_dir = Path(__file__).parent.resolve() / "assets"
    prof = select_varied_scene_profile(topic, seed=seed)
    for fname in (prof["asset_file"], prof.get("fallback_asset", ""), "webtoon_student_panicked.jpg"):
        if not fname:
            continue
        cand = assets_dir / fname
        if cand.exists():
            try:
                return Image.open(cand).convert("RGB")
            except OSError as exc:
                logger.warning("Cannot open fallback bg %s: %s", cand, safe_err(exc))
    return Image.new("RGB", (1024, 1024), (255, 255, 255))


async def fetch_anime_image(
    prompt: str,
    out_path: Path,
    seed: int = 42,
    gemini_api_key: str = "",
    sensitivity_mode: str = "normal",
    topic: str = "",
) -> Image.Image:
    """
    Generate or load a varied Flat 2D Cartoon / Webtoon Meme character illustration on pure white background
    using the 3-step UNI PODCAST Thumbnail Generator specification.
    """
    scene_desc = infer_thumbnail_character_en(topic or prompt, prompt, seed=seed)
    styled_prompt = (
        f"Flat 2D cartoon / webtoon meme style illustration on pure white background, thick black outlines, "
        f"flat solid colors, no gradients. Character: {scene_desc} "
        f"Character on the left, cropped at chest or waist, large and filling the frame, exaggerated emotion, high contrast."
    )
    active_key = gemini_api_key.strip() or DEFAULT_GEMINI_KEY

    async with _image_semaphore():
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
            if active_key and _image_key_available(active_key):
                for img_model in ("gemini-2.5-flash-image", "gemini-3.1-flash-lite-image"):
                    try:
                        gem_img_url = f"https://generativelanguage.googleapis.com/v1beta/models/{img_model}:generateContent?key={active_key}"
                        r_gem = await client.post(
                            gem_img_url,
                            json={
                                "contents": [{"parts": [{"text": styled_prompt}]}],
                                "generationConfig": {"responseModalities": ["IMAGE"]},
                            },
                            timeout=18.0,
                        )
                        if r_gem.status_code == 200:
                            parts = r_gem.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
                            for p in parts:
                                b64_data = p.get("inlineData", {}).get("data")
                                if b64_data:
                                    raw_bytes = base64.b64decode(b64_data)
                                    img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
                                    img.save(out_path, "JPEG", quality=95)
                                    return img
                        elif r_gem.status_code == 429:
                            _cool_down_image_key(active_key)
                            break
                    except Exception as exc:
                        logger.warning("Gemini image request failed: %s", safe_err(exc))

    img = make_fallback_anime_bg(seed, topic=topic or prompt)
    img.save(out_path, "JPEG", quality=95)
    return img


def extract_speaker_and_emotion_prefix(text: str, default_speaker: str = "") -> tuple[str, str, str]:
    """
    Extract speaker and emotion prefix from custom script lines, supporting:
      - 'Anna [sad]: English' / 'Tom (angry): English' / 'Anna [buồn]: English'
      - '[sad]: English' / '[buồn] English' (for 1-speaker Monologue)
      - 'Anna: English'
    Returns (speaker, normalized_emotion, clean_text).
    """
    s = text.strip()
    # Case 1: Speaker + optional [emotion] or (emotion) + colon
    m = re.match(
        r"^(Anna|Tom|Jack|Mia|Speaker\s*[AB12]|Nữ|Nam)\s*(?:[\[\(]([^\]\)]+)[\]\)])?\s*:\s*(.+)$",
        s,
        flags=re.IGNORECASE,
    )
    if m:
        raw_sp = m.group(1).strip().lower()
        raw_emo = (m.group(2) or "").strip().lower()
        if raw_sp == "jack":
            sp = "Jack"
        elif raw_sp == "mia":
            sp = "Mia"
        elif raw_sp in ("tom", "speaker b", "speaker 2", "nam"):
            sp = "Tom"
        else:
            sp = "Anna"
        norm_emo = detect_line_emotion("", "", explicit_emotion=raw_emo) if raw_emo else ""
        if norm_emo and ":" in raw_emo:
            norm_emo = f"{norm_emo}:{raw_emo.split(':', 1)[1].strip()}"
        return sp, norm_emo, m.group(3).strip()

    # Case 2: Monologue emotion tag at start without speaker, e.g. '[sad]: English' or '[sad] English'
    m_mono = re.match(r"^[\[\(]([^\]\)]+)[\]\)]\s*:?\s*(.+)$", s, flags=re.IGNORECASE)
    if m_mono:
        raw_emo = m_mono.group(1).strip().lower()
        norm_emo = detect_line_emotion("", "", explicit_emotion=raw_emo)
        if norm_emo and ":" in raw_emo:
            norm_emo = f"{norm_emo}:{raw_emo.split(':', 1)[1].strip()}"
        return default_speaker, norm_emo, m_mono.group(2).strip()

    return default_speaker, "", s


def extract_speaker_prefix(text: str, default_speaker: str = "") -> tuple[str, str]:
    """Extract 'Anna: ...', 'Tom: ...', 'Jack: ...', or 'Mia: ...' prefix so TTS never reads the speaker name out loud."""
    sp, _, clean_txt = extract_speaker_and_emotion_prefix(text, default_speaker)
    return sp, clean_txt


def _is_male_voice_id(voice_id: str) -> bool:
    v = (voice_id or "").lower()
    for mv in ("puck", "fenrir", "charon", "enceladus", "orus", "iapetus", "umbriel", "achird", "algenib", "brian", "andrew", "christopher", "guy", "eric", "roger", "steffan", "ryan", "william"):
        if mv in v:
            return True
    return False


def parse_custom_script_text(
    raw_text: str,
    topic: str,
    level: str,
    category: str,
    podcast_mode: str = "dialogue",
    pair_type: str = "male_female",
) -> Dict[str, Any]:
    """
    Parse either JSON script or plain 'Anna [sad:60%]: English | Tiếng Việt' lines pasted by user.
    Automatically detects:
      - Title header ('Title: ...' / 'Tiêu đề: ...' / '# ...') if present
      - CEFR Level ('Level: B1' header or auto-estimated from average English word count per sentence)
      - Podcast mode ('monologue' vs 'dialogue') & Pair type ('male_female', 'male_male', 'female_female')
        directly from speaker prefixes in the script
      - Per-line emotion (from explicit '[emotion:intensity]' tag or auto-inferred from English/Vietnamese text)
    """
    raw_text = raw_text.strip()
    if raw_text.startswith("{"):
        return json.loads(raw_text)

    detected_title = (topic or "").strip()
    explicit_level = ""
    raw_entries: List[Dict[str, str]] = []
    explicit_speakers: List[str] = []

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Optional metadata headers at top of custom script
        m_title = re.match(r"^(?:title|tiêu\s*đề|chủ\s*đề|#)\s*[:\-]?\s*(.+)$", line, flags=re.IGNORECASE)
        if m_title and not raw_entries and "|" not in line:
            detected_title = m_title.group(1).strip()
            continue
        m_lvl = re.match(r"^(?:level|trình\s*độ)\s*[:\-]\s*(A1|A2|B1|B2|C1|C2)\b", line, flags=re.IGNORECASE)
        if m_lvl and not raw_entries:
            explicit_level = m_lvl.group(1).upper()
            continue

        if "|" in line:
            en_part, vi_part = line.split("|", 1)
            sp, emo, clean_en = extract_speaker_and_emotion_prefix(en_part, "")
            _, _, clean_vi = extract_speaker_and_emotion_prefix(vi_part, "")
        else:
            sp, emo, clean_en = extract_speaker_and_emotion_prefix(line, "")
            clean_vi = ""

        if not clean_en:
            continue
        if sp:
            explicit_speakers.append(sp)
        resolved_emo = emo or detect_line_emotion(clean_en, clean_vi)
        raw_entries.append(
            {
                "speaker": sp,
                "emotion": resolved_emo or "normal",
                "en": clean_en,
                "vi": clean_vi,
            }
        )

    # Auto-detect podcast_mode and pair_type from explicit speakers in the script
    unique_speakers = list(dict.fromkeys(explicit_speakers))
    ptype = (pair_type or "male_female").strip().lower()
    eff_mode = podcast_mode

    if len(explicit_speakers) == 0 or len(unique_speakers) == 1:
        eff_mode = "monologue"
    elif len(unique_speakers) >= 2:
        eff_mode = "dialogue"
        sp_set = {s.lower() for s in unique_speakers[:2]}
        if sp_set == {"tom", "jack"}:
            ptype = "male_male"
        elif sp_set == {"anna", "mia"}:
            ptype = "female_female"
        elif "anna" in sp_set and "tom" in sp_set:
            ptype = "male_female"

    if ptype == "male_male":
        sp1_default, sp2_default = "Tom", "Jack"
    elif ptype == "female_female":
        sp1_default, sp2_default = "Anna", "Mia"
    else:
        sp1_default, sp2_default = "Anna", "Tom"

    lines_list = []
    for idx_counter, entry in enumerate(raw_entries):
        sp = entry["speaker"]
        if eff_mode == "dialogue" and not sp:
            sp = sp1_default if idx_counter % 2 == 0 else sp2_default
        elif eff_mode == "monologue":
            sp = ""
        lines_list.append(
            {
                "speaker": sp,
                "emotion": entry["emotion"],
                "en": entry["en"],
                "vi": entry["vi"],
            }
        )

    # Auto-estimate CEFR level from average word count if not explicitly specified in script header
    if explicit_level:
        eff_level = explicit_level
    elif lines_list:
        avg_words = sum(len(x["en"].split()) for x in lines_list) / float(len(lines_list))
        if avg_words <= 6.5:
            eff_level = "A1"
        elif avg_words <= 8.5:
            eff_level = "A2"
        elif avg_words <= 11.5:
            eff_level = "B1"
        elif avg_words <= 14.0:
            eff_level = "B2"
        elif avg_words <= 16.5:
            eff_level = "C1"
        else:
            eff_level = "C2"
    else:
        eff_level = (level or "B1").upper()

    if not detected_title and lines_list:
        first_vi = lines_list[0].get("vi", "").strip()
        first_en = lines_list[0].get("en", "").strip()
        detected_title = (first_vi or first_en)[:42].strip(".,?! ") or "KỊCH BẢN CÓ SẴN"

    scenes = []
    chunk_size = max(1, math.ceil(len(lines_list) / 4))
    for i in range(0, len(lines_list), chunk_size):
        chunk = lines_list[i : i + chunk_size]
        scenes.append(
            {
                "image_prompt": f"expressive anime scene about {detected_title}, warm cinematic lighting",
                "lines": chunk,
            }
        )

    return {
        "title_vi": detected_title.upper(),
        "level": eff_level.upper(),
        "category": category.upper(),
        "podcast_mode": eff_mode,
        "pair_type": ptype,
        "thumb_prompt": f"expressive anime characters talking about {detected_title}, warm cozy lighting",
        "scenes": scenes,
    }


async def generate_script(
    topic: str,
    level: str = "B1",
    category: str = "TIẾNG ANH HỌC THUẬT",
    target_sentences: int = 10,
    podcast_mode: str = "dialogue",
    gemini_api_key: str = "",
    custom_script: str = "",
    sensitivity_mode: str = "normal",
    pair_type: str = "male_female",
) -> Dict[str, Any]:
    """
    Generate bilingual podcast script in the exact style of @REACH-l5p.
    Supports sensitivity_mode: 'polite' | 'normal' | 'sensitive_ok'.
    Supports pair_type: 'male_female' | 'male_male' | 'female_female'.
    """
    if custom_script and custom_script.strip():
        return parse_custom_script_text(custom_script, topic, level, category, podcast_mode, pair_type=pair_type)

    ptype = (pair_type or "male_female").strip().lower()
    if ptype == "male_male":
        sp1_name, sp2_name = "Tom", "Jack"
        pair_desc = "**Tom** (Male 1) and **Jack** (Male 2)"
    elif ptype == "female_female":
        sp1_name, sp2_name = "Anna", "Mia"
        pair_desc = "**Anna** (Female 1) and **Mia** (Female 2)"
    else:
        sp1_name, sp2_name = "Anna", "Tom"
        pair_desc = "**Anna** (female) and **Tom** (male)"

    num_scenes = max(2, min(8, max(2, target_sentences // 4)))
    sents_per_scene = max(3, round(target_sentences / num_scenes))

    lvl_up = level.strip().upper() or "B1"
    level_word_range = {
        "A1": "4 to 7 words per turn",
        "A2": "6 to 9 words per turn",
        "B1": "8 to 12 words per turn",
        "B2": "10 to 14 words per turn",
        "C1": "11 to 16 words per turn",
        "C2": "12 to 18 words per turn",
    }.get(lvl_up, "8 to 12 words per turn")

    level_guide = {
        "A1": (
            "STRICT CEFR A1 (Beginner — 4 to 7 words per turn ONLY): "
            "Use ONLY very simple top-500 everyday English words and short simple sentences in Present Simple or Past Simple. "
            "STRICTLY FORBIDDEN: NO sentences over 7 words, NO complex clauses, NO idioms, NO advanced phrasal verbs."
        ),
        "A2": (
            "STRICT CEFR A2 (Elementary — 6 to 9 words per turn): "
            "Use everyday top-1000 vocabulary and short sentences with 'and', 'but', 'because', 'so', 'when'. Do NOT exceed 9 words per turn."
        ),
        "B1": (
            "STRICT CEFR B1 (Intermediate — 8 to 12 words per turn): "
            "Use natural intermediate conversational English, common phrasal verbs (find out, make up, give up, move on), "
            "and relative/subordinate clauses expressing clear emotional reasons."
        ),
        "B2": (
            "STRICT CEFR B2 (Upper-Intermediate — 10 to 14 words per turn): "
            "Use expressive upper-intermediate vocabulary, emotional idioms (take for granted, behind my back, mixed signals), "
            "and natural multi-clause conversational phrasing."
        ),
        "C1": (
            "STRICT CEFR C1 (Advanced — 11 to 16 words per turn): "
            "Use sophisticated emotional phrasing, advanced idioms (string someone along, read between the lines), and nuanced subtext."
        ),
        "C2": (
            "STRICT CEFR C2 (Mastery / Native — 12 to 18 words per turn): "
            "Use authentic native-level American English dialogue with rich idiomatic expressions and cinematic emotional depth."
        ),
    }.get(lvl_up, "Use CEFR B1 natural emotional English (8-12 words per turn).")

    tone_guide = {
        "polite": (
            "CONTENT SENSITIVITY MODE = LỊCH SỰ (Warm & Heartfelt): "
            "Use warm, sincere, relatable everyday emotions and gentle storytelling."
        ),
        "normal": (
            "CONTENT SENSITIVITY MODE = CHÂN THẬT ĐỜI THƯỜNG (Authentic Real-Life Emotion): "
            "Write like a real conversation or movie scene between two people with genuine feelings, specific memories, and vulnerability."
        ),
        "sensitive_ok": (
            "CONTENT SENSITIVITY MODE = DRAMA & CẢM XÚC MÃNH LIỆT (Deep Romance / Heartbreak / Drama): "
            "Explore bold, emotionally intense, secret crush, situationship, heartbreak, betrayal, or raw confessions."
        ),
    }.get(sensitivity_mode, "Use authentic, emotionally resonant spoken conversation.")

    if podcast_mode == "dialogue":
        system_prompt = (
            "You are the lead emotional storytelling scriptwriter for 'UNI Podcast'. "
            "NEVER write dry textbook dialogues, NEVER write like two hosts discussing a topic from the outside, and NEVER write robotic 2-word fragments. "
            "Write an AUTHENTIC, EMOTIONAL REAL-LIFE STORY where the speakers ARE the two characters living inside the moment together ('I' and 'You'), "
            "sharing specific memories, real feelings, and a clear story arc from an unforgettable opening Hook to an emotional Twist. "
            f"STRICTLY OBEY CEFR LEVEL {lvl_up}: {level_guide} "
            "Return ONLY valid JSON without markdown fences."
        )
        mode_instruction = f"""Write a deeply authentic, emotional, and CLEARLY TOLD 2-speaker 1st-person story dialogue between {pair_desc} who are personally living through the situation: "{topic}" (CEFR Level: {lvl_up} — {level_word_range}).
{tone_guide}
CRITICAL STORYTELLING, LEVEL {lvl_up} & HOOK RULES:
1. **KILLER EMOTIONAL OPENING HOOK (Lines 1–2):** The very first 1–2 lines MUST immediately pull the listener into an intimate, tense, or surprising moment between {sp1_name} and {sp2_name} (NO greetings like "Hi, how are you?", and NEVER use the cliché "standing outside my door at two in the morning").
2. **A REAL STORY BETWEEN 2 PEOPLE (Coherent 4-Stage Story Arc):**
   - {sp1_name} and {sp2_name} are NOT commentators discussing "{topic}" — they ARE the two people experiencing it together right now!
   - Include specific, vivid human details so the listener clearly understands their backstory and feels their emotion.
   - Progress clearly through: (1) Strong Hook -> (2) Clear Backstory & Rising Emotion -> (3) Emotional Climax / Confession / Twist -> (4) Meaningful Resolution.
3. **STRICT CEFR {lvl_up} SENTENCE LENGTH & VOCABULARY ({level_word_range}):** {level_guide}
4. **Natural Vietnamese translation (`vi`):** Use real-life pronouns matching their relationship (`Anh - Em`, `Tớ - Cậu`, `Mình - Cậu`, `Tao - Mày`).
5. **18 Vocal Emotions + Blended Emotions + 3 Intensity Levels (`emotion`):** Tag every turn with the exact emotion from the 18-emotion catalog (`"normal"`, `"happy"`, `"excited"`, `"sad"`, `"angry"`, `"angry_cold"`, `"fear"`, `"surprised"`, `"disgusted"`, `"friendly"`, `"empathetic"`, `"serious"`, `"confident"`, `"anxious"`, `"shy"`, `"sarcastic"`, `"tired"`, `"whisper"`, `"gentle"`) or blended (`"happy+anxious"`, `"sad+angry"`, `"happy+sarcastic"`), optionally with intensity (`:30%`, `:60%`, `:100%` — keep most turns at `:30%` or `:60%` and only peak climax turns at `:100%`).
6. Alternate turns naturally between "{sp1_name}" and "{sp2_name}". Do NOT put "{sp1_name}:" inside the "en" string."""
        line_schema = f'{{"speaker": "{sp1_name}", "emotion": "sad:60%", "en": "English sentence strictly matching CEFR {lvl_up} ({level_word_range}).", "vi": "Câu dịch tiếng Việt chân thật, giàu cảm xúc."}}'
    else:
        system_prompt = (
            "You are a master 1-person Storytime / Confession narrator for 'UNI Podcast'. "
            "STRICT RULE: NEVER write a 2-person back-and-forth dialogue! There is ONLY ONE narrator ('I' / 'Mình' - 'Tôi') telling their own personal story from start to finish. "
            "Write a continuous, deeply emotional first-person narrative ('I' recounting what happened with 'he/she/they') with a killer opening Hook, vivid real-life details, an emotional Twist, and a memorable conclusion. "
            f"STRICTLY OBEY CEFR LEVEL {lvl_up}: {level_guide} "
            "Return ONLY valid JSON without markdown fences."
        )
        mode_instruction = f"""Write a deeply personal, emotional, and clearly structured 1-SPEAKER FIRST-PERSON STORYTELLING MONOLOGUE ('I' telling a story to the audience — STRICTLY NO 2-person conversation!) about: "{topic}" (CEFR Level: {lvl_up} — {level_word_range}).
{tone_guide}
CRITICAL MONOLOGUE STORYTELLING & LEVEL {lvl_up} RULES:
1. **100% SINGLE NARRATOR STORYTELLING (NO DIALOGUE):** Every sentence is spoken by the SAME single narrator ('I' / 'Mình') recounting their story from beginning to end. Refer to the other person in the 3rd person ('he', 'she', 'my best friend', 'my ex'). NEVER alternate between two speakers!
2. **KILLER OPENING HOOK (Line 1–2):** Start immediately with an unforgettable, intimate confession or shocking realization that hooks the listener in the first 3 seconds!
3. **CLEAR 4-STAGE STORY ARC ({level_word_range}):** (1) Strong Opening Hook -> (2) Backstory & specific real-life details -> (3) Climax / Twist discovery -> (4) Lingering realization or resolution.
4. **STRICT CEFR {lvl_up} SENTENCE LENGTH & VOCABULARY ({level_word_range}):** {level_guide}
5. **18 Vocal Emotions + Blended Emotions + 3 Intensity Levels (`emotion`):** Tag each sentence with `"normal"`, `"happy"`, `"excited"`, `"sad"`, `"angry"`, `"angry_cold"`, `"fear"`, `"surprised"`, `"disgusted"`, `"friendly"`, `"empathetic"`, `"serious"`, `"confident"`, `"anxious"`, `"shy"`, `"sarcastic"`, `"tired"`, `"whisper"`, `"gentle"`, `"happy+anxious"`, `"sad+angry"`, or `"happy+sarcastic"` (with `:30%`, `:60%`, or `:100%`)."""
        line_schema = f'{{"speaker": "", "emotion": "sad:60%", "en": "1st-person narrative sentence strictly matching CEFR {lvl_up} ({level_word_range}).", "vi": "Câu kể chuyện tự sự tiếng Việt tự nhiên, chạm cảm xúc."}}'

    user_prompt = f"""{mode_instruction}
{level_guide}

Structure the output into {num_scenes} scenes, with {sents_per_scene} lines per scene (total ~{num_scenes * sents_per_scene} lines).
Each line MUST have natural English ("en" strictly matching CEFR {lvl_up}: {level_word_range}) and natural, emotional Vietnamese translation ("vi").

Return ONLY a JSON object matching this exact schema (IMPORTANT: "title_vi" MUST be a fresh, catchy, viral 2–5 word Vietnamese Gen Z slang title in ALL CAPS matching this specific story):
{{
  "title_vi": "TIÊU ĐỀ GEN Z MỚI CỰC CHÁY (2-5 TỪ IN HOA)",
  "level": "{lvl_up}",
  "category": "{category.upper()}",
  "podcast_mode": "{podcast_mode}",
  "thumb_prompt": "STEP 2 Scene in English: vary gender, age (child to elder), body type, hair style/color, outfit, accessories (never default to a young man in a blue shirt); specific facial muscles (eyebrows, eyelids, mouth, cheeks); pose/hands + 1 story prop; camera angle; small accents.",
  "scenes": [
    {{
      "image_prompt": "detailed anime scene illustration description in English (setting, mood, character action)",
      "lines": [
        {line_schema}
      ]
    }}
  ]
}}"""

    raw_json_str = ""
    active_key = gemini_api_key.strip() or _load_default_gemini_key() or DEFAULT_GEMINI_KEY
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            if active_key:
                payload = {
                    "contents": [{"parts": [{"text": system_prompt + "\n\n" + user_prompt}]}],
                    "generationConfig": {"responseMimeType": "application/json", "temperature": 0.75},
                }
                for model_id in (
                    "gemini-2.5-flash",
                    "gemini-3-flash-preview",
                    "gemini-flash-lite-latest",
                ):
                    try:
                        gem_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent?key={active_key}"
                        resp = await client.post(gem_url, json=payload, timeout=18.0)
                        if resp.status_code == 200:
                            raw_json_str = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                            if raw_json_str and "{" in raw_json_str:
                                break
                    except Exception as exc:
                        logger.warning("Gemini script model %s failed: %s", model_id, safe_err(exc))
                        continue
            if not raw_json_str:
                for attempt in range(4):
                    try:
                        resp = await client.post(
                            "https://text.pollinations.ai/openai",
                            json={
                                "model": "openai",
                                "messages": [
                                    {"role": "system", "content": system_prompt},
                                    {"role": "user", "content": user_prompt},
                                ],
                                "response_format": {"type": "json_object"},
                            },
                            timeout=60.0,
                        )
                        if resp.status_code == 200:
                            data = resp.json()
                            raw_json_str = data["choices"][0]["message"]["content"]
                            if raw_json_str and "{" in raw_json_str:
                                break
                        elif resp.status_code in (402, 429, 502, 503):
                            m = re.search(r"Retry after (\d+)s", resp.text)
                            wait_s = min(20, max(4, int(m.group(1)) + 1)) if m else (6 * (attempt + 1))
                            await asyncio.sleep(wait_s)
                            continue
                    except Exception as exc:
                        logger.warning("Script fallback API attempt %d failed: %s", attempt + 1, safe_err(exc))
                        await asyncio.sleep(3 * (attempt + 1))

        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_json_str.strip(), flags=re.MULTILINE)
        start_idx = cleaned.find("{")
        end_idx = cleaned.rfind("}")
        if start_idx != -1 and end_idx != -1:
            cleaned = cleaned[start_idx : end_idx + 1]
        parsed = json.loads(cleaned)
        parsed["podcast_mode"] = podcast_mode
        parsed["level"] = level.upper()
        parsed["sensitivity_mode"] = sensitivity_mode
        return parsed
    except Exception as exc:
        logger.warning("Script generation failed, using fallback script: %s", safe_err(exc))
        return build_fallback_script(topic, level, category, podcast_mode)


def build_fallback_script(
    topic: str, level: str, category: str, podcast_mode: str = "dialogue"
) -> Dict[str, Any]:
    t_upper = topic.strip().upper()
    if podcast_mode == "dialogue":
        return {
            "title_vi": t_upper,
            "level": level.upper(),
            "category": category.upper(),
            "podcast_mode": "dialogue",
            "thumb_prompt": f"anime girl and boy sitting next to each other in high school classroom, secret crush vibe, theme {topic}, warm sunset light",
            "scenes": [
                {
                    "image_prompt": "anime high school girl and cold handsome boy sitting next to each other by classroom window, warm sunlight",
                    "lines": [
                        {
                            "speaker": "Anna",
                            "en": "Tom, do you remember what I told you about my older cousin? Yesterday she told me a story from her school days, and it feels so real.",
                            "vi": "Tom này, cậu còn nhớ chuyện tớ kể về chị họ tớ không? Hôm qua chị ấy kể cho tớ một câu chuyện thời đi học, nghe thật đến mức cứ như xảy ra quanh tụi mình vậy.",
                        },
                        {
                            "speaker": "Tom",
                            "en": "Really? What kind of story is it? Why do you sound so serious?",
                            "vi": "Thật á? Chuyện gì thế? Sao giọng cậu nghe nghiêm túc vậy?",
                        },
                        {
                            "speaker": "Anna",
                            "en": "So it is about two people, let's call them Lily and Ethan. Lily was a normal, clumsy girl, while Ethan was the cold top student.",
                            "vi": "Chuyện kể về hai người, cứ gọi họ là Lily và Ethan nhé. Lily là một cô gái bình thường hơi vụng về, còn Ethan là học sinh giỏi nhất lớp nhưng khá lạnh lùng.",
                        },
                        {
                            "speaker": "Tom",
                            "en": "Yeah, I can imagine that type. And let me guess, they had to sit next to each other?",
                            "vi": "Ừ, tớ hình dung ra kiểu người đó rồi. Và để tớ đoán nhé, hai người họ bị xếp ngồi cạnh nhau đúng không?",
                        },
                    ],
                },
                {
                    "image_prompt": "anime boy quietly tapping pen on girl's notebook to help her with math formula after school, gentle expression",
                    "lines": [
                        {
                            "speaker": "Anna",
                            "en": "Exactly! During a test, Lily made many mistakes. Ethan noticed it and quietly tapped his pen on her paper to point out the wrong formula.",
                            "vi": "Chuẩn luôn! Trong một bài kiểm tra, Lily làm sai rất nhiều. Ethan nhận ra và khẽ gõ bút lên giấy của cô ấy để chỉ ra công thức bị sai.",
                        },
                        {
                            "speaker": "Tom",
                            "en": "So he helped her without saying it directly. He actually cared about her, even if he acted cold.",
                            "vi": "Vậy là cậu ấy giúp cô ấy mà không nói thẳng ra. Thực ra cậu ấy rất quan tâm dù bề ngoài tỏ ra lạnh lùng.",
                        },
                        {
                            "speaker": "Anna",
                            "en": "Yes, and after school he even stayed late to explain the lesson. That was when Lily realized her heart was beating faster around him.",
                            "vi": "Đúng vậy, và sau giờ học cậu ấy thậm chí còn ở lại muộn để giảng bài. Đó cũng là lúc Lily nhận ra tim mình đập nhanh hơn khi ở cạnh cậu ấy.",
                        },
                        {
                            "speaker": "Tom",
                            "en": "That is when feelings start to grow. Sometimes the quietest moments leave the deepest impression.",
                            "vi": "Đó chính là lúc tình cảm bắt đầu lớn dần lên. Đôi khi những khoảnh khắc lặng lẽ nhất lại để lại ấn tượng sâu sắc nhất.",
                        },
                    ],
                },
            ],
        }

    return {
        "title_vi": t_upper,
        "level": level.upper(),
        "category": category.upper(),
        "podcast_mode": "monologue",
        "thumb_prompt": f"expressive anime student sitting at desk late at night surrounded by books and glowing laptop, theme {topic}, warm lamp light",
        "scenes": [
            {
                "image_prompt": "cozy anime bedroom at night, student sitting at desk with notebooks and coffee cup, city lights outside window",
                "lines": [
                    {
                        "speaker": "",
                        "en": "Hello everyone, and welcome back to the Reach podcast.",
                        "vi": "Xin chào mọi người, và chào mừng các bạn đã quay trở lại với Reach Podcast.",
                    },
                    {
                        "speaker": "",
                        "en": "Have you ever wanted people to look at you and think, wow, they are working so hard?",
                        "vi": "Bạn đã bao giờ muốn mọi người nhìn vào mình và nghĩ: Chà, người này đang cố gắng chăm chỉ thật đấy chưa?",
                    },
                    {
                        "speaker": "",
                        "en": "You sit at your desk for three hours with colorful highlighters and an open laptop.",
                        "vi": "Bạn ngồi vào bàn học suốt ba tiếng đồng hồ với những cây bút nhớ đủ màu và chiếc laptop đang mở.",
                    },
                    {
                        "speaker": "",
                        "en": "From the outside, your life looks like a perfect picture of discipline.",
                        "vi": "Nhìn từ bên ngoài, cuộc sống của bạn trông giống như một bức tranh hoàn hảo của sự kỷ luật.",
                    },
                ],
            },
            {
                "image_prompt": "anime student in a warm cafe looking at smartphone while textbooks lie open on wooden table, soft bokeh",
                "lines": [
                    {
                        "speaker": "",
                        "en": "But inside, you know the truth is very different.",
                        "vi": "Nhưng sâu bên trong, bạn hiểu rằng sự thật lại hoàn toàn khác.",
                    },
                    {
                        "speaker": "",
                        "en": "Every ten minutes, you check your phone just to see who liked your study photo.",
                        "vi": "Cứ mỗi mười phút, bạn lại cầm điện thoại lên chỉ để xem ai vừa thả tim bức ảnh học bài của mình.",
                    },
                    {
                        "speaker": "",
                        "en": "We often fall in love with the image of working hard instead of actually focusing.",
                        "vi": "Chúng ta thường yêu cái cảm giác tỏ ra chăm chỉ hơn là thực sự tập trung làm việc.",
                    },
                    {
                        "speaker": "",
                        "en": "Real progress happens quietly when nobody is watching you.",
                        "vi": "Sự tiến bộ thực sự luôn diễn ra trong thầm lặng khi không có ai đang nhìn vào bạn.",
                    },
                ],
            },
        ],
    }


GEMINI_TTS_MODELS = (
    "gemini-2.5-flash-preview-tts",
    "gemini-3.1-flash-tts-preview",
    "gemini-3.8-flash-tts",
    "gemini-3.8-flash-lite-tts",
)
_GEMINI_TTS_QUOTA_EXHAUSTED_UNTIL: float = 0.0

EXPRESSIVE_VOICE_MAP = {
    "en-US-AvaNeural": "en-US-AvaMultilingualNeural",
    "en-US-AndrewNeural": "en-US-AndrewMultilingualNeural",
    "en-US-BrianNeural": "en-US-BrianMultilingualNeural",
    "en-US-EmmaMultilingualNeural": "en-US-EmmaMultilingualNeural",
    # Female voices mapping (distinct neural voices per selection)
    "gemini:Aoede": "en-US-AvaMultilingualNeural",
    "Aoede": "en-US-AvaMultilingualNeural",
    "gemini:Kore": "en-US-EmmaMultilingualNeural",
    "Kore": "en-US-EmmaMultilingualNeural",
    "gemini:Leda": "en-US-JennyNeural",
    "Leda": "en-US-JennyNeural",
    "gemini:Zephyr": "en-US-AriaNeural",
    "Zephyr": "en-US-AriaNeural",
    "gemini:Callirrhoe": "en-US-MichelleNeural",
    "Callirrhoe": "en-US-MichelleNeural",
    "gemini:Despina": "en-US-SaraNeural",
    "Despina": "en-US-SaraNeural",
    "gemini:Autonoe": "en-GB-SoniaNeural",
    "Autonoe": "en-GB-SoniaNeural",
    "gemini:Achernar": "en-US-NancyNeural",
    "Achernar": "en-US-NancyNeural",
    "gemini:Sulafat": "en-AU-NatashaNeural",
    "Sulafat": "en-AU-NatashaNeural",
    # Male voices mapping (distinct neural voices per selection)
    "gemini:Puck": "en-US-BrianMultilingualNeural",
    "Puck": "en-US-BrianMultilingualNeural",
    "gemini:Fenrir": "en-US-ChristopherNeural",
    "Fenrir": "en-US-ChristopherNeural",
    "gemini:Charon": "en-US-AndrewMultilingualNeural",
    "Charon": "en-US-AndrewMultilingualNeural",
    "gemini:Enceladus": "en-US-GuyNeural",
    "Enceladus": "en-US-GuyNeural",
    "gemini:Orus": "en-US-EricNeural",
    "Orus": "en-US-EricNeural",
    "gemini:Iapetus": "en-US-RogerNeural",
    "Iapetus": "en-US-RogerNeural",
    "gemini:Umbriel": "en-US-SteffanNeural",
    "Umbriel": "en-US-SteffanNeural",
    "gemini:Achird": "en-GB-RyanNeural",
    "Achird": "en-GB-RyanNeural",
    "gemini:Algenib": "en-AU-WilliamNeural",
    "Algenib": "en-AU-WilliamNeural",
}


def sanitize_english_for_tts(raw_text: str) -> str:
    """
    Ensure text sent to English TTS is 100% pure natural American English:
      - Converts ellipses ('...' / '…') into natural comma pauses so Gemini Live Native Audio
        never trails off or cuts off words after '...'.
      - Removes any stray Vietnamese accented words/phrases so TTS stays 100% in native English mode.
      - Replaces written interjections like 'Ugh' or 'Haha' that TTS pronounces robotically.
    """
    import unicodedata

    t = (raw_text or "").strip()
    if not t:
        return "Hello."
    # Strip leading ellipses ('...I deserve that.' -> 'I deserve that.')
    t = re.sub(r"^(?:\.{2,}|…)+\s*", "", t)
    # Replace mid-sentence ellipses with a comma pause so Gemini Live never truncates after '...'
    t = re.sub(r"\s*(?:\.{2,}|…)+\s*([A-Za-z0-9])", r", \1", t)
    t = re.sub(r"(?:\.{2,}|…)+", ".", t)
    # Replace written interjections that neural TTS reads awkwardly ("Ugh" -> "Ấc")
    t = re.sub(r"\bugh+\b[,!.]*", "Oh man,", t, flags=re.IGNORECASE)
    t = re.sub(r"\bhaha+\b[,!.]*", "Ha ha,", t, flags=re.IGNORECASE)
    t = re.sub(r"\bhmm+\b[,!.]*", "Well,", t, flags=re.IGNORECASE)
    t = re.sub(r"\buhm+\b[,!.]*", "um,", t, flags=re.IGNORECASE)
    # Remove tokens that contain Vietnamese diacritics if any ever slipped into English text
    tokens = t.split()
    clean_tokens = []
    for tok in tokens:
        has_vn_diacritic = any(ord(ch) > 127 and ch not in "’‘“”—–…" for ch in tok)
        if has_vn_diacritic:
            continue
        clean_tokens.append(tok)
    t = (
        " ".join(clean_tokens)
        if clean_tokens
        else unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode("ascii")
    )
    t = re.sub(r"[^A-Za-z0-9\s.,?!'\"-]", " ", t)
    # Quotation marks make the native-audio model hesitate before the quoted phrase ('Things like [stall] "Take care"');
    # lowercase mid-sentence quoted starters (unless "I") and drop the quotes for TTS while keeping them in subtitles.
    t = re.sub(
        r'([a-z,])\s+"([A-HJ-Z][a-z]+)',
        lambda m: f"{m.group(1)} {m.group(2).lower()}",
        t,
    )
    t = t.replace('"', "")
    t = re.sub(r"\s*,\s*,+", ",", t)
    # Remove comma after single-word starters ("No, I'm" -> "No I'm", "Okay, I" -> "Okay I") to prevent start-of-sentence stutters
    t = re.sub(r"^(No|Yes|Okay|Well|Wait|Oh|Look),\s+", r"\1 ", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip()
    return t or "Hello."


# ==============================================================================
# 18-EMOTION + BLENDED EMOTIONS + 3-INTENSITY (30%/60%/100%) + 3-LAYER PROSODY
# ==============================================================================

_log = logging.getLogger(__name__)

# Hard limits applied after intensity scaling / blending so stacked values never distort the voice.
EMOTION_MAX_PITCH_HZ = 24      # female-reference Hz offset; scaled down for male voices
EMOTION_MAX_RATE_PCT = 25
EMOTION_MAX_ENERGY_PCT = 45
MALE_PITCH_HZ_FACTOR = 0.55    # male F0 (~110Hz) vs female F0 (~210Hz): same Hz offset sounds ~2x stronger on male
MAX_INTERNAL_PAUSE_MS = 220    # longest silence kept inside one spoken line; longer model stalls are shortened to this
SILENCE_RMS_THRESHOLD = 260.0  # 10ms-window RMS below this counts as silence (Gemini stall gaps measure 5-200; speech tails stay above 400)
PAUSE_JITTER = 0.12           # +-12% random variation so sentence gaps are not machine-regular


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


VOCAL_EMOTION_SPECS: Dict[str, Dict[str, Any]] = {
    # --- A. Nhóm cảm xúc cơ bản (1 - 8) ---
    "normal": {
        "pitch_hz": 0,
        "range_pct": 15,
        "rate_pct": 0,
        "energy_pct": 0,
        "rms_target": 3500.0,
        "end_decay": 0.86,
        "pause_sec": 0.44,
        "acting": (
            "neutral natural baseline: pitch 0%, moderate natural pitch contour (never flat or robotic), "
            "rate 100%, clean voice with minimal breathiness, stress key content words clearly and let energy drop gently at the end of the sentence"
        ),
        "nonverbal": "",
    },
    "happy": {
        "pitch_hz": 12,
        "range_pct": 40,
        "rate_pct": 12,
        "energy_pct": 18,
        "rms_target": 3950.0,
        "end_decay": 0.95,
        "pause_sec": 0.28,
        "acting": (
            "happy and joyful: pitch +12-15%, pitch range +35-45%, rate +12%, energy +18%, "
            "bright open-mouth resonance as if smiling naturally while speaking, pitch bounces lightly on stressed words (sincere joy, NOT fake commercial hype)"
        ),
        "nonverbal": "an optional brief warm chuckle (0.3s) only at the very start or end",
    },
    "excited": {
        "pitch_hz": 18,
        "range_pct": 60,
        "rate_pct": 20,
        "energy_pct": 28,
        "rms_target": 4250.0,
        "end_decay": 1.0,
        "pause_sec": 0.20,
        "acting": (
            "excited and thrilled: pitch +18-22%, wide dynamic pitch range +55-65%, brisk rate +20%, high energy +28%, "
            "bright energetic voice with quick connected phrasing and an upward inflection at the end"
        ),
        "nonverbal": "a quick excited intake of breath (0.25s) before speaking",
    },
    "sad": {
        "pitch_hz": -12,
        "range_pct": -35,
        "rate_pct": -18,
        "energy_pct": -26,
        "rms_target": 2950.0,
        "end_decay": 0.75,
        "pause_sec": 0.62,
        "acting": (
            "sad and heartbroken: pitch -12%, narrower pitch range -35%, unhurried pace -15%, softer energy -25%, "
            "breathy voice with genuine emotional weight (NOT sleepy), softening gently at the end of the sentence in one unbroken flow"
        ),
        "nonverbal": "a soft, heartfelt sigh (0.4s) before speaking",
    },
    "angry": {
        "pitch_hz": 10,
        "range_pct": 30,
        "rate_pct": 15,
        "energy_pct": 42,
        "rms_target": 4300.0,
        "end_decay": 0.96,
        "pause_sec": 0.28,
        "acting": (
            "angry and indignant (explosive anger): pitch +10%, range +30%, brisk rate +15%, firm high energy +40%, "
            "tense vocal cords, crisp hard initial consonants, decisive phrasing punching each stressed syllable with real emotional tension"
        ),
        "nonverbal": "",
    },
    "angry_cold": {
        "pitch_hz": -6,
        "range_pct": -15,
        "rate_pct": -14,
        "energy_pct": 10,
        "rms_target": 3450.0,
        "end_decay": 0.92,
        "pause_sec": 0.50,
        "acting": (
            "cold controlled anger: pitch -5%, narrow controlled range, slower deliberate rate -14%, "
            "low controlled volume but intense vocal tension through clenched teeth, articulating every single word with icy precision"
        ),
        "nonverbal": "",
    },
    "fear": {
        "pitch_hz": 16,
        "range_pct": 22,
        "rate_pct": 22,
        "energy_pct": -12,
        "rms_target": 3150.0,
        "end_decay": 0.85,
        "pause_sec": 0.34,
        "acting": (
            "scared and panicked: pitch +16-20%, unstable pitch contour, hurried rate +22%, low-to-medium energy, "
            "slight vocal tremor, shallow breathy delivery as if genuinely startled and afraid"
        ),
        "nonverbal": "a quick startled gasp of breath (0.25s) at the start",
    },
    "surprised": {
        "pitch_hz": 22,
        "range_pct": 50,
        "rate_pct": 12,
        "energy_pct": 20,
        "rms_target": 3950.0,
        "end_decay": 0.92,
        "pause_sec": 0.25,
        "acting": (
            "genuinely surprised: pitch rises +25-35% on the opening surprise word then settles naturally, "
            "quick animated delivery with a sudden burst of vocal energy in one continuous flow"
        ),
        "nonverbal": "a quick intake of breath (0.2s) right before the first word",
    },
    "disgusted": {
        "pitch_hz": -8,
        "range_pct": 15,
        "rate_pct": -10,
        "energy_pct": -5,
        "rms_target": 3350.0,
        "end_decay": 0.88,
        "pause_sec": 0.38,
        "acting": (
            "disgusted and displeased: pitch -8% dropping lower on the criticized word, slower drawl on key vowels, "
            "slight nasal resonance conveying clear distaste and annoyance"
        ),
        "nonverbal": "a brief disapproving tongue click 'tsk' (0.15s) before speaking",
    },
    # --- B. Nhóm cảm xúc xã hội (9 - 18) ---
    "friendly": {
        "pitch_hz": 5,
        "range_pct": 15,
        "rate_pct": -5,
        "energy_pct": 4,
        "rms_target": 3550.0,
        "end_decay": 0.90,
        "pause_sec": 0.36,
        "acting": (
            "friendly and warm: pitch +5%, range +15%, relaxed rate -5%, balanced energy, "
            "soft warm timbre with a gentle smile in the voice"
        ),
        "nonverbal": "",
    },
    "empathetic": {
        "pitch_hz": -5,
        "range_pct": -10,
        "rate_pct": -15,
        "energy_pct": -14,
        "rms_target": 3100.0,
        "end_decay": 0.84,
        "pause_sec": 0.56,
        "acting": (
            "empathetic and comforting: pitch -5%, gentle range -10%, unhurried pace -15%, soft soothing energy, "
            "warm caring tone with natural thoughtful pacing so the listener feels deeply understood"
        ),
        "nonverbal": "",
    },
    "serious": {
        "pitch_hz": -4,
        "range_pct": -20,
        "rate_pct": -8,
        "energy_pct": 4,
        "rms_target": 3600.0,
        "end_decay": 0.90,
        "pause_sec": 0.45,
        "acting": (
            "serious and composed: pitch -4%, controlled range -20%, steady rate -8%, even grounded energy, "
            "firm vocal posture with crisp consonants and clear phrase boundaries"
        ),
        "nonverbal": "",
    },
    "confident": {
        "pitch_hz": -2,
        "range_pct": 20,
        "rate_pct": 0,
        "energy_pct": 14,
        "rms_target": 3850.0,
        "end_decay": 0.92,
        "pause_sec": 0.40,
        "acting": (
            "confident and persuasive: steady pace, energy +12-15%, clear authoritative stress on key words, "
            "downward pitch resolution at the end of the sentence for a decisive, convincing finish"
        ),
        "nonverbal": "",
    },
    "anxious": {
        "pitch_hz": 5,
        "range_pct": 20,
        "rate_pct": 10,
        "energy_pct": -6,
        "rms_target": 3300.0,
        "end_decay": 0.86,
        "pause_sec": 0.36,
        "acting": (
            "anxious and restless: pitch +5% with expressive pitch movement, slightly brisk rate +10%, "
            "uneasy nervous vocal timbre delivered in one smooth continuous breath flow without stopping mid-sentence"
        ),
        "nonverbal": "a brief nervous breath intake before speaking",
    },
    "shy": {
        "pitch_hz": 6,
        "range_pct": -15,
        "rate_pct": -12,
        "energy_pct": -18,
        "rms_target": 2900.0,
        "end_decay": 0.80,
        "pause_sec": 0.48,
        "acting": (
            "shy and bashful: slightly higher pitch +6%, soft gentle energy -18%, warm modest pace -10%, "
            "sweet self-conscious voice delivered in one smooth unbroken breath flow without pausing between words"
        ),
        "nonverbal": "",
    },
    "sarcastic": {
        "pitch_hz": 6,
        "range_pct": 42,
        "rate_pct": -10,
        "energy_pct": 8,
        "rms_target": 3650.0,
        "end_decay": 0.88,
        "pause_sec": 0.42,
        "acting": (
            "sarcastic and ironic: wide pitch swing +40%, slightly slower rate -10% on the ironic word, "
            "exaggerated dry emphasis on the opposite-meaning word with a wry, knowing edge"
        ),
        "nonverbal": "a dry ironic chuckle (0.3s)",
    },
    "tired": {
        "pitch_hz": -8,
        "range_pct": -35,
        "rate_pct": -20,
        "energy_pct": -28,
        "rms_target": 2850.0,
        "end_decay": 0.72,
        "pause_sec": 0.60,
        "acting": (
            "tired and exhausted: pitch -8%, narrow pitch range -35%, unhurried weary pace -16%, low energy -28%, "
            "weary breathy voice that softens gently at the end of the sentence in one connected flow"
        ),
        "nonverbal": "a weary exhale sigh (0.4s) before speaking",
    },
    "whisper": {
        "pitch_hz": -2,
        "range_pct": -25,
        "rate_pct": -10,
        "energy_pct": -32,
        "rms_target": 2400.0,
        "end_decay": 0.85,
        "pause_sec": 0.28,
        "acting": (
            "hushed secretive close-mic tone: very soft intimate volume -30%, breathy close-to-microphone delivery, "
            "rate -10%, short intimate phrasing as if sharing a secret"
        ),
        "nonverbal": "",
    },
    "gentle": {
        "pitch_hz": -6,
        "range_pct": -20,
        "rate_pct": -22,
        "energy_pct": -20,
        "rms_target": 2950.0,
        "end_decay": 0.80,
        "pause_sec": 0.56,
        "acting": (
            "gentle and soothing: low warm pitch -6%, narrow smooth wave-like intonation, calm unhurried rate -22%, "
            "soft tender voice"
        ),
        "nonverbal": "",
    },
    # --- D. Nhóm trộn cảm xúc (Blended Emotions) ---
    "happy+anxious": {
        "pitch_hz": 9,
        "range_pct": 30,
        "rate_pct": 11,
        "energy_pct": 6,
        "rms_target": 3600.0,
        "end_decay": 0.88,
        "pause_sec": 0.32,
        "acting": (
            "blended happy + anxious (relieved yet afraid it might go wrong): bright hopeful smile in the voice "
            "mixed with a slight nervous quiver and cautious breathless excitement"
        ),
        "nonverbal": "",
    },
    "sad+angry": {
        "pitch_hz": 2,
        "range_pct": 25,
        "rate_pct": -4,
        "energy_pct": 16,
        "rms_target": 3850.0,
        "end_decay": 0.85,
        "pause_sec": 0.46,
        "acting": (
            "blended sad + angry (deeply hurt, bitter and resentful 'uất ức'): voice thick with heartbreak and held-back tears "
            "yet firm and trembling with indignant accusation"
        ),
        "nonverbal": "a shaky breath intake before speaking",
    },
    "happy+sarcastic": {
        "pitch_hz": 8,
        "range_pct": 38,
        "rate_pct": -6,
        "energy_pct": 6,
        "rms_target": 3600.0,
        "end_decay": 0.86,
        "pause_sec": 0.40,
        "acting": (
            "blended happy + sarcastic (bitter smile / 'cười nhạt'): light smiling vocal tone masking dry, "
            "bittersweet irony with elongated stress on the key word"
        ),
        "nonverbal": "a brief dry laugh (0.3s)",
    },
}

EMOTION_ALIAS_MAP: Dict[str, str] = {
    # 1. Trung tính / tự nhiên
    "normal": "normal", "neutral": "normal", "baseline": "normal",
    "binh_thuong": "normal", "binh thuong": "normal", "bình thường": "normal",
    "trung_tinh": "normal", "trung tính": "normal", "tự nhiên": "normal", "tu nhien": "normal",
    # 2. Vui vẻ / hạnh phúc
    "happy": "happy", "joy": "happy", "cheerful": "happy", "glad": "happy",
    "vui": "happy", "vui vẻ": "happy", "vui ve": "happy", "hạnh phúc": "happy", "hanh phuc": "happy",
    # 3. Hào hứng / phấn khích
    "excited": "excited", "thrilled": "excited", "enthusiastic": "excited",
    "hao_hung": "excited", "hào hứng": "excited", "hao hung": "excited", "phấn khích": "excited", "phan khich": "excited",
    # 4. Buồn
    "sad": "sad", "hurt": "sad", "heartbroken": "sad", "cry": "sad", "tearful": "sad", "sorrow": "sad",
    "buon": "sad", "buồn": "sad", "buồn bã": "sad", "đau lòng": "sad", "nghẹn ngào": "sad",
    # 5. Tức giận (Bùng nổ & Giận lạnh)
    "angry": "angry", "furious": "angry", "mad": "angry", "upset": "angry", "frustrated": "angry", "angry_explosive": "angry",
    "gian": "angry", "giận": "angry", "dận": "angry", "giận dữ": "angry", "dận dữ": "angry",
    "tức giận": "angry", "tuc gian": "angry", "giận bùng nổ": "angry", "phẫn nộ": "angry",
    "angry_cold": "angry_cold", "cold_anger": "angry_cold", "icy": "angry_cold",
    "giận lạnh": "angry_cold", "gian lanh": "angry_cold", "nghiến răng": "angry_cold",
    # 6. Sợ hãi / hoảng
    "fear": "fear", "scared": "fear", "afraid": "fear", "panicked": "fear", "terrified": "fear",
    "so": "fear", "sợ": "fear", "sợ hãi": "fear", "so hai": "fear", "hoảng": "fear", "hoảng sợ": "fear",
    # 7. Ngạc nhiên
    "surprised": "surprised", "shocked": "surprised", "astonished": "surprised", "amazed": "surprised",
    "ngac_nhien": "surprised", "ngạc nhiên": "surprised", "ngac nhien": "surprised", "bất ngờ": "surprised", "sững sờ": "surprised",
    # 8. Ghê tởm / khó chịu
    "disgusted": "disgusted", "annoyed": "disgusted", "displeased": "disgusted", "grossed_out": "disgusted",
    "ghe_tom": "disgusted", "ghê tởm": "disgusted", "khó chịu": "disgusted", "kho chiu": "disgusted", "chê": "disgusted",
    # 9. Thân thiện / ấm áp
    "friendly": "friendly", "warm": "friendly", "kind": "friendly", "welcoming": "friendly",
    "than_thien": "friendly", "thân thiện": "friendly", "ấm áp": "friendly", "am ap": "friendly",
    # 10. Đồng cảm / an ủi
    "empathetic": "empathetic", "comforting": "empathetic", "sympathetic": "empathetic", "caring": "empathetic",
    "dong_cam": "empathetic", "đồng cảm": "empathetic", "an ủi": "empathetic", "an ui": "empathetic", "chia sẻ": "empathetic",
    # 11. Nghiêm túc / trang trọng
    "serious": "serious", "formal": "serious", "solemn": "serious", "firm": "serious", "calm": "serious",
    "nghiem_tuc": "serious", "nghiem tuc": "serious", "nghiêm túc": "serious", "trang trọng": "serious",
    # 12. Tự tin / thuyết phục
    "confident": "confident", "persuasive": "confident", "assured": "confident", "determined": "confident",
    "tu_tin": "confident", "tự tin": "confident", "tu tin": "confident", "thuyết phục": "confident", "dứt khoát": "confident",
    # 13. Lo lắng / bồn chồn
    "anxious": "anxious", "nervous": "anxious", "worried": "anxious", "uneasy": "anxious",
    "lo_lang": "anxious", "lo lắng": "anxious", "lo lang": "anxious", "bồn chồn": "anxious", "bất an": "anxious",
    # 14. Ngượng ngùng / e dè
    "shy": "shy", "hesitant": "shy", "bashful": "shy", "awkward": "shy", "embarrassed": "shy",
    "nguong_ngung": "shy", "ngượng ngùng": "shy", "ngượng": "shy", "e dè": "shy", "e de": "shy", "ngại ngùng": "shy",
    # 15. Mỉa mai / châm biếm
    "sarcastic": "sarcastic", "ironic": "sarcastic", "mocking": "sarcastic", "cynical": "sarcastic",
    "mia_mai": "sarcastic", "mỉa mai": "sarcastic", "mia mai": "sarcastic", "châm biếm": "sarcastic", "cà khịa": "sarcastic",
    # 16. Mệt mỏi / uể oải
    "tired": "tired", "exhausted": "tired", "weary": "tired", "drained": "tired",
    "met_moi": "tired", "mệt mỏi": "tired", "met moi": "tired", "uể oải": "tired", "kiệt sức": "tired",
    # 17. Thì thầm / bí mật
    "whisper": "whisper", "secretive": "whisper", "hushed": "whisper",
    "thi_tham": "whisper", "thì thầm": "whisper", "thi tham": "whisper", "bí mật": "whisper", "bi mat": "whisper",
    # 18. Dịu dàng / ru ngủ
    "gentle": "gentle", "soothing": "gentle", "tender": "gentle", "soft": "gentle",
    "diu_dang": "gentle", "dịu dàng": "gentle", "diu dang": "gentle", "ru ngủ": "gentle",
    # D. Trộn cảm xúc (Blended Emotions)
    "happy+anxious": "happy+anxious", "vui+lo lắng": "happy+anxious", "mừng nhưng sợ": "happy+anxious", "relieved_anxious": "happy+anxious",
    "sad+angry": "sad+angry", "angry+sad": "sad+angry", "buồn+tức giận": "sad+angry", "uất ức": "sad+angry", "uat uc": "sad+angry", "resentful": "sad+angry", "bitter": "sad+angry",
    "happy+sarcastic": "happy+sarcastic", "sarcastic+happy": "happy+sarcastic", "vui+mỉa mai": "happy+sarcastic", "cười nhạt": "happy+sarcastic", "cuoi nhat": "happy+sarcastic", "bitter_smile": "happy+sarcastic",
}


def parse_emotion_and_intensity(raw_emotion: str) -> tuple[str, float]:
    """
    Parse an emotion tag that may include:
      - Any of the 18 emotions or Vietnamese equivalents (e.g. 'sad', 'buồn', 'sarcastic', 'mỉa mai', 'angry_cold', 'giận lạnh')
      - Blended emotions separated by '+' or '/' (e.g. 'sad+angry', 'uất ức', 'happy+anxious', 'happy+sarcastic')
      - Optional 3-level intensity suffix:
          * 'light' / 'nhẹ' / '30%'  -> scale 0.50
          * 'medium' / 'vừa' / '60%' -> scale 0.78 (default for most turns)
          * 'strong' / 'mạnh' / '100%' -> scale 1.00 (climax turns)
    Returns (canonical_emotion_key, intensity_scale).
    """
    raw = (raw_emotion or "").strip().lower()
    if not raw:
        return "", 0.78

    intensity_scale = 0.78  # Default: medium (60%) as recommended in Section D
    if ":" in raw:
        emo_part, int_part = raw.split(":", 1)
        raw = emo_part.strip()
        int_part = int_part.strip().rstrip("%")
        if int_part in ("light", "nhe", "nhẹ", "low", "1", "30"):
            intensity_scale = 0.50
        elif int_part in ("medium", "vua", "vừa", "mid", "2", "60"):
            intensity_scale = 0.78
        elif int_part in ("strong", "manh", "mạnh", "high", "3", "100"):
            intensity_scale = 1.00
        else:
            try:
                pct_val = float(int_part)
                intensity_scale = max(0.35, min(1.0, 0.30 + 0.70 * (pct_val / 100.0)))
            except ValueError:
                pass

    if raw in EMOTION_ALIAS_MAP:
        return EMOTION_ALIAS_MAP[raw], intensity_scale

    # Handle arbitrary blended emotion pairs 'emo1+emo2' or 'emo1/emo2'
    if "+" in raw or "/" in raw:
        sep = "+" if "+" in raw else "/"
        parts = [p.strip() for p in raw.split(sep) if p.strip()]
        if len(parts) >= 2:
            c1 = EMOTION_ALIAS_MAP.get(parts[0], parts[0])
            c2 = EMOTION_ALIAS_MAP.get(parts[1], parts[1])
            combo = f"{c1}+{c2}"
            rev_combo = f"{c2}+{c1}"
            if combo in VOCAL_EMOTION_SPECS:
                return combo, intensity_scale
            if rev_combo in VOCAL_EMOTION_SPECS:
                return rev_combo, intensity_scale
            if c1 in VOCAL_EMOTION_SPECS and c2 in VOCAL_EMOTION_SPECS:
                return f"{c1}+{c2}", intensity_scale

    return "", intensity_scale


def get_blended_or_single_spec(emo_key: str) -> Dict[str, Any]:
    """Return the acoustic/acting specification for a single or dynamically blended emotion."""
    if emo_key in VOCAL_EMOTION_SPECS:
        return VOCAL_EMOTION_SPECS[emo_key]
    if "+" in emo_key:
        p1, p2 = emo_key.split("+", 1)
        s1 = VOCAL_EMOTION_SPECS.get(p1, VOCAL_EMOTION_SPECS["normal"])
        s2 = VOCAL_EMOTION_SPECS.get(p2, VOCAL_EMOTION_SPECS["normal"])
        def _dominant(k: str) -> int:
            # Keep the stronger of the two so opposite signs (e.g. sad -26 vs angry +42) do not cancel to neutral.
            return int(round(max(s1[k], s2[k], key=abs)))

        return {
            "pitch_hz": int(round(0.55 * s1["pitch_hz"] + 0.45 * s2["pitch_hz"])),
            "range_pct": _dominant("range_pct"),
            "rate_pct": int(round(0.55 * s1["rate_pct"] + 0.45 * s2["rate_pct"])),
            "energy_pct": int(round(_clamp(_dominant("energy_pct"), EMOTION_MAX_ENERGY_PCT))),
            "rms_target": 0.55 * s1["rms_target"] + 0.45 * s2["rms_target"],
            "end_decay": 0.55 * s1["end_decay"] + 0.45 * s2["end_decay"],
            "pause_sec": round(0.55 * s1["pause_sec"] + 0.45 * s2["pause_sec"], 2),
            "acting": f"blended emotion ({p1} + {p2}): combining ({s1['acting']}) with ({s2['acting']})",
            "nonverbal": s1.get("nonverbal") or s2.get("nonverbal") or "",
        }
    return VOCAL_EMOTION_SPECS["normal"]


def extract_emphasis_keywords(en_text: str) -> List[str]:
    """
    Layer 3 (Word-level emphasis): Identify 1-2 key content words in the sentence so even
    baseline/neutral sentences have natural focal stress and never sound flat or robotic.
    """
    stopwords = {
        "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with",
        "by", "is", "are", "was", "were", "be", "been", "am", "i", "you", "he", "she", "it",
        "we", "they", "me", "him", "her", "us", "them", "my", "your", "his", "their", "our",
        "this", "that", "these", "those", "do", "did", "does", "have", "had", "has", "just",
        "so", "if", "as", "from", "up", "out", "about", "into", "over", "after", "Anna", "Tom",
        "anna", "tom", "jack", "mia",
    }
    words = [re.sub(r"[^A-Za-z']", "", w) for w in (en_text or "").split()]
    candidates = [w for w in words if len(w) >= 4 and w.lower() not in stopwords]
    if not candidates:
        candidates = [w for w in words if len(w) >= 3]
    if len(candidates) <= 2:
        return candidates
    # Pick the most emotionally/semantically salient words (usually mid-sentence verb/adjective and final noun)
    return [candidates[0], candidates[-1]]


def detect_line_emotion(en_text: str, vi_text: str = "", explicit_emotion: str = "") -> str:
    """
    Classify a dialogue line into one of the 18 vocal emotions or 3 blended emotions:
      Group A (Basic 1-8): normal, happy, excited, sad, angry, angry_cold, fear, surprised, disgusted
      Group B (Social 9-18): friendly, empathetic, serious, confident, anxious, shy, sarcastic, tired, whisper, gentle
      Group D (Blended): happy+anxious, sad+angry, happy+sarcastic
    """
    canon, _ = parse_emotion_and_intensity(explicit_emotion)
    if canon:
        return canon

    combined = f"{en_text} {vi_text}".lower()
    en_clean = (en_text or "").strip()

    # Check specific social & nuanced cues first
    if any(k in combined for k in ("whisper", "keep your voice down", "don't let anyone hear", "nói nhỏ thôi", "thì thầm", "bí mật nhé", "suỵt")):
        return "whisper"
    if any(k in combined for k in ("oh great, just wonderful", "yeah right", "how nice of you", "hay lắm cơ", "giỏi quá cơ", "tốt đẹp quá nhỉ", "cười nhạt")):
        return "sarcastic"
    if any(k in combined for k in ("ew,", "gross", "disgusting", "pathetic", "eo ơi", "ghê quá", "tởm", "chướng mắt")):
        return "disgusted"
    if any(k in combined for k in ("what?!", "wait, what", "are you serious?!", "no way!", "hả?", "thật á?", "cái gì cơ", "không thể tin nổi")):
        return "surprised"
    if any(k in combined for k in ("scared", "terrified", "shaking", "someone is there", "afraid", "sợ quá", "hoảng", "run quá", "cứu")):
        return "fear"
    if any(k in combined for k in ("exhausted", "so tired", "drained", "can't do this anymore", "mệt mỏi quá", "kiệt sức", "uể oải", "chán nản")):
        return "tired"
    if any(k in combined for k in ("i understand how hard", "it's okay, i'm here", "must be tough for you", "mình hiểu mà", "đừng buồn nữa", "thương cậu", "an ủi")):
        return "empathetic"
    if any(k in combined for k in ("um,", "uh,", "i'm not sure if", "what if they", "nervous", "worried", "lo quá", "bồn chồn", "không chắc nữa")):
        return "anxious"
    if any(k in combined for k in ("blushing", "embarrassed", "shy", "ngượng", "ngại quá", "xấu hổ")):
        return "shy"
    if any(k in combined for k in ("trust me", "i am certain", "without a doubt", "i decided", "tự tin", "chắc chắn", "quyết tâm", "tin tôi đi")):
        return "confident"
    if any(k in combined for k in ("how could you", "backup plan", "spare tire", "lied to me for months", "lốp dự phòng", "uất ức", "cắm sừng", "phản bội")):
        return "sad+angry"

    angry_cold_cues = (
        "don't ever touch", "delete my number", "we are done", "never call me again",
        "get out of my sight", "xóa số tôi đi", "chấm hết rồi", "đừng bao giờ xuất hiện",
    )
    if any(k in combined for k in angry_cold_cues):
        return "angry_cold"

    angry_cues = (
        "shut up", "lie to me", "lied to me", "sick of", "enough!", "betray",
        "ridiculous", "unbelievable!", "get out", "stop lying", "dare you", "cheated",
        "fake", "hypocrite", "sao anh dám", "sao em dám", "đồ tồi", "im đi", "dối trá",
        "lừa tôi", "quá đáng", "không thể chấp nhận", "tức", "giận",
    )
    sad_cues = (
        "hurts", "heartbroken", "broke my heart", "pain", "sad", "cry", "crying",
        "tears", "alone", "sorry", "regret", "ashamed", "waited for you",
        "missed you", "lonely", "never meant to", "goodbye", "over between us", "buồn", "khóc",
        "đau lòng", "tổn thương", "cô đơn", "xin lỗi", "hối hận", "nghẹn",
    )
    excited_cues = (
        "oh my god!", "you have to see this!", "can't believe we won", "incredible!",
        "trời ơi tin được không", "phải xem cái này", "phấn khích", "hào hứng",
    )
    happy_cues = (
        "so happy", "love you", "thank you so much", "amazing", "wonderful",
        "glad", "relieved", "smile", "smiling", "laugh", "finally!", "best day", "sweet",
        "adorable", "like you too", "vui", "hạnh phúc", "cảm ơn", "tuyệt vời", "thích cậu",
        "yêu anh", "yêu em", "mỉm cười", "nhẹ nhõm", "may quá",
    )
    serious_cues = (
        "listen to me", "tell me the truth", "be honest", "seriously", "promise me",
        "we need to talk", "important", "truth is", "realize", "boundary", "decision",
        "look at me", "nghiêm túc", "nói thật đi", "hứa với", "chúng ta cần nói chuyện",
        "sự thật là", "quyết định", "nhìn thẳng",
    )
    friendly_cues = (
        "let me help you", "good to see you", "don't worry", "welcome", "coffee together",
        "để mình giúp", "rất vui được gặp", "thân thiện", "ấm áp",
    )

    if any(k in combined for k in angry_cues) or en_clean.endswith("?!"):
        return "angry"
    if any(k in combined for k in sad_cues):
        return "sad"
    if any(k in combined for k in excited_cues):
        return "excited"
    if any(k in combined for k in happy_cues):
        return "happy"
    if any(k in combined for k in serious_cues):
        return "serious"
    if any(k in combined for k in friendly_cues):
        return "friendly"
    if en_clean.endswith("!"):
        return "happy"
    return "normal"


def smooth_emotional_arc(flat_items: List[Dict[str, Any]]) -> None:
    """
    Implement Section D (3-Layer Prosody, Smooth Emotion Transitions, 3 Intensity Levels 30%/60%/100%,
    and Section C Sparse Non-Verbal gating):
      1. Layer 1 (Scene Mood): Compute the dominant underlying mood per scene (`scene_mood`).
      2. Layer 2 (Smooth Transition & 3 Intensity Levels):
         - Prevent jarring 1-line flip-flops (EXCEPT 'surprised', which is naturally a 1-turn spike!).
         - Assign intensity ('light' 30% -> 0.50, 'medium' 60% -> 0.78, 'strong' 100% -> 1.00) so most lines
           stay at light-to-medium (30-60%) and only peak climax turns hit strong (100%).
      3. Layer 3 (Word Stress): Extract 1-2 focal keywords (`emphasis_words`) per line.
      4. Section C (Sparse Non-Verbals): Allow non-verbal cues on at most ~18% of lines, spaced >= 4 turns apart.
    """
    n = len(flat_items)
    if n == 0:
        return

    # 1. Parse raw emotion & explicit intensity on each item
    for idx, it in enumerate(flat_items):
        raw_emo = str(it.get("raw_emotion") or it.get("emotion") or "normal")
        canon_emo, explicit_scale = parse_emotion_and_intensity(raw_emo)
        if not canon_emo:
            if raw_emo.strip():
                _log.warning("Unrecognized emotion tag %r (line %d): falling back to text-based detection", raw_emo, idx)
            canon_emo = detect_line_emotion(it.get("en", ""), it.get("vi", ""), raw_emo)
        it["emotion"] = canon_emo
        it["explicit_has_intensity"] = ":" in raw_emo
        it["intensity_scale"] = explicit_scale

    # 2. Smooth abrupt 1-line flip-flops per speaker (except 'surprised' which is a 1-line spike)
    by_sp: Dict[str, List[int]] = {}
    for idx, it in enumerate(flat_items):
        sp_key = str(it.get("speaker") or "_mono_")
        by_sp.setdefault(sp_key, []).append(idx)

    for indices in by_sp.values():
        if len(indices) >= 3:
            for pos in range(1, len(indices) - 1):
                prev_i, curr_i, next_i = indices[pos - 1], indices[pos], indices[pos + 1]
                e_prev = flat_items[prev_i]["emotion"]
                e_curr = flat_items[curr_i]["emotion"]
                e_next = flat_items[next_i]["emotion"]
                if e_curr == "surprised":
                    continue  # Section A.7 & D: Surprise is a short 1-turn spike before transitioning
                if e_prev == e_next and e_curr != e_prev and not flat_items[curr_i].get("explicit_has_intensity"):
                    flat_items[curr_i]["emotion"] = e_prev
                elif (e_prev, e_curr) in (("angry", "happy"), ("happy", "angry"), ("sad", "excited")):
                    flat_items[curr_i]["emotion"] = "serious" if e_prev in ("angry", "sad") else "normal"

    # 3. Compute Layer 1 (Scene Mood), Layer 2 (3-level intensity 30%/60%/100%), Layer 3 (Keywords), and Sparse Non-verbals
    scene_emotions: Dict[int, List[str]] = {}
    for it in flat_items:
        scene_emotions.setdefault(int(it.get("scene_idx", 0)), []).append(it["emotion"])

    scene_mood_map: Dict[int, str] = {}
    for s_idx, emos in scene_emotions.items():
        non_normal = [e for e in emos if e != "normal"]
        dominant = max(set(non_normal), key=non_normal.count) if non_normal else "normal"
        scene_mood_map[s_idx] = dominant

    last_nonverbal_idx = -10
    max_nonverbals = max(1, n // 5)  # At most ~20% of lines get a non-verbal cue (Section C: chèn thưa)
    nonverbal_count = 0

    for idx, it in enumerate(flat_items):
        s_idx = int(it.get("scene_idx", 0))
        emo = it["emotion"]
        it["scene_mood"] = scene_mood_map.get(s_idx, "normal")
        it["emphasis_words"] = extract_emphasis_keywords(it.get("en", ""))

        # Determine 3-level intensity (30% light / 60% medium / 100% strong) if not explicitly tagged
        if not it.get("explicit_has_intensity"):
            prev_emo = flat_items[idx - 1]["emotion"] if idx > 0 else emo
            is_climax_zone = (0.45 * n) <= idx <= (0.82 * n)
            is_exclam = it.get("en", "").strip().endswith("!")
            if emo == "normal":
                it["intensity_scale"] = 0.50  # Light 30% baseline variation
                it["intensity_label"] = "light (30%)"
            elif emo != prev_emo and emo != "surprised":
                # Smooth transition (Section D): start a new emotion at light-to-medium (30-60%)
                it["intensity_scale"] = 0.58
                it["intensity_label"] = "light-medium (45%)"
            elif is_climax_zone and (is_exclam or emo in ("angry", "angry_cold", "sad+angry", "excited", "surprised", "fear")):
                it["intensity_scale"] = 1.00
                it["intensity_label"] = "strong (100%)"
            else:
                it["intensity_scale"] = 0.78
                it["intensity_label"] = "medium (60%)"
        else:
            sc = it["intensity_scale"]
            it["intensity_label"] = "strong (100%)" if sc >= 0.9 else ("light (30%)" if sc <= 0.55 else "medium (60%)")

        # Sparse non-verbal cue assignment (Section C)
        spec = get_blended_or_single_spec(emo)
        nv_hint = spec.get("nonverbal", "")
        if nv_hint and nonverbal_count < max_nonverbals and (idx - last_nonverbal_idx) >= 4 and it["intensity_scale"] >= 0.75:
            it["use_nonverbal"] = True
            last_nonverbal_idx = idx
            nonverbal_count += 1
        else:
            it["use_nonverbal"] = False

        # Compute emotion-specific sentence pause_sec (scaled smoothly)
        base_pause = get_emotion_pause_sec(emo, it["intensity_scale"])
        # Seeded by line index so re-renders of the same script stay identical.
        jitter = random.Random(idx).uniform(1.0 - PAUSE_JITTER, 1.0 + PAUSE_JITTER)
        it["pause_sec"] = round(base_pause * jitter, 2)


def get_emotion_pause_sec(emotion: str, intensity_scale: float = 0.78) -> float:
    """Return the natural post-sentence pause duration (in seconds) for the given emotion and intensity."""
    canon, parsed_scale = parse_emotion_and_intensity(emotion)
    spec = get_blended_or_single_spec(canon or emotion or "normal")
    base_pause = float(spec.get("pause_sec", 0.42))
    scale = intensity_scale if intensity_scale else parsed_scale
    # Blend between baseline 0.40s and the emotion's characteristic pause
    return round(0.40 + (base_pause - 0.40) * scale, 2)


def compute_emotional_prosody(
    text: str,
    base_rate: str = "+0%",
    vi_text: str = "",
    explicit_emotion: str = "",
    intensity_scale: float = 0.0,
    is_male: bool = False,
) -> tuple[str, str, str]:
    """
    Compute (pitch, rate, volume) for Edge TTS across all 18 emotions + blended emotions,
    scaled by the 3-level intensity (30% light / 60% medium / 100% strong).
    """
    m = re.match(r"^([+-]?\d+)%$", (base_rate or "+0%").strip())
    base_r = max(-6, int(m.group(1))) if m else 0
    canon, parsed_scale = parse_emotion_and_intensity(explicit_emotion)
    emo = canon or detect_line_emotion(text, vi_text, explicit_emotion)
    scale = intensity_scale if intensity_scale > 0 else parsed_scale
    spec = get_blended_or_single_spec(emo)

    pitch_factor = MALE_PITCH_HZ_FACTOR if is_male else 1.0
    pitch_val = int(round(_clamp(spec["pitch_hz"] * scale, EMOTION_MAX_PITCH_HZ) * pitch_factor))
    rate_val = int(round(_clamp(base_r + spec["rate_pct"] * scale, EMOTION_MAX_RATE_PCT)))
    vol_val = int(round(_clamp(spec["energy_pct"] * scale, EMOTION_MAX_ENERGY_PCT)))
    return (f"{pitch_val:+d}Hz", f"{rate_val:+d}%", f"{vol_val:+d}%")


GEMINI_LIVE_AUDIO_MODELS = (
    "models/gemini-2.5-flash-native-audio-latest",
    "models/gemini-2.5-flash-native-audio-preview-12-2025",
    "models/gemini-3.1-flash-live-preview",
)


def infer_acting_direction(
    en_text: str,
    vi_text: str = "",
    is_male: bool = False,
    explicit_emotion: str = "",
    scene_mood: str = "normal",
    intensity_label: str = "medium (60%)",
    emphasis_words: Optional[List[str]] = None,
    use_nonverbal: bool = False,
) -> str:
    """
    Build the 3-Layer Vocal Acting Direction (Section A-D):
      - Layer 1: Underlying scene mood (`scene_mood`)
      - Layer 2: Line-specific emotion from the 18-emotion + blended catalog at 30%/60%/100% intensity
      - Layer 3: Key content word emphasis (`emphasis_words`) so delivery is never flat
      - Section C: Optional sparse non-verbal cue when `use_nonverbal` is True
    """
    canon, _ = parse_emotion_and_intensity(explicit_emotion)
    emo = canon or detect_line_emotion(en_text, vi_text, explicit_emotion)
    spec = get_blended_or_single_spec(emo)
    kw = emphasis_words if emphasis_words is not None else extract_emphasis_keywords(en_text)
    kw_str = f", naturally emphasizing key words [{', '.join(kw)}]" if kw else ""
    nv_str = f", subtly including {spec['nonverbal']}" if (use_nonverbal and spec.get("nonverbal")) else ""
    bg_str = f"scene mood: {scene_mood}; " if scene_mood and scene_mood != emo else ""
    return (
        f"{bg_str}line emotion [{emo} at {intensity_label} intensity]: {spec['acting']}"
        f"{kw_str}{nv_str}, smooth connected breath flow"
    )


def apply_acoustic_emotion_shaping(
    pcm_bytes: bytes,
    emotion: str,
    sample_rate: int = 24000,
    intensity_scale: float = 0.78,
    is_question: bool = False,
) -> bytes:
    """
    Preserve 100% pure, untouched studio waveform (NO FFmpeg atempo WSOLA grain-slicing and NO mid-sentence cuts
    that cause 'dực dực' choppiness).
    Applies emotion-aware target RMS energy (matching each of the 18 emotions' Energy spec scaled by intensity),
    a natural end-of-sentence energy falloff (`end_decay`, e.g. softening at the end of `normal`, `sad`, `tired` lines),
    and a smooth 15ms cosine fade-in / 25ms cosine fade-out at line boundaries.
    """
    from array import array

    if len(pcm_bytes) < 4000:
        return pcm_bytes

    if len(pcm_bytes) % 2 != 0:
        pcm_bytes = pcm_bytes[:-1]
    samples = array("h")
    samples.frombytes(pcm_bytes)
    n = len(samples)
    if not n:
        return pcm_bytes

    canon, parsed_scale = parse_emotion_and_intensity(emotion)
    spec = get_blended_or_single_spec(canon or emotion or "normal")
    scale = intensity_scale if intensity_scale > 0 else parsed_scale

    # Blend between baseline RMS (3500) and the emotion's target RMS based on 30%/60%/100% intensity
    target_rms = 3500.0 + (float(spec.get("rms_target", 3500.0)) - 3500.0) * scale
    end_decay = 1.0 - (1.0 - float(spec.get("end_decay", 0.90))) * scale
    if is_question:
        # Questions rise at the end (and Vietnamese hỏi/ngã/nặng tones sit on the final syllable): do not taper.
        end_decay = 1.0

    sum_sq = sum(s * s for s in samples)
    rms = math.sqrt(sum_sq / n)
    gain = 1.0
    if rms > 120.0:
        gain = max(0.55, min(1.65, target_rms / rms))

    fade_in_len = min(int(0.015 * sample_rate), n // 4)
    fade_out_len = min(int(0.025 * sample_rate), n // 4)
    tail_decay_start = int(n * 0.72)  # Final 28% of sentence gently tapers to end_decay

    for i in range(n):
        env = 1.0
        if i < fade_in_len and fade_in_len > 0:
            env = 0.5 * (1.0 - math.cos(math.pi * i / fade_in_len))
        elif i >= n - fade_out_len and fade_out_len > 0:
            rem = n - 1 - i
            env = 0.5 * (1.0 - math.cos(math.pi * rem / fade_out_len))

        if i > tail_decay_start and n > tail_decay_start:
            prog = (i - tail_decay_start) / float(n - tail_decay_start)
            env *= 1.0 - (1.0 - end_decay) * prog

        v = int(samples[i] * gain * env)
        samples[i] = 32767 if v > 32767 else (-32768 if v < -32768 else v)

    return samples.tobytes()


def trim_pcm_silence(pcm_bytes: bytes, sample_rate: int = 24000, threshold: int = 140) -> bytes:
    """
    Trim dead leading/trailing silence from 16-bit mono PCM while keeping 100% of the internal
    sentence waveform completely untouched (zero mid-sentence cutting or splicing).
    """
    from array import array

    if len(pcm_bytes) < 4000:
        return pcm_bytes
    if len(pcm_bytes) % 2 != 0:
        pcm_bytes = pcm_bytes[:-1]
    samples = array("h")
    samples.frombytes(pcm_bytes)
    n = len(samples)
    first_idx = 0
    for i in range(n):
        if abs(samples[i]) > threshold:
            first_idx = i
            break
    last_idx = n - 1
    for j in range(n - 1, -1, -1):
        if abs(samples[j]) > threshold:
            last_idx = j
            break
    if first_idx >= last_idx:
        return pcm_bytes
    keep_lead = int(0.07 * sample_rate)
    keep_trail = int(0.12 * sample_rate)
    start = max(0, first_idx - keep_lead)
    end = min(n, last_idx + keep_trail)
    return samples[start:end].tobytes()


def smooth_internal_stutters(
    pcm_bytes: bytes, sample_rate: int = 24000, max_gap_ms: int = 130
) -> tuple[bytes, int]:
    """
    Trim outer dead silence, then shorten unnatural mid-sentence stalls (e.g. 0.4-0.9s of digital silence after
    'like' / 'and' / 'I' from the Gemini Live model) down to MAX_INTERNAL_PAUSE_MS. Only runs of near-digital
    silence are touched, so consonant closures and breaths stay intact and the cut joins two silent edges
    (no waveform splice inside speech). Returns (cleaned_pcm_bytes, number_of_shortened_gaps).
    """
    from array import array

    trimmed = trim_pcm_silence(pcm_bytes, sample_rate)
    if len(trimmed) < 8000:
        return trimmed, 0
    if len(trimmed) % 2 != 0:
        trimmed = trimmed[:-1]
    samples = array("h")
    samples.frombytes(trimmed)
    win = int(0.01 * sample_rate)
    n_wins = len(samples) // win
    if n_wins < 30:
        return trimmed, 0

    is_silent = []
    for w in range(n_wins):
        chunk = samples[w * win : (w + 1) * win]
        is_silent.append(math.sqrt(sum(x * x for x in chunk) / len(chunk)) < SILENCE_RMS_THRESHOLD)

    keep_wins = max(max_gap_ms, MAX_INTERNAL_PAUSE_MS) // 10
    edge_guard = 7  # never touch the first 70ms / last 110ms (already handled by trim_pcm_silence)
    out = array("h")
    shortened = 0
    w = 0
    while w < n_wins:
        if not is_silent[w] or w < edge_guard or w >= n_wins - 11:
            out.extend(samples[w * win : (w + 1) * win])
            w += 1
            continue
        run_end = w
        while run_end < n_wins - 11 and is_silent[run_end]:
            run_end += 1
        run_len = run_end - w
        if run_len > keep_wins:
            half = keep_wins // 2
            out.extend(samples[w * win : (w + half) * win])
            out.extend(samples[(run_end - (keep_wins - half)) * win : run_end * win])
            shortened += 1
        else:
            out.extend(samples[w * win : run_end * win])
        w = run_end
    out.extend(samples[n_wins * win :])
    return out.tobytes(), shortened


ALL_GEMINI_LIVE_VOICES = {
    "Aoede", "Kore", "Leda", "Zephyr", "Callirrhoe", "Autonoe", "Despina", "Erinome",
    "Laomedeia", "Achernar", "Gacrux", "Pulcherrima", "Sulafat", "Vindemiatrix",
    "Puck", "Fenrir", "Charon", "Orus", "Enceladus", "Iapetus", "Umbriel", "Algieba",
    "Algenib", "Rasalgethi", "Alnilam", "Schedar", "Achird", "Zubenelgenubi", "Sadachbia", "Sadaltager",
}


def resolve_gemini_voice_name(voice: str, is_male: bool) -> str:
    """Resolve UI voice string to a Gemini Live Native Audio voice name."""
    raw = (voice or "").strip()
    if raw.startswith("gemini:"):
        v = raw.split(":", 1)[-1].strip()
        if v:
            return v
    if raw in ALL_GEMINI_LIVE_VOICES:
        return raw
    return "Puck" if is_male else "Aoede"


async def synthesize_chunk_gemini_live_ws(
    gem_voice: str,
    chunk_entries: List[tuple[int, str, str]],
    active_key: str,
    model_name: str = "models/gemini-2.5-flash-native-audio-latest",
    strict_transcript: bool = True,
) -> Dict[int, bytes]:
    """
    Open a fresh Gemini Live BidiGenerateContent WebSocket session with certifi SSL context
    so all lines are spoken 100% by the exact chosen Gemini voice.
    """
    import ssl
    import aiohttp
    import certifi

    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    url = (
        "wss://generativelanguage.googleapis.com/ws/"
        f"google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={active_key}"
    )
    sys_txt = (
        "You are a natural American English voice actor recording a real-life story conversation. "
        "CRITICAL CONSISTENCY, SMOOTHNESS & PACING RULES: "
        "1. MAINTAIN THE EXACT SAME VOCAL IDENTITY, TIMBRE, AND CONVERSATIONAL VOLUME across every turn. "
        "2. DELIVER EVERY SENTENCE IN ONE SMOOTH, CONNECTED, UNBROKEN BREATH FLOW (legato phrasing) — "
        "never speak in a choppy, staccato, or jerky rhythm, and never pause abruptly mid-sentence. "
        "3. NEVER shout/scream and NEVER whisper inaudibly — convey sad, angry, happy, serious, or normal feelings "
        "through warm, genuine emotional inflection at a steady, natural microphone volume. "
        "4. SPEAK AT A NATURAL, BRISK EVERYDAY CONVERSATIONAL SPEED (~160 words per minute). NEVER speak slowly "
        "and NEVER drag out words. "
        "5. Speak ONLY the exact English words inside <read>...</read> verbatim from start to finish."
    )
    results: Dict[int, bytes] = {}
    timeout = aiohttp.ClientTimeout(total=85.0)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.ws_connect(url, ssl=ssl_ctx, heartbeat=20.0) as ws:
            setup_msg = {
                "setup": {
                    "model": model_name,
                    "generationConfig": {
                        "responseModalities": ["AUDIO"],
                        "temperature": 0.45,
                        "speechConfig": {
                            "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": gem_voice}}
                        },
                    },
                    "systemInstruction": {"parts": [{"text": sys_txt}]},
                    "outputAudioTranscription": {},
                }
            }
            await ws.send_json(setup_msg)
            await asyncio.wait_for(ws.receive(), timeout=12.0)

            for idx, clean_text, emo_dir in chunk_entries:
                prompt = (
                    f"Keep your exact same voice and steady volume, speaking at a natural brisk conversational pace ({emo_dir}). "
                    f"Read EVERY word inside the tag from start to finish without stopping early: <read>{clean_text}</read>"
                )
                turn_msg = {
                    "clientContent": {
                        "turns": [{"role": "user", "parts": [{"text": prompt}]}],
                        "turnComplete": True,
                    }
                }
                await ws.send_json(turn_msg)
                pcm = bytearray()
                tr_parts: List[str] = []
                while True:
                    m = await asyncio.wait_for(ws.receive(), timeout=20.0)
                    if m.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
                    raw = m.data.decode("utf-8") if isinstance(m.data, bytes) else m.data
                    if not raw:
                        continue
                    data = json.loads(raw)
                    sc = data.get("serverContent", {})
                    if "outputTranscription" in sc:
                        tr_parts.append(sc["outputTranscription"].get("text", ""))
                    for p in sc.get("modelTurn", {}).get("parts", []):
                        b64 = p.get("inlineData", {}).get("data")
                        if b64:
                            pcm.extend(base64.b64decode(b64))
                    if sc.get("turnComplete"):
                        break
                raw_spoken = "".join(tr_parts)
                spoken = re.sub(r"</?read[^>]*>", " ", raw_spoken, flags=re.IGNORECASE).strip().lower()
                target_words = [w.strip(".,?!'\"-").lower() for w in clean_text.strip().split() if w.strip(".,?!'\"-")]
                spoken_words = [w.strip(".,?!'\"-").lower() for w in spoken.split() if w.strip(".,?!'\"-")]
                bad_direction_leaked = any(
                    bw in spoken and bw not in clean_text.lower()
                    for bw in (
                        "breath", "gasp", "sigh", "trembling", "tone", "direction",
                        "tearful", "conversational", "smoothly", "legato", "continuous",
                    )
                )
                too_long = len(spoken_words) > len(target_words) + 5
                too_short = (
                    strict_transcript
                    and bool(spoken_words)
                    and len(target_words) >= 6
                    and len(spoken_words) < max(2, int(len(target_words) * 0.68))
                )
                last_root = target_words[-1][:4] if target_words else ""
                tail_str = " ".join(spoken_words[-3:]) if spoken_words else ""
                missing_end = (
                    strict_transcript
                    and len(target_words) >= 5
                    and bool(spoken_words)
                    and (target_words[-1] not in spoken_words[-3:] and last_root not in tail_str)
                )
                min_pcm_bytes = max(2200, int(len(target_words) * 0.13 * SAMPLE_RATE * 2))
                cleaned_pcm, _ = smooth_internal_stutters(bytes(pcm), SAMPLE_RATE, max_gap_ms=145)
                if (
                    len(cleaned_pcm) >= min_pcm_bytes
                    and not bad_direction_leaked
                    and not too_long
                    and not too_short
                    and not missing_end
                ):
                    results[idx] = cleaned_pcm
    return results


async def synthesize_batch_gemini_live_ws(
    flat_items: List[Dict[str, Any]],
    work_dir: Path,
    pause_sec: float = 0.38,
    gemini_api_key: str = "",
) -> None:
    """
    Synthesize dialogue turns using Gemini Live Native Audio WebSocket (BidiGenerateContent)
    grouping turns by speaker voice into chunks of up to 4 turns per WebSocket session
    with automatic rate-limit delay/backoff so 100% of sentences use the exact chosen Gemini voice.
    """
    active_key = (gemini_api_key or "").strip() or _load_default_gemini_key() or DEFAULT_GEMINI_KEY
    if not active_key:
        return

    by_voice: Dict[str, List[tuple[int, str, str]]] = {}
    for idx, it in enumerate(flat_items):
        v_raw = str(it.get("voice") or "")
        gem_voice = resolve_gemini_voice_name(v_raw, bool(it.get("is_male")))
        clean_en = sanitize_english_for_tts(it.get("en", ""))
        emo_dir = infer_acting_direction(
            clean_en,
            it.get("vi", ""),
            bool(it.get("is_male")),
            explicit_emotion=str(it.get("emotion") or ""),
            scene_mood=str(it.get("scene_mood") or "normal"),
            intensity_label=str(it.get("intensity_label") or "medium (60%)"),
            emphasis_words=it.get("emphasis_words"),
            use_nonverbal=bool(it.get("use_nonverbal", False)),
        )
        by_voice.setdefault(gem_voice, []).append((idx, clean_en, emo_dir))

    tasks_list: List[tuple[str, List[tuple[int, str, str]]]] = []
    for gem_voice, entries in by_voice.items():
        # Group up to 4 turns per WebSocket session to minimize session handshakes while keeping vocal timbre 100% locked
        for i in range(0, len(entries), 4):
            tasks_list.append((gem_voice, entries[i : i + 4]))

    sem = asyncio.Semaphore(2)
    pcm_map: Dict[int, bytes] = {}
    retry_delays = [2.0, 4.0, 7.0, 11.0, 16.0, 22.0]

    async def run_chunk(gem_v: str, ch: List[tuple[int, str, str]]) -> None:
        async with sem:
            for round_idx, wait_s in enumerate(retry_delays):
                use_strict = round_idx == 0
                for m_name in GEMINI_LIVE_AUDIO_MODELS:
                    try:
                        got = await synthesize_chunk_gemini_live_ws(
                            gem_v, ch, active_key, m_name, strict_transcript=use_strict
                        )
                        if got:
                            pcm_map.update(got)
                        missing_now = [entry for entry in ch if entry[0] not in pcm_map]
                        if not missing_now:
                            break
                        ch = missing_now
                    except Exception as exc:
                        logger.warning("Gemini Live chunk failed (will retry): %s", safe_err(exc))
                        continue
                missing = [entry for entry in ch if entry[0] not in pcm_map]
                if not missing:
                    break
                ch = missing
                # Progressive delay when session/rate-limit window is reached
                await asyncio.sleep(wait_s)
            await asyncio.sleep(0.45)

    await asyncio.gather(*(run_chunk(gv, c) for gv, c in tasks_list))

    for idx, pcm_frames in pcm_map.items():
        wav_p = work_dir / f"line_{idx:03d}.wav"
        item = flat_items[idx]
        emo_tag = str(item.get("emotion") or "normal")
        int_scale = float(item.get("intensity_scale") or 0.78)
        line_pause = float(item.get("pause_sec") or get_emotion_pause_sec(emo_tag, int_scale) or pause_sec)
        pad_frames = b"\x00\x00" * int(line_pause * SAMPLE_RATE)
        shaped_pcm = apply_acoustic_emotion_shaping(
            pcm_frames,
            emo_tag,
            SAMPLE_RATE,
            intensity_scale=int_scale,
            is_question=str(item.get("en") or "").rstrip().endswith("?"),
        )
        all_pcm = shaped_pcm + pad_frames
        with wave.open(str(wav_p), "wb") as out_wf:
            out_wf.setnchannels(1)
            out_wf.setsampwidth(2)
            out_wf.setframerate(SAMPLE_RATE)
            out_wf.writeframes(all_pcm)
        item["wav"] = wav_p
        item["duration"] = (len(all_pcm) // 2) / float(SAMPLE_RATE)


async def synthesize_gemini_tts_rest(
    gem_voice: str,
    clean_text: str,
    emo_dir: str,
    active_key: str,
) -> Optional[bytes]:
    """Fallback to Gemini 2.5 Flash Preview TTS REST API using the exact same Gemini voice."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-tts:generateContent?key={active_key}"
    payload = {
        "contents": [{"parts": [{"text": f"{emo_dir}: {clean_text}"}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": gem_voice}}},
        },
    }
    try:
        async with httpx.AsyncClient(timeout=22.0) as client:
            r = await client.post(url, json=payload)
            if r.status_code == 200:
                parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
                for p in parts:
                    b64 = p.get("inlineData", {}).get("data")
                    if b64:
                        raw_pcm = base64.b64decode(b64)
                        cleaned_pcm, _ = smooth_internal_stutters(raw_pcm, SAMPLE_RATE)
                        if len(cleaned_pcm) > 1800:
                            return cleaned_pcm
    except Exception as exc:
        logger.warning("Gemini REST TTS failed: %s", safe_err(exc))
    return None


async def synthesize_line_wav(
    text: str,
    voice: str,
    rate: str,
    mp3_path: Path,
    wav_path: Path,
    pause_sec: float = 0.42,
    is_male: bool = False,
    line_idx: int = 0,
    gemini_api_key: str = "",
    vi_text: str = "",
    explicit_emotion: str = "",
    scene_mood: str = "normal",
    intensity_label: str = "medium (60%)",
    intensity_scale: float = 0.78,
    emphasis_words: Optional[List[str]] = None,
    use_nonverbal: bool = False,
) -> float:
    """
    Synthesize 1 English sentence using ONLY Gemini voices (Live Native Audio WS + Gemini 2.5 TTS REST).
    If a Gemini session or RPM rate-limit is reached, automatically delays/waits with progressive backoff
    until Gemini succeeds — NEVER mixing in Microsoft Edge-TTS voices mid-video.
    """
    clean_tts_text = sanitize_english_for_tts(text)
    active_key = gemini_api_key.strip() or _load_default_gemini_key() or DEFAULT_GEMINI_KEY
    canon_emo, parsed_scale = parse_emotion_and_intensity(explicit_emotion)
    resolved_emo = canon_emo or detect_line_emotion(clean_tts_text, vi_text, explicit_emotion=explicit_emotion)
    eff_scale = intensity_scale if intensity_scale > 0 else parsed_scale
    eff_pause = get_emotion_pause_sec(resolved_emo, eff_scale) if pause_sec == 0.42 else pause_sec

    if not active_key:
        raise RuntimeError("Thiếu Gemini API Key để tạo giọng đọc Gemini.")

    gem_voice = resolve_gemini_voice_name(voice, is_male)
    emo_dir = infer_acting_direction(
        clean_tts_text,
        vi_text,
        is_male,
        explicit_emotion=resolved_emo,
        scene_mood=scene_mood,
        intensity_label=intensity_label,
        emphasis_words=emphasis_words,
        use_nonverbal=use_nonverbal,
    )
    wait_schedule = [0.0, 2.5, 5.0, 8.0, 12.0, 18.0, 25.0, 30.0, 35.0, 40.0]
    for retry_round, delay_sec in enumerate(wait_schedule):
        if delay_sec > 0:
            await asyncio.sleep(delay_sec)
        use_strict = retry_round == 0
        for m_name in GEMINI_LIVE_AUDIO_MODELS:
            try:
                got = await synthesize_chunk_gemini_live_ws(
                    gem_voice,
                    [(line_idx, clean_tts_text, emo_dir)],
                    active_key,
                    m_name,
                    strict_transcript=use_strict,
                )
                if line_idx in got:
                    pcm_frames = apply_acoustic_emotion_shaping(
                        got[line_idx],
                        resolved_emo,
                        SAMPLE_RATE,
                        intensity_scale=eff_scale,
                        is_question=clean_tts_text.rstrip().endswith("?"),
                    )
                    pad_frames = b"\x00\x00" * int(eff_pause * SAMPLE_RATE)
                    all_pcm = pcm_frames + pad_frames
                    with wave.open(str(wav_path), "wb") as out_wf:
                        out_wf.setnchannels(1)
                        out_wf.setsampwidth(2)
                        out_wf.setframerate(SAMPLE_RATE)
                        out_wf.writeframes(all_pcm)
                    return (len(all_pcm) // 2) / float(SAMPLE_RATE)
            except Exception as exc:
                logger.warning("Single-line Gemini Live synth failed: %s", safe_err(exc))
                continue

        rest_pcm = await synthesize_gemini_tts_rest(gem_voice, clean_tts_text, emo_dir, active_key)
        if rest_pcm:
            shaped_pcm = apply_acoustic_emotion_shaping(
                rest_pcm,
                resolved_emo,
                SAMPLE_RATE,
                intensity_scale=eff_scale,
                is_question=clean_tts_text.rstrip().endswith("?"),
            )
            pad_frames = b"\x00\x00" * int(eff_pause * SAMPLE_RATE)
            all_pcm = shaped_pcm + pad_frames
            with wave.open(str(wav_path), "wb") as out_wf:
                out_wf.setnchannels(1)
                out_wf.setsampwidth(2)
                out_wf.setframerate(SAMPLE_RATE)
                out_wf.writeframes(all_pcm)
            return (len(all_pcm) // 2) / float(SAMPLE_RATE)

    raise RuntimeError(f"Gemini TTS vượt quá thời gian chờ phiên ở câu #{line_idx + 1}: {clean_tts_text}")




def concat_wav_files(wav_paths: List[Path], out_wav: Path) -> float:
    """Concatenate PCM WAV files with 0 sample loss and return total duration in seconds."""
    total_frames = 0
    with wave.open(str(out_wav), "wb") as out_wf:
        out_wf.setnchannels(1)
        out_wf.setsampwidth(2)
        out_wf.setframerate(SAMPLE_RATE)
        for p in wav_paths:
            with wave.open(str(p), "rb") as in_wf:
                data = in_wf.readframes(in_wf.getnframes())
                out_wf.writeframes(data)
                total_frames += len(data) // 2
    return total_frames / float(SAMPLE_RATE)


DEFAULT_INTRO_WAV = Path(__file__).parent / "assets" / "intro_signature.wav"
DEFAULT_TRANSITION_WAV = Path(__file__).parent / "assets" / "part_transition.wav"


def create_silent_wav(duration_sec: float, out_wav: Path) -> None:
    num_frames = int(duration_sec * SAMPLE_RATE)
    with wave.open(str(out_wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"\x00\x00" * num_frames)


def create_intro_wav(duration_sec: float, out_wav: Path, is_transition: bool = False) -> None:
    """Build the opening or part-transition WAV using the signature chime padded to duration_sec."""
    src_wav = (
        DEFAULT_TRANSITION_WAV
        if (is_transition and DEFAULT_TRANSITION_WAV.exists())
        else DEFAULT_INTRO_WAV
    )
    if src_wav.exists():
        cmd = [
            FFMPEG_EXE,
            "-y",
            "-i",
            str(src_wav),
            "-af",
            f"apad=whole_dur={duration_sec:.3f}",
            "-t",
            f"{duration_sec:.3f}",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(out_wav),
        ]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if res.returncode == 0 and out_wav.exists() and out_wav.stat().st_size > 1000:
            return
    create_silent_wav(duration_sec, out_wav)


def render_section_mp4(
    slides: List[tuple[Path, float]],
    audio_wav: Path,
    out_mp4: Path,
) -> None:
    """
    Encode one section (Intro, Part 1, Part 2, or Part 3) from [(image_path, duration), ...] + audio_wav.
    Using ultrafast stillimage H.264 + AAC so all 4 sections can be stitched with -c copy.
    """
    concat_txt = out_mp4.parent / f"{out_mp4.stem}_slides.txt"
    lines = []
    for img_path, dur in slides:
        safe_path = str(img_path.resolve()).replace("\\", "/")
        lines.append(f"file '{safe_path}'")
        lines.append(f"duration {dur:.4f}")
    last_safe = str(slides[-1][0].resolve()).replace("\\", "/")
    lines.append(f"file '{last_safe}'")
    concat_txt.write_text("\n".join(lines), encoding="utf-8")

    cmd = [
        FFMPEG_EXE,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_txt),
        "-i",
        str(audio_wav),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "16",
        "-tune",
        "stillimage",
        "-r",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        "1",
        "-shortest",
        str(out_mp4),
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)


def format_mmss(seconds: float) -> str:
    total = int(round(seconds))
    m, s = divmod(total, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def format_srt_time(seconds: float) -> str:
    total_ms = int(round(max(0.0, seconds) * 1000))
    total, ms = divmod(total_ms, 1000)
    m, s = divmod(total, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def render_fast_test_slide_card(
    en_text: str,
    vi_text: str,
    out_path: Path,
    speaker: str = "",
    emotion: str = "",
    intensity_label: str = "",
) -> None:
    """
    Render a lightweight 480p (854x480) bilingual test slide card in <1ms so test videos
    encode almost instantaneously while showing speaker, emotion badge, English, and Vietnamese.
    """
    w, h = 854, 480
    cx = w // 2
    img = Image.new("RGB", (w, h), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Top badge showing Speaker + Emotion + Intensity for quick voice/acting verification
    f_badge = get_font(18, bold=True)
    sp_part = f"{speaker} • " if speaker else ""
    emo_part = f"[{emotion}]" if emotion else "[normal]"
    int_part = f" ({intensity_label})" if intensity_label else ""
    badge_str = f"⚡ BẢN TEST NHANH  |  {sp_part}{emo_part}{int_part}"
    draw.rounded_rectangle([cx - 260, 22, cx + 260, 56], radius=17, fill=(28, 36, 52), outline=(56, 189, 248), width=1)
    draw.text((cx, 39), badge_str, font=f_badge, fill="#38BDF8", anchor="mm")

    f_en = get_font(28, bold=True)
    f_vi = get_font(23, bold=True)
    en_prefix = f"{speaker}: {en_text}" if speaker else en_text
    en_lines = wrap_text_lines(draw, en_prefix, f_en, 760)
    vi_lines = wrap_text_lines(draw, vi_text, f_vi, 760) if vi_text else []

    y = 140 if len(en_lines) <= 2 else 115
    for line in en_lines[:4]:
        draw.text((cx, y), line, font=f_en, fill="#FFFFFF", anchor="mm")
        y += 38

    y += 18
    for line in vi_lines[:4]:
        draw.text((cx, y), line, font=f_vi, fill="#FFAE00", anchor="mm")
        y += 32

    img.save(out_path, "PNG", compress_level=1)


async def build_video_job(
    job_dir: Path,
    topic: str,
    level: str = "B1",
    category: str = "TIẾNG ANH HỌC THUẬT",
    podcast_mode: str = "dialogue",
    voice: str = "gemini:Aoede",
    voice_male: str = "gemini:Puck",
    rate: str = "-5%",
    target_sentences: int = 10,
    channel_tag: str = "REACH PODCAST",
    gemini_api_key: str = "",
    custom_script: str = "",
    subtitle_style: str = "teleprompter",
    sensitivity_mode: str = "normal",
    pair_type: str = "male_female",
    fast_test: bool = False,
    on_progress: Optional[Callable[[str, int], None]] = None,
) -> Dict[str, Any]:
    """
    Full end-to-end pipeline for 1 video (supports both 2-speaker 'dialogue' and 1-speaker 'monologue').
    Renders pure black background videos in both Horizontal (16:9) & Vertical (9:16), plus TikTok climax cuts.
    """
    def report(msg: str, pct: int) -> None:
        if on_progress:
            on_progress(msg, pct)

    job_dir.mkdir(parents=True, exist_ok=True)
    work_dir = job_dir / "work"
    work_dir.mkdir(exist_ok=True)

    ptype = (pair_type or "male_female").strip().lower()
    if ptype == "male_male":
        sp1_default, sp2_default = "Tom", "Jack"
        mode_label = "Đối thoại Nam - Nam (Tom & Jack)" if podcast_mode == "dialogue" else "Độc thoại 1 người"
    elif ptype == "female_female":
        sp1_default, sp2_default = "Anna", "Mia"
        mode_label = "Đối thoại Nữ - Nữ (Anna & Mia)" if podcast_mode == "dialogue" else "Độc thoại 1 người"
    else:
        sp1_default, sp2_default = "Anna", "Tom"
        mode_label = "Đối thoại Nam - Nữ (Anna & Tom)" if podcast_mode == "dialogue" else "Độc thoại 1 người"

    report(f"1/5 Đang viết kịch bản {mode_label} song ngữ Anh - Việt...", 8)
    script = await generate_script(
        topic=topic,
        level=level,
        category=category,
        target_sentences=target_sentences,
        podcast_mode=podcast_mode,
        gemini_api_key=gemini_api_key,
        custom_script=custom_script,
        sensitivity_mode=sensitivity_mode,
        pair_type=ptype,
    )
    if custom_script and custom_script.strip():
        podcast_mode = str(script.get("podcast_mode") or podcast_mode)
        ptype = str(script.get("pair_type") or ptype).strip().lower()
        if ptype == "male_male":
            sp1_default, sp2_default = "Tom", "Jack"
            if not _is_male_voice_id(voice):
                voice = "gemini:Puck"
            if not _is_male_voice_id(voice_male) or voice_male == voice:
                voice_male = "gemini:Fenrir" if voice != "gemini:Fenrir" else "gemini:Puck"
        elif ptype == "female_female":
            sp1_default, sp2_default = "Anna", "Mia"
            if _is_male_voice_id(voice):
                voice = "gemini:Aoede"
            if _is_male_voice_id(voice_male) or voice_male == voice:
                voice_male = "gemini:Leda" if voice != "gemini:Leda" else "gemini:Aoede"
        else:
            sp1_default, sp2_default = "Anna", "Tom"
    (job_dir / "script.json").write_text(
        json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    scenes = script.get("scenes", [])
    if not scenes:
        raise ValueError("Script has no scenes.")

    base_seed = int(time.time()) % 100000
    thumb_bg_path = work_dir / "thumb_bg.jpg"
    if fast_test:
        report("2/5 [Test Nhanh] Tạo nhanh bìa Thumbnail nháp...", 22)
        thumb_bg = make_fallback_anime_bg(base_seed, topic=script.get("title_vi", topic))
        thumb_bg.save(thumb_bg_path, "JPEG", quality=80)
    else:
        report("2/5 Đang vẽ nhân vật Webtoon 2D & tạo Thumbnail bìa 4K...", 22)
        thumb_bg = await fetch_anime_image(
            script.get("thumb_prompt", topic),
            thumb_bg_path,
            seed=base_seed,
            gemini_api_key=gemini_api_key,
            sensitivity_mode=sensitivity_mode,
            topic=script.get("title_vi", topic),
        )

    thumb_out = job_dir / "thumbnail.jpg"
    render_thumbnail(
        bg_img=thumb_bg,
        level=script.get("level", level),
        title_vi=script.get("title_vi", topic),
        category=script.get("category", category),
        channel_tag=channel_tag,
        out_path=thumb_out,
    )

    # 3. Flatten lines and resolve speaker + voice per turn
    flat_items: List[Dict[str, Any]] = []
    turn_idx = 0
    for s_idx, sc in enumerate(scenes):
        for line in sc.get("lines", []):
            raw_en = line.get("en", "").strip()
            if not raw_en:
                continue
            default_sp = (
                line.get("speaker", "").strip()
                or ((sp1_default if turn_idx % 2 == 0 else sp2_default) if podcast_mode == "dialogue" else "")
            )
            sp, parsed_emo, clean_en = extract_speaker_and_emotion_prefix(raw_en, default_sp)
            _, _, clean_vi = extract_speaker_and_emotion_prefix(line.get("vi", "").strip(), "")
            raw_line_emo = str(line.get("emotion") or parsed_emo or "").strip().lower()
            resolved_emo = detect_line_emotion(clean_en, clean_vi, explicit_emotion=raw_line_emo)
            if podcast_mode == "dialogue":
                sp_low = sp.lower()
                if sp_low == sp2_default.lower() or sp_low in ("speaker b", "speaker 2", "b"):
                    is_second_speaker = True
                    sp = sp2_default
                elif sp_low == sp1_default.lower() or sp_low in ("speaker a", "speaker 1", "a"):
                    is_second_speaker = False
                    sp = sp1_default
                else:
                    is_second_speaker = (turn_idx % 2 == 1)
                    sp = sp2_default if is_second_speaker else sp1_default
                chosen_voice = voice_male if is_second_speaker else voice
            else:
                chosen_voice = voice
            is_male_turn = _is_male_voice_id(chosen_voice)
            flat_items.append(
                {
                    "scene_idx": s_idx,
                    "speaker": sp if podcast_mode == "dialogue" else "",
                    "raw_emotion": raw_line_emo,
                    "emotion": resolved_emo,
                    "is_male": is_male_turn,
                    "voice": chosen_voice,
                    "en": clean_en,
                    "vi": clean_vi,
                }
            )
            turn_idx += 1

    smooth_emotional_arc(flat_items)

    report(f"3/5 Đang thu âm 100% giọng Gemini diễn xuất 18 sắc thái ({len(flat_items)} lượt thoại)...", 45)
    try:
        await synthesize_batch_gemini_live_ws(
            flat_items,
            work_dir,
            pause_sec=0.38,
            gemini_api_key=gemini_api_key,
        )
    except Exception as exc:
        logger.warning("Batch Gemini Live synth failed, per-line fallback will run: %s", safe_err(exc))

    sem = asyncio.Semaphore(1)

    async def synth_one(idx: int, item: Dict[str, Any]) -> float:
        if "wav" in item and item.get("duration", 0) > 0:
            return item["duration"]
        async with sem:
            mp3_p = work_dir / f"line_{idx:03d}.mp3"
            wav_p = work_dir / f"line_{idx:03d}.wav"
            dur = await synthesize_line_wav(
                item["en"],
                item["voice"],
                rate,
                mp3_p,
                wav_p,
                pause_sec=float(item.get("pause_sec") or 0.42),
                is_male=item["is_male"],
                line_idx=idx,
                gemini_api_key=gemini_api_key,
                vi_text=item.get("vi", ""),
                explicit_emotion=item.get("emotion", ""),
                scene_mood=str(item.get("scene_mood") or "normal"),
                intensity_label=str(item.get("intensity_label") or "medium (60%)"),
                intensity_scale=float(item.get("intensity_scale") or 0.78),
                emphasis_words=item.get("emphasis_words"),
                use_nonverbal=bool(item.get("use_nonverbal", False)),
            )
            item["wav"] = wav_p
            item["duration"] = dur
            return dur

    await asyncio.gather(*(synth_one(i, it) for i, it in enumerate(flat_items)))

    voice_body_wav = work_dir / "voice_body.wav"
    body_duration = concat_wav_files([it["wav"] for it in flat_items], voice_body_wav)

    if fast_test:
        report("4/5 [Test Nhanh] Đang dựng video 480p tối thiểu (1 phần song ngữ)...", 78)
        test_slides: List[tuple[Path, float]] = []
        for i, it in enumerate(flat_items):
            t_png = work_dir / f"test_{i:03d}.png"
            render_fast_test_slide_card(
                en_text=it["en"],
                vi_text=it["vi"],
                out_path=t_png,
                speaker=it["speaker"],
                emotion=str(it.get("emotion") or "normal"),
                intensity_label=str(it.get("intensity_label") or ""),
            )
            test_slides.append((t_png, it["duration"]))

        def _encode_fast_test_mp4() -> None:
            concat_txt = work_dir / "test_slides.txt"
            lines = []
            for img_p, dur in test_slides:
                safe_p = str(img_p.resolve()).replace("\\", "/")
                lines.append(f"file '{safe_p}'")
                lines.append(f"duration {dur:.4f}")
            last_safe = str(test_slides[-1][0].resolve()).replace("\\", "/")
            lines.append(f"file '{last_safe}'")
            concat_txt.write_text("\n".join(lines), encoding="utf-8")

            final_mp4 = job_dir / "video_3_phan.mp4"
            cmd = [
                FFMPEG_EXE,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_txt),
                "-i",
                str(voice_body_wav),
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "30",
                "-tune",
                "stillimage",
                "-r",
                "8",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-ar",
                str(SAMPLE_RATE),
                "-ac",
                "1",
                "-shortest",
                str(final_mp4),
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)

        await asyncio.to_thread(_encode_fast_test_mp4)

        yt_title = f"⚡ [BẢN TEST NHANH 480P] Level {script.get('level', level)} - {script.get('title_vi', topic).upper()}"
        full_script_en = "\n".join(
            f"{it['speaker']} [{it.get('emotion', 'normal')}]: {it['en']} | {it['vi']}"
            if it["speaker"]
            else f"[{it.get('emotion', 'normal')}]: {it['en']} | {it['vi']}"
            for it in flat_items
        )
        yt_desc = (
            f"{yt_title}\n\n"
            f"Chế độ: Video chất lượng tối thiểu (480p • 1 phần song ngữ) để test giọng đọc & kịch bản nhanh.\n\n"
            f"script video:\n{full_script_en}\n"
        )
        (job_dir / "youtube_description.txt").write_text(yt_desc, encoding="utf-8")

        srt_lines = []
        curr_t = 0.0
        for idx, it in enumerate(flat_items, start=1):
            st = format_srt_time(curr_t)
            et = format_srt_time(curr_t + it["duration"])
            en_line = f"{it['speaker']}: {it['en']}" if it["speaker"] else it["en"]
            srt_lines.append(f"{idx}\n{st} --> {et}\n{en_line}\n{it['vi']}\n")
            curr_t += it["duration"]
        (job_dir / "subtitles_bilingual.srt").write_text("\n".join(srt_lines), encoding="utf-8")

        shutil.rmtree(work_dir, ignore_errors=True)
        report("Hoàn tất bản Test Nhanh 100%!", 100)
        return {
            "title": yt_title,
            "title_vi": script.get("title_vi", topic),
            "level": script.get("level", level),
            "category": "⚡ TEST NHANH (480P)",
            "podcast_mode": podcast_mode,
            "sensitivity_mode": sensitivity_mode,
            "fast_test": True,
            "duration_formatted": format_mmss(body_duration),
            "chapters": {
                "part1": "00:00",
                "part2": format_mmss(body_duration),
                "part3": format_mmss(body_duration),
            },
            "sentence_count": len(flat_items),
            "video_file": "video_3_phan.mp4",
            "video_vertical_file": "",
            "tiktok_vertical_file": "",
            "tiktok_horizontal_file": "",
            "thumbnail_file": "thumbnail.jpg",
            "description_file": "youtube_description.txt",
            "srt_file": "subtitles_bilingual.srt",
        }

    # 4. Render transition cards & slide cards on pure black (#000000) for both 16:9 and 9:16
    report("4/5 Đang dựng phụ đề nền đen (Ngang 16:9 & Dọc 9:16 + Bản cắt TikTok)...", 68)
    await asyncio.sleep(0.05)
    trans1_dur = 3.0
    trans2_dur = 2.4
    trans3_dur = 2.4

    trans1_png = work_dir / "trans1.png"
    trans2_png = work_dir / "trans2.png"
    trans3_png = work_dir / "trans3.png"
    render_part_transition_card(1, trans1_png, aspect="16:9")
    render_part_transition_card(2, trans2_png, aspect="16:9")
    render_part_transition_card(3, trans3_png, aspect="16:9")

    trans1_v_png = work_dir / "trans1_v.png"
    trans2_v_png = work_dir / "trans2_v.png"
    trans3_v_png = work_dir / "trans3_v.png"
    render_part_transition_card(1, trans1_v_png, aspect="9:16")
    render_part_transition_card(2, trans2_v_png, aspect="9:16")
    render_part_transition_card(3, trans3_v_png, aspect="9:16")

    trans1_wav = work_dir / "trans1.wav"
    trans2_wav = work_dir / "trans2.wav"
    trans3_wav = work_dir / "trans3.wav"
    create_intro_wav(trans1_dur, trans1_wav, is_transition=False)
    create_intro_wav(trans2_dur, trans2_wav, is_transition=True)
    create_intro_wav(trans3_dur, trans3_wav, is_transition=True)

    scene_durations = [0.0] * len(scenes)
    for it in flat_items:
        scene_durations[it["scene_idx"]] += it["duration"]

    part1_slides: List[tuple[Path, float]] = []
    part2_slides: List[tuple[Path, float]] = []
    part1_v_slides: List[tuple[Path, float]] = []
    part2_v_slides: List[tuple[Path, float]] = []
    for i, it in enumerate(flat_items):
        if subtitle_style != "teleprompter":
            p1_png = work_dir / f"p1_{i:03d}.png"
            p1_v_png = work_dir / f"p1_v_{i:03d}.png"
            render_slide_card(None, "part1", it["en"], it["vi"], p1_png, speaker=it["speaker"], aspect="16:9")
            render_slide_card(None, "part1", it["en"], it["vi"], p1_v_png, speaker=it["speaker"], aspect="9:16")
            part1_slides.append((p1_png, it["duration"]))
            part1_v_slides.append((p1_v_png, it["duration"]))
        p2_png = work_dir / f"p2_{i:03d}.png"
        p2_v_png = work_dir / f"p2_v_{i:03d}.png"
        render_slide_card(None, "part2", it["en"], it["vi"], p2_png, speaker=it["speaker"], aspect="16:9")
        render_slide_card(None, "part2", it["vi"], it["vi"], p2_v_png, speaker=it["speaker"], aspect="9:16") if False else render_slide_card(None, "part2", it["en"], it["vi"], p2_v_png, speaker=it["speaker"], aspect="9:16")
        part2_slides.append((p2_png, it["duration"]))
        part2_v_slides.append((p2_v_png, it["duration"]))

    # 5. Encode Horizontal (16:9) + Vertical (9:16) 3-part videos + TikTok cuts
    report("5/5 Đang xuất MP4 Ngang (16:9), Dọc (9:16) & Bản cắt TikTok...", 78)
    await asyncio.sleep(0.05)

    def _encode_all_videos() -> None:
        sec_trans1_mp4 = work_dir / "sec_trans1.mp4"
        sec_trans2_mp4 = work_dir / "sec_trans2.mp4"
        sec_trans3_mp4 = work_dir / "sec_trans3.mp4"
        sec1_mp4 = work_dir / "sec1_part1.mp4"
        sec2_mp4 = work_dir / "sec2_part2.mp4"
        sec3_mp4 = work_dir / "sec3_part3.mp4"

        sec_trans1_v_mp4 = work_dir / "sec_trans1_v.mp4"
        sec_trans2_v_mp4 = work_dir / "sec_trans2_v.mp4"
        sec_trans3_v_mp4 = work_dir / "sec_trans3_v.mp4"
        sec1_v_mp4 = work_dir / "sec1_part1_v.mp4"
        sec2_v_mp4 = work_dir / "sec2_part2_v.mp4"
        sec3_v_mp4 = work_dir / "sec3_part3_v.mp4"

        render_section_mp4([(trans1_png, trans1_dur)], trans1_wav, sec_trans1_mp4)
        render_section_mp4([(trans2_png, trans2_dur)], trans2_wav, sec_trans2_mp4)
        render_section_mp4([(trans3_png, trans3_dur)], trans3_wav, sec_trans3_mp4)

        render_section_mp4([(trans1_v_png, trans1_dur)], trans1_wav, sec_trans1_v_mp4)
        render_section_mp4([(trans2_v_png, trans2_dur)], trans2_wav, sec_trans2_v_mp4)
        render_section_mp4([(trans3_v_png, trans3_dur)], trans3_wav, sec_trans3_v_mp4)

        if subtitle_style == "teleprompter":
            render_scrolling_section_mp4(flat_items, None, "part1", voice_body_wav, sec1_mp4, fps=20, aspect="16:9")
            render_scrolling_section_mp4(flat_items, None, "part1", voice_body_wav, sec1_v_mp4, fps=20, aspect="9:16")
        else:
            render_section_mp4(part1_slides, voice_body_wav, sec1_mp4)
            render_section_mp4(part1_v_slides, voice_body_wav, sec1_v_mp4)

        render_section_mp4(part2_slides, voice_body_wav, sec2_mp4)
        render_section_mp4(part2_v_slides, voice_body_wav, sec2_v_mp4)
        render_part3_audio_ring_mp4(
            None, scene_durations, voice_body_wav, sec3_mp4, fps=20, out_vertical_mp4=sec3_v_mp4
        )

        final_list_txt = work_dir / "final_concat.txt"
        final_list_txt.write_text(
            "\n".join(
                f"file '{str(p.resolve()).replace(chr(92), '/')}'"
                for p in (sec_trans1_mp4, sec1_mp4, sec_trans2_mp4, sec2_mp4, sec_trans3_mp4, sec3_mp4)
            ),
            encoding="utf-8",
        )
        final_mp4 = job_dir / "video_3_phan.mp4"
        subprocess.run(
            [
                FFMPEG_EXE,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(final_list_txt),
                "-c",
                "copy",
                str(final_mp4),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
        )

        final_v_list_txt = work_dir / "final_v_concat.txt"
        final_v_list_txt.write_text(
            "\n".join(
                f"file '{str(p.resolve()).replace(chr(92), '/')}'"
                for p in (sec_trans1_v_mp4, sec1_v_mp4, sec_trans2_v_mp4, sec2_v_mp4, sec_trans3_v_mp4, sec3_v_mp4)
            ),
            encoding="utf-8",
        )
        final_v_mp4 = job_dir / "video_3_phan_doc.mp4"
        subprocess.run(
            [
                FFMPEG_EXE,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(final_v_list_txt),
                "-c",
                "copy",
                str(final_v_mp4),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
        )

        # Render standalone TikTok climax cut in both Vertical (9:16) and Horizontal (16:9)
        tiktok_v_mp4 = job_dir / "video_tiktok_doc.mp4"
        tiktok_h_mp4 = job_dir / "video_tiktok_ngang.mp4"
        render_tiktok_cut_mp4(
            flat_items=flat_items,
            level=script.get("level", level),
            work_dir=work_dir,
            out_vertical_mp4=tiktok_v_mp4,
            out_horizontal_mp4=tiktok_h_mp4,
        )

    await asyncio.to_thread(_encode_all_videos)

    # Calculate YouTube chapters
    t_part1 = 0.0
    t_part2 = trans1_dur + body_duration
    t_part3 = trans1_dur + body_duration + trans2_dur + body_duration
    t_total = trans1_dur + trans2_dur + trans3_dur + body_duration * 3

    yt_title = f"🎧 Luyện Nghe Tiếng Anh Level {script.get('level', level)} - {script.get('title_vi', topic).upper()} - | Listening English Podcast |🎯"
    if podcast_mode == "dialogue":
        full_script_en = "\n".join(
            f"{it['speaker']}: {it['en']}" if it["speaker"] else it["en"] for it in flat_items
        )
    else:
        full_script_en = "\n\n".join(
            " ".join(line["en"] for line in sc.get("lines", [])) for sc in scenes
        )

    yt_desc = (
        f"{yt_title}\n\n"
        f"nội dung video sẽ có 3 phần:\n"
        f"{format_mmss(t_part1)} - Part 1: English Subtitles\n"
        f"{format_mmss(t_part2)} - Part 2: Vietnamese Subtitles\n"
        f"{format_mmss(t_part3)} - Part 3: No Subtitles\n\n"
        f"script video:\n{full_script_en}\n\n"
        f"#LearnEnglish #EnglishListening #{script.get('level', level)}English #EnglishForBeginners "
        f"#LuyenNgheTiengAnh #learnenglishpodcast #englishpodcast #ieltslistening #tienganh"
    )
    (job_dir / "youtube_description.txt").write_text(yt_desc, encoding="utf-8")

    # Export bilingual SRT
    srt_lines = []
    curr_t = trans1_dur
    for idx, it in enumerate(flat_items, start=1):
        st = format_srt_time(curr_t)
        et = format_srt_time(curr_t + it["duration"])
        en_line = f"{it['speaker']}: {it['en']}" if it["speaker"] else it["en"]
        srt_lines.append(f"{idx}\n{st} --> {et}\n{en_line}\n{it['vi']}\n")
        curr_t += it["duration"]
    (job_dir / "subtitles_bilingual.srt").write_text("\n".join(srt_lines), encoding="utf-8")

    shutil.rmtree(work_dir, ignore_errors=True)

    report("Hoàn tất 100%!", 100)
    return {
        "title": yt_title,
        "title_vi": script.get("title_vi", topic),
        "level": script.get("level", level),
        "category": script.get("category", category),
        "podcast_mode": podcast_mode,
        "sensitivity_mode": sensitivity_mode,
        "duration_formatted": format_mmss(t_total),
        "chapters": {
            "part1": format_mmss(t_part1),
            "part2": format_mmss(t_part2),
            "part3": format_mmss(t_part3),
        },
        "sentence_count": len(flat_items),
        "video_file": "video_3_phan.mp4",
        "video_vertical_file": "video_3_phan_doc.mp4",
        "tiktok_vertical_file": "video_tiktok_doc.mp4",
        "tiktok_horizontal_file": "video_tiktok_ngang.mp4",
        "thumbnail_file": "thumbnail.jpg",
        "description_file": "youtube_description.txt",
        "srt_file": "subtitles_bilingual.srt",
    }
