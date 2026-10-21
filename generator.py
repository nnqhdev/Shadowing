import asyncio
import base64
import io
import json
import math
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import wave
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import edge_tts
import httpx
import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

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
IMAGE_SEM = asyncio.Semaphore(2)
_GEMINI_IMG_DISABLED_KEYS: set[str] = set()
_USED_IMAGE_URLS: set[str] = set()


def get_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    """Load a Windows font that supports full Vietnamese diacritics."""
    win_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    candidates = (
        ["segoeuib.ttf", "arialbd.ttf", "tahomabd.ttf", "calibrib.ttf"]
        if bold
        else ["segoeui.ttf", "arial.ttf", "tahoma.ttf", "calibri.ttf"]
    )
    for name in candidates:
        p = win_fonts / name
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
    non_repeating = [p for p in matched if p["id"] != _LAST_SCENE_PROFILE_ID]
    if not non_repeating:
        non_repeating = [p for p in VARIED_SCENE_PROFILES if p["id"] != _LAST_SCENE_PROFILE_ID]

    idx = abs(hash((topic or "") + str(seed))) % len(non_repeating)
    chosen = non_repeating[idx]
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
            except Exception:
                pass
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

    async with IMAGE_SEM:
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
            if active_key and active_key not in _GEMINI_IMG_DISABLED_KEYS:
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
                            _GEMINI_IMG_DISABLED_KEYS.add(active_key)
                            break
                    except Exception:
                        pass

    img = make_fallback_anime_bg(seed, topic=topic or prompt)
    img.save(out_path, "JPEG", quality=95)
    return img


def extract_speaker_prefix(text: str, default_speaker: str = "") -> tuple[str, str]:
    """Extract 'Anna: ...' or 'Tom: ...' prefix so TTS never reads the speaker name out loud."""
    m = re.match(r"^(Anna|Tom|Speaker\s*[AB12]|Nữ|Nam)\s*:\s*(.+)$", text.strip(), flags=re.IGNORECASE)
    if m:
        raw_sp = m.group(1).strip().lower()
        sp = "Tom" if raw_sp in ("tom", "speaker b", "speaker 2", "nam") else "Anna"
        return sp, m.group(2).strip()
    return default_speaker, text.strip()


def parse_custom_script_text(
    raw_text: str,
    topic: str,
    level: str,
    category: str,
    podcast_mode: str = "dialogue",
) -> Dict[str, Any]:
    """Parse either JSON script or plain 'Anna: English | Tiếng Việt' lines pasted by user."""
    raw_text = raw_text.strip()
    if raw_text.startswith("{"):
        return json.loads(raw_text)

    lines_list = []
    idx_counter = 0
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        fallback_sp = ("Anna" if idx_counter % 2 == 0 else "Tom") if podcast_mode == "dialogue" else ""
        if "|" in line:
            en_part, vi_part = line.split("|", 1)
            sp, clean_en = extract_speaker_prefix(en_part, fallback_sp)
            _, clean_vi = extract_speaker_prefix(vi_part, "")
            lines_list.append({"speaker": sp, "en": clean_en, "vi": clean_vi})
        else:
            sp, clean_en = extract_speaker_prefix(line, fallback_sp)
            lines_list.append({"speaker": sp, "en": clean_en, "vi": ""})
        idx_counter += 1

    scenes = []
    chunk_size = max(1, math.ceil(len(lines_list) / 4))
    for i in range(0, len(lines_list), chunk_size):
        chunk = lines_list[i : i + chunk_size]
        scenes.append(
            {
                "image_prompt": f"two anime students Anna and Tom talking together in cozy setting about {topic}, warm lighting",
                "lines": chunk,
            }
        )

    return {
        "title_vi": topic.upper(),
        "level": level.upper(),
        "category": category.upper(),
        "podcast_mode": podcast_mode,
        "thumb_prompt": f"anime girl and boy sitting together talking about {topic}, expressive faces, warm cozy lighting",
        "scenes": scenes,
    }


