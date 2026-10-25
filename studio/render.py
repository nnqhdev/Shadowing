import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps
from .common import FFMPEG_EXE, SAMPLE_RATE

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
