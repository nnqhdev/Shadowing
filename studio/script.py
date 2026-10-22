import asyncio
import json
import logging
import math
import re
from typing import Any, Dict, List

import httpx
from .common import DEFAULT_GEMINI_KEY, _load_default_gemini_key, safe_err
from .emotion import detect_line_emotion

logger = logging.getLogger(__name__)

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