async def generate_script(
    topic: str,
    level: str = "B1",
    category: str = "TIẾNG ANH HỌC THUẬT",
    target_sentences: int = 24,
    podcast_mode: str = "dialogue",
    gemini_api_key: str = "",
    custom_script: str = "",
    sensitivity_mode: str = "normal",
) -> Dict[str, Any]:
    """
    Generate bilingual podcast script in the exact style of @REACH-l5p.
    Supports sensitivity_mode: 'polite' | 'normal' | 'sensitive_ok'.
    """
    if custom_script and custom_script.strip():
        return parse_custom_script_text(custom_script, topic, level, category, podcast_mode)

    num_scenes = max(3, min(10, target_sentences // 4))
    sents_per_scene = max(3, target_sentences // num_scenes)

    level_guide = {
        "A1": "Use CEFR A1 basic vocabulary, ULTRA-SHORT direct 1st-person roleplay lines (2-6 words per turn).",
        "A2": "Use CEFR A2 everyday vocabulary, SHORT direct 1st-person roleplay lines (3-8 words per turn).",
        "B1": "Use CEFR B1 intermediate vocabulary, SHORT & DRAMATIC direct 1st-person roleplay lines (3-9 words per turn).",
        "B2": "Use CEFR B2 upper-intermediate vocabulary, PUNCHY & EXPRESSIVE direct 1st-person roleplay lines (4-10 words per turn).",
        "C1": "Use CEFR C1 advanced vocabulary, SHARP & CONCISE direct 1st-person roleplay lines (4-11 words per turn).",
        "C2": "Use CEFR C2 native phrasing, CONCISE & NUANCED direct 1st-person roleplay lines (4-11 words per turn).",
    }.get(level.upper(), "Use CEFR B1 natural conversational English (3-9 words per turn).")

    tone_guide = {
        "polite": (
            "CONTENT SENSITIVITY MODE = LỊCH SỰ (Polite & Classroom-Safe): "
            "Use polite, respectful, wholesome language and gentle everyday situations. "
            "Strictly avoid any sensitive, romantic drama, or controversial themes."
        ),
        "normal": (
            "CONTENT SENSITIVITY MODE = BÌNH THƯỜNG (Natural Everyday): "
            "Use natural, relatable everyday spoken conversation and genuine youth/adult emotions."
        ),
        "sensitive_ok": (
            "CONTENT SENSITIVITY MODE = CHO PHÉP NHẠY CẢM (Edgy / Deep Romance / Drama Allowed): "
            "Feel free to explore bold, emotionally intense, romantic, secret crush/confession, "
            "heartbreak, betrayal, toxic relationship, or edgy real-life drama topics with candid, raw feelings."
        ),
    }.get(sensitivity_mode, "Use natural, relatable everyday spoken conversation.")

    system_prompt = (
        "You are the scriptwriter for the 'REACH Podcast' YouTube/TikTok channel. "
        "Your signature style is DIRECT 1ST-PERSON ROLEPLAY DIALOGUE ('I' and 'You') with SHORT, PUNCHY, EMOTIONAL turns (only 2 to 9 words per turn) "
        "so TTS voices read slowly, clearly, and with deep dramatic expressiveness. "
        "Return ONLY valid JSON without markdown fences."
    )

    if podcast_mode == "dialogue":
        mode_instruction = f"""Write an intense, emotional 2-speaker DIRECT 1ST-PERSON ROLEPLAY dialogue between **Anna** (female) and **Tom** (male) directly inside the situation: "{topic}" (CEFR Level: {level}).
{tone_guide}
CRITICAL RULES (MATCHING REACH REFERENCE VIDEOS):
1. **Direct 1st-person roleplay (`I` and `You`):** Anna and Tom are NOT podcast hosts talking about someone else. They ARE the two main characters directly confronting, confessing, or talking to each other in the moment!
2. **Short, punchy turns (2 to 9 words per line ONLY):** Keep every single line between 2 and 9 words so the voice reads slowly with maximum emotional weight.
   Examples of the exact target rhythm:
   - "Because I like you." / "What?" / "I like you, I like you a lot." / "Wait, this is sudden." / "But we are best friends."
   - "Why didn't you tell me you were married?" / "You never asked me." / "What do you mean?" / "I asked if you have a boyfriend!" / "Yes, and I said no." / "But you have a husband!" / "Are you serious right now?"
3. **Natural Vietnamese translation (`vi`):** Use real-life pronouns matching the relationship (`Anh - Em` or `Tao - Mày` / `Tớ - Cậu`).
4. Alternate turns naturally between "Anna" and "Tom". Do NOT put "Anna:" inside the "en" string."""
        line_schema = '{"speaker": "Anna", "en": "Short dramatic English line (2-9 words).", "vi": "Câu dịch tiếng Việt ngắn gọn, đúng xưng hô Anh-Em hoặc Tao-Mày."}'
    else:
        mode_instruction = f"""Write an engaging 1-speaker first-person English storytelling podcast episode about the topic: "{topic}" (CEFR Level: {level}).
{tone_guide}
IMPORTANT FOR NATURAL VOICE & SHADOWING:
- Include natural spoken interjections, gentle sighs, chuckles, and conversational pauses ("Hmm...", "Honestly...", "Oh gosh, you know what?", "Haha, looking back now...", "Well, the truth is...").
- Start with: "Hello everyone, and welcome back to the Reach podcast." """
        line_schema = '{"speaker": "", "en": "English sentence with natural spoken interjections.", "vi": "Câu dịch tiếng Việt tự nhiên, giàu cảm xúc."}'

    user_prompt = f"""{mode_instruction}
{level_guide}

Structure the output into {num_scenes} scenes, with {sents_per_scene} lines per scene (total ~{num_scenes * sents_per_scene} lines).
Each line MUST have natural English ("en") and natural, emotional Vietnamese translation ("vi").

Return ONLY a JSON object matching this exact schema:
{{
  "title_vi": "{topic.upper()}",
  "level": "{level.upper()}",
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
    active_key = gemini_api_key.strip() or DEFAULT_GEMINI_KEY
    try:
        async with httpx.AsyncClient(timeout=55.0) as client:
            if active_key:
                payload = {
                    "contents": [{"parts": [{"text": system_prompt + "\n\n" + user_prompt}]}],
                    "generationConfig": {"responseMimeType": "application/json", "temperature": 0.75},
                }
                for model_id in (
                    "gemini-3-flash-preview",
                    "gemini-3.5-flash-lite",
                    "gemini-flash-lite-latest",
                    "gemini-3.8-flash",
                    "gemini-3.6-flash",
                    "gemini-3.7-flash",
                    "gemini-flash-latest",
                ):
                    try:
                        gem_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent?key={active_key}"
                        resp = await client.post(gem_url, json=payload, timeout=18.0)
                        if resp.status_code == 200:
                            raw_json_str = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                            if raw_json_str and "{" in raw_json_str:
                                break
                    except Exception:
                        continue
            if not raw_json_str:
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
                )
                resp.raise_for_status()
                data = resp.json()
                raw_json_str = data["choices"][0]["message"]["content"]

        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_json_str.strip(), flags=re.MULTILINE)
        start_idx = cleaned.find("{")
        end_idx = cleaned.rfind("}")
        if start_idx != -1 and end_idx != -1:
            cleaned = cleaned[start_idx : end_idx + 1]
        parsed = json.loads(cleaned)
        parsed["podcast_mode"] = podcast_mode
        return parsed
    except Exception:
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
    "en-US-EmmaMultilingualNeural": "en-US-AvaMultilingualNeural",
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
    t = re.sub(r"\s*,\s*,+", ",", t)
    # Remove comma after single-word starters ("No, I'm" -> "No I'm", "Okay, I" -> "Okay I") to prevent start-of-sentence stutters
    t = re.sub(r"^(No|Yes|Okay|Well|Wait|Oh|Look),\s+", r"\1 ", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip()
    return t or "Hello."


def compute_emotional_prosody(text: str, base_rate: str = "-12%") -> tuple[str, str, str]:
    """
    Analyze sentence emotion and return (pitch, rate, volume) for expressive acting
    at a slow, clear, deliberate REACH-style pace (default -12%):
      - Shock / Hook / Excitement -> higher pitch (+6Hz), clear projection (+8%)
      - Heartbreak / Empathy / Sadness -> lower intimate pitch (-4Hz), slower pace, soft breathy tone
      - Relief / Warmth -> gentle warm pitch (-2Hz), relaxed pace
      - Question / Curiosity -> natural upward inflection (+5Hz)
    """
    m = re.match(r"^([+-]?\d+)%$", (base_rate or "-12%").strip())
    base_r = int(m.group(1)) if m else -12
    low = text.lower()

    sad_cues = (
        "hurts", "betrayal", "afraid", "broke", "pain", "unforgivable", "sad",
        "quiet", "ignore", "hiding", "cry", "alone", "sorry", "deeply", "secret",
        "exhausted", "regret", "ruining", "ashamed", "heartbreak", "scared", "waited",
    )
    excited_cues = (
        "oh gosh", "oh my gosh", "wait, seriously", "seriously?!", "no way",
        "crazy", "shocking", "unbelievable", "twist", "exact same way", "guess what",
        "what do you mean", "are you serious",
    )
    warm_cues = ("relieved", "peace", "grateful", "realized", "quietest", "honestly", "like you")

    if any(k in low for k in sad_cues):
        r_val = max(-18, base_r - 2)
        return ("-4Hz", f"{r_val:+d}%", "+2%")
    if any(k in low for k in excited_cues) or text.endswith("?!") or text.endswith("!"):
        r_val = min(-4, base_r + 2)
        return ("+6Hz", f"{r_val:+d}%", "+8%")
    if text.endswith("?"):
        r_val = min(-6, base_r + 1)
        return ("+5Hz", f"{r_val:+d}%", "+6%")
    if any(k in low for k in warm_cues):
        r_val = max(-16, base_r - 1)
        return ("-2Hz", f"{r_val:+d}%", "+4%")
    return ("+1Hz", f"{base_r:+d}%", "+4%")


GEMINI_LIVE_AUDIO_MODELS = (
    "models/gemini-2.5-flash-native-audio-latest",
    "models/gemini-2.5-flash-native-audio-preview-12-2025",
    "models/gemini-3.1-flash-live-preview",
)


def infer_acting_direction(en_text: str, vi_text: str = "", is_male: bool = False) -> str:
    """
    Return smooth, fluent conversational tone descriptors for Gemini Live Native Audio.
    Avoids words like 'nervous', 'hurt', or 'slow' that cause the model to insert mid-sentence stutters.
    """
    if en_text.strip().endswith("?!") or en_text.strip().endswith("!"):
        return "smooth, expressive, energetic conversational"
    if en_text.strip().endswith("?"):
        return "smooth, natural, inquisitive conversational"
    return "smooth, warm, fluent conversational"


def trim_pcm_silence(pcm_bytes: bytes, sample_rate: int = 24000, threshold: int = 160) -> bytes:
    """Trim dead leading/trailing silence from 16-bit mono PCM while preserving natural breath."""
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
    keep_lead = int(0.06 * sample_rate)
    keep_trail = int(0.10 * sample_rate)
    start = max(0, first_idx - keep_lead)
    end = min(n, last_idx + keep_trail)
    return samples[start:end].tobytes()


def smooth_internal_stutters(
    pcm_bytes: bytes, sample_rate: int = 24000, max_gap_ms: int = 130
) -> tuple[bytes, int]:
    """
    Detect and remove unnatural mid-sentence hesitation/stutter pockets ('vấp đĩa' >= 180ms) inside a single
    spoken line while keeping natural consonant closures (< 180ms) 100% untouched.
    Returns (cleaned_pcm_bytes, severe_stutter_count).
    """
    import math
    from array import array

    trimmed = trim_pcm_silence(pcm_bytes, sample_rate)
    if len(trimmed) < 8000:
        return trimmed, 0
    if len(trimmed) % 2 != 0:
        trimmed = trimmed[:-1]
    samples = array("h")
    samples.frombytes(trimmed)
    win = int(0.01 * sample_rate)  # 10ms window = 240 samples at 24kHz
    n_wins = len(samples) // win
    if n_wins < 18:
        return trimmed, 0

    is_silent = []
    for w in range(n_wins):
        chunk = samples[w * win : (w + 1) * win]
        rms = math.sqrt(sum(x * x for x in chunk) / len(chunk))
        is_silent.append(rms < 85.0)

    out_samples = array("h")
    severe_count = 0
    w = 0
    keep_wins = max(10, max_gap_ms // 10)
    while w < n_wins:
        if not is_silent[w] or w < 7 or w >= n_wins - 11:
            out_samples.extend(samples[w * win : (w + 1) * win])
            w += 1
            continue
        run_end = w
        while run_end < n_wins - 11 and is_silent[run_end]:
            run_end += 1
        run_len = run_end - w
        if run_len >= 26:
            severe_count += 1
        if run_len >= 18:
            keep_head = keep_wins // 2
            keep_tail = keep_wins - keep_head
            out_samples.extend(samples[w * win : (w + keep_head) * win])
            out_samples.extend(samples[(run_end - keep_tail) * win : run_end * win])
        else:
            out_samples.extend(samples[w * win : run_end * win])
        w = run_end
    out_samples.extend(samples[n_wins * win :])
    return out_samples.tobytes(), severe_count


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
) -> Dict[int, bytes]:
    """
    Open a fresh Gemini Live BidiGenerateContent WebSocket session with temperature=0.30
    and fluent connected-speech instructions so the voice never stutters or pauses mid-sentence.
    """
    import aiohttp

    url = (
        "wss://generativelanguage.googleapis.com/ws/"
        f"google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={active_key}"
    )
    sys_txt = (
        "You are a professional American English podcast host recording a smooth, natural listening lesson. "
        "Speak in a fluent, continuous, connected stream of words with natural warmth and clear pronunciation. "
        "CRITICAL RULES: "
        "1. NEVER pause, hesitate, or stutter between words inside a sentence. "
        "2. NEVER insert dramatic sighs, gasps, or mid-sentence breaks. "
        "3. Speak ONLY the exact words inside <read>...</read> verbatim from start to finish."
    )
    results: Dict[int, bytes] = {}
    timeout = aiohttp.ClientTimeout(total=45.0)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.ws_connect(url, heartbeat=20.0) as ws:
            setup_msg = {
                "setup": {
                    "model": model_name,
                    "generationConfig": {
                        "responseModalities": ["AUDIO"],
                        "temperature": 0.30,
                        "speechConfig": {
                            "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": gem_voice}}
                        },
                    },
                    "systemInstruction": {"parts": [{"text": sys_txt}]},
                    "outputAudioTranscription": {},
                }
            }
            await ws.send_json(setup_msg)
            await asyncio.wait_for(ws.receive(), timeout=10.0)

            for idx, clean_text, emo_dir in chunk_entries:
                for attempt in range(2):
                    if attempt == 0:
                        prompt = (
                            f"Read smoothly and fluently in one continuous breath without any mid-sentence pauses "
                            f"({emo_dir}): <read>{clean_text}</read>"
                        )
                    else:
                        prompt = (
                            f"Read ONLY these exact words aloud smoothly in one continuous, uninterrupted flow: "
                            f"<read>{clean_text}</read>"
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
                        m = await asyncio.wait_for(ws.receive(), timeout=18.0)
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
                        for bw in ("breath", "gasp", "sigh", "trembling", "tone", "direction", "tearful", "conversational", "smoothly")
                    )
                    too_long = len(spoken_words) > len(target_words) + 3
                    missing_end = (
                        len(target_words) >= 3
                        and bool(spoken_words)
                        and target_words[-1] not in spoken_words[-2:]
                    )
                    cleaned_pcm, severe_stutters = smooth_internal_stutters(bytes(pcm), SAMPLE_RATE)
                    if len(cleaned_pcm) > 2400 and not bad_direction_leaked and not too_long and not missing_end and severe_stutters == 0:
                        results[idx] = cleaned_pcm
                        break
                    elif attempt == 1 and len(cleaned_pcm) > 2400:
                        results[idx] = cleaned_pcm
    return results


async def synthesize_batch_gemini_live_ws(
    flat_items: List[Dict[str, Any]],
    work_dir: Path,
    pause_sec: float = 0.45,
    gemini_api_key: str = "",
) -> None:
    """
    Synthesize dialogue turns using Gemini Live Native Audio WebSocket (BidiGenerateContent)
    with 1 fresh WebSocket per turn (up to 4 in parallel) so every line has consistent, grounded emotional acting.
    """
    active_key = (gemini_api_key or "").strip() or DEFAULT_GEMINI_KEY
    if not active_key:
        return

    tasks_list: List[tuple[str, List[tuple[int, str, str]]]] = []
    for idx, it in enumerate(flat_items):
        v_raw = str(it.get("voice") or "")
        if v_raw.startswith("edge:"):
            continue
        gem_voice = resolve_gemini_voice_name(v_raw, bool(it.get("is_male")))
        clean_en = sanitize_english_for_tts(it.get("en", ""))
        emo_dir = it.get("emotion") or infer_acting_direction(
            clean_en, it.get("vi", ""), bool(it.get("is_male"))
        )
        tasks_list.append((gem_voice, [(idx, clean_en, emo_dir)]))

    sem = asyncio.Semaphore(4)
    pcm_map: Dict[int, bytes] = {}

    async def run_chunk(gem_v: str, ch: List[tuple[int, str, str]]) -> None:
        async with sem:
            for m_name in GEMINI_LIVE_AUDIO_MODELS:
                try:
                    got = await synthesize_chunk_gemini_live_ws(gem_v, ch, active_key, m_name)
                    if got:
                        pcm_map.update(got)
                        break
                except Exception:
                    continue

    await asyncio.gather(*(run_chunk(gv, c) for gv, c in tasks_list))

    pad_frames = b"\x00\x00" * int(pause_sec * SAMPLE_RATE)
    for idx, pcm_frames in pcm_map.items():
        wav_p = work_dir / f"line_{idx:03d}.wav"
        all_pcm = pcm_frames + pad_frames
        with wave.open(str(wav_p), "wb") as out_wf:
            out_wf.setnchannels(1)
            out_wf.setsampwidth(2)
            out_wf.setframerate(SAMPLE_RATE)
            out_wf.writeframes(all_pcm)
        flat_items[idx]["wav"] = wav_p
        flat_items[idx]["duration"] = (len(all_pcm) // 2) / float(SAMPLE_RATE)


async def synthesize_line_wav(
    text: str,
    voice: str,
    rate: str,
    mp3_path: Path,
    wav_path: Path,
    pause_sec: float = 0.45,
    is_male: bool = False,
    line_idx: int = 0,
    gemini_api_key: str = "",
    vi_text: str = "",
) -> float:
    """
    Synthesize 1 English sentence with maximum human expressiveness:
      1. First tries Gemini Live Native Audio WebSocket (BidiGenerateContent) with emotional acting direction.
      2. Falls back to Microsoft Gen-2 Dragon HD / LLM MultilingualNeural voices with Dynamic Emotional Prosody.
    Returns exact duration in seconds (sample-accurate, zero drift).
    """
    clean_tts_text = sanitize_english_for_tts(text)
    active_key = gemini_api_key.strip() or DEFAULT_GEMINI_KEY

    if active_key and not voice.startswith("edge:"):
        gem_voice = resolve_gemini_voice_name(voice, is_male)
        emo_dir = infer_acting_direction(clean_tts_text, vi_text, is_male)
        for m_name in GEMINI_LIVE_AUDIO_MODELS[:2]:
            try:
                got = await synthesize_chunk_gemini_live_ws(
                    gem_voice, [(line_idx, clean_tts_text, emo_dir)], active_key, m_name
                )
                if line_idx in got:
                    pcm_frames = got[line_idx]
                    pad_frames = b"\x00\x00" * int(pause_sec * SAMPLE_RATE)
                    all_pcm = pcm_frames + pad_frames
                    with wave.open(str(wav_path), "wb") as out_wf:
                        out_wf.setnchannels(1)
                        out_wf.setsampwidth(2)
                        out_wf.setframerate(SAMPLE_RATE)
                        out_wf.writeframes(all_pcm)
                    return (len(all_pcm) // 2) / float(SAMPLE_RATE)
            except Exception:
                continue

    raw_v = voice.split(":", 1)[-1] if voice.startswith("edge:") else voice
    mapped_voice = EXPRESSIVE_VOICE_MAP.get(raw_v, raw_v)
    fallback_edge_voice = (
        mapped_voice
        if mapped_voice.startswith("en-")
        else ("en-US-BrianMultilingualNeural" if is_male else "en-US-AvaMultilingualNeural")
    )
    alt_voice = "en-US-AndrewMultilingualNeural" if is_male else "en-US-AriaNeural"
    emo_pitch, emo_rate, emo_vol = compute_emotional_prosody(clean_tts_text, rate)
    for attempt in range(3):
        try:
            v_try = fallback_edge_voice if attempt < 2 else alt_voice
            comm = edge_tts.Communicate(
                clean_tts_text,
                voice=v_try,
                rate=emo_rate if attempt == 0 else rate,
                pitch=emo_pitch if attempt == 0 else "+0Hz",
                volume=emo_vol if attempt == 0 else "+0%",
            )
            await comm.save(str(mp3_path))
            if mp3_path.exists() and mp3_path.stat().st_size > 200:
                break
        except Exception:
            await asyncio.sleep(0.8 * (attempt + 1))

    cmd = [
        FFMPEG_EXE,
        "-y",
        "-i",
        str(mp3_path),
        "-af",
        f"apad=pad_dur={pause_sec}",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        str(wav_path),
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    with wave.open(str(wav_path), "rb") as wf:
        frames = wf.getnframes()
        return frames / float(SAMPLE_RATE)




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
    ms = int(round((seconds % 1) * 1000))
    total = int(seconds)
    m, s = divmod(total, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


async def build_video_job(
    job_dir: Path,
    topic: str,
    level: str = "B1",
    category: str = "TIẾNG ANH HỌC THUẬT",
    podcast_mode: str = "dialogue",
    voice: str = "gemini:Aoede",
    voice_male: str = "gemini:Puck",
    rate: str = "-5%",
    target_sentences: int = 20,
    channel_tag: str = "REACH PODCAST",
    gemini_api_key: str = "",
    custom_script: str = "",
    subtitle_style: str = "teleprompter",
    sensitivity_mode: str = "normal",
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

    mode_label = "Hội thoại 2 người (Anna & Tom)" if podcast_mode == "dialogue" else "Độc thoại 1 người"
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
    )
    (job_dir / "script.json").write_text(
        json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    scenes = script.get("scenes", [])
    if not scenes:
        raise ValueError("Script has no scenes.")

    report("2/5 Đang vẽ nhân vật Webtoon 2D & tạo Thumbnail bìa 4K...", 22)
    base_seed = int(time.time()) % 100000
    thumb_bg_path = work_dir / "thumb_bg.jpg"
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
                or (("Anna" if turn_idx % 2 == 0 else "Tom") if podcast_mode == "dialogue" else "")
            )
            sp, clean_en = extract_speaker_prefix(raw_en, default_sp)
            _, clean_vi = extract_speaker_prefix(line.get("vi", "").strip(), "")
            is_male_turn = bool(
                podcast_mode == "dialogue" and sp.lower() in ("tom", "male", "nam", "b")
            )
            chosen_voice = voice_male if is_male_turn else voice
            flat_items.append(
                {
                    "scene_idx": s_idx,
                    "speaker": sp if podcast_mode == "dialogue" else "",
                    "is_male": is_male_turn,
                    "voice": chosen_voice,
                    "en": clean_en,
                    "vi": clean_vi,
                }
            )
            turn_idx += 1

    report(f"3/5 Đang thu âm giọng diễn xuất cảm xúc Gemini Live ({len(flat_items)} lượt thoại)...", 45)
    if not (str(voice).startswith("edge:") and str(voice_male).startswith("edge:")):
        try:
            await synthesize_batch_gemini_live_ws(
                flat_items,
                work_dir,
                pause_sec=0.45,
                gemini_api_key=gemini_api_key,
            )
        except Exception:
            pass

    sem = asyncio.Semaphore(2)

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
                is_male=item["is_male"],
                line_idx=idx,
                gemini_api_key=gemini_api_key,
            )
            item["wav"] = wav_p
            item["duration"] = dur
            return dur

    await asyncio.gather(*(synth_one(i, it) for i, it in enumerate(flat_items)))

    voice_body_wav = work_dir / "voice_body.wav"
    body_duration = concat_wav_files([it["wav"] for it in flat_items], voice_body_wav)

    # 4. Render transition cards & slide cards on pure black (#000000) for both 16:9 and 9:16
    report("4/5 Đang dựng phụ đề nền đen (Ngang 16:9 & Dọc 9:16 + Bản cắt TikTok)...", 68)
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
        render_slide_card(None, "part2", it["en"], it["vi"], p2_v_png, speaker=it["speaker"], aspect="9:16")
        part2_slides.append((p2_png, it["duration"]))
        part2_v_slides.append((p2_v_png, it["duration"]))

    # 5. Encode Horizontal (16:9) + Vertical (9:16) 3-part videos + TikTok cuts
    report("5/5 Đang xuất MP4 Ngang (16:9), Dọc (9:16) & Bản cắt TikTok...", 78)
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
