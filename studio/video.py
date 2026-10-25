import asyncio
import json
import logging
import shutil
import subprocess
import time
import wave
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PIL import Image, ImageDraw
from .common import ASSETS_DIR, FFMPEG_EXE, SAMPLE_RATE, safe_err
from .emotion import detect_line_emotion, smooth_emotional_arc
from .render import get_font, render_part3_audio_ring_mp4, render_part_transition_card, render_scrolling_section_mp4, render_slide_card, wrap_text_lines
from .script import _is_male_voice_id, extract_speaker_and_emotion_prefix, generate_script
from .thumbnail import fetch_anime_image, make_fallback_anime_bg, render_thumbnail
from .tts import concat_wav_files, synthesize_batch_gemini_live_ws, synthesize_line_wav

logger = logging.getLogger(__name__)

DEFAULT_INTRO_WAV = ASSETS_DIR / "intro_signature.wav"
DEFAULT_TRANSITION_WAV = ASSETS_DIR / "part_transition.wav"


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
