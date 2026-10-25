import logging
import random
import re
from typing import Any, Dict, List


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
