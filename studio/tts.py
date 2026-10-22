import asyncio
import base64
import json
import logging
import math
import re
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from .common import DEFAULT_GEMINI_KEY, SAMPLE_RATE, _load_default_gemini_key, safe_err
from .emotion import MAX_INTERNAL_PAUSE_MS, SILENCE_RMS_THRESHOLD, detect_line_emotion, extract_emphasis_keywords, get_blended_or_single_spec, get_emotion_pause_sec, parse_emotion_and_intensity

logger = logging.getLogger(__name__)

GEMINI_TTS_MODELS = (
    "gemini-2.5-flash-preview-tts",
    "gemini-3.1-flash-tts-preview",
    "gemini-3.8-flash-tts",
    "gemini-3.8-flash-lite-tts",
)

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
