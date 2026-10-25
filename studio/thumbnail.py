import asyncio
import base64
import io
import logging
import math
import re
import threading
import time
import weakref
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from PIL import Image, ImageDraw
from .common import ASSETS_DIR, DEFAULT_GEMINI_KEY, safe_err
from .render import get_font

logger = logging.getLogger(__name__)

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
    assets_dir = ASSETS_DIR
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
