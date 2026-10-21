import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from generator import DEFAULT_GEMINI_KEY, build_video_job
from screenwriter import discover_hot_topics_mode_b, generate_5step_screenplay

BASE_DIR = Path(__file__).parent.resolve()
OUTPUT_DIR = BASE_DIR / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)

app = FastAPI(title="REACH Podcast Factory")
app.mount("/outputs", StaticFiles(directory=str(OUTPUT_DIR)), name="outputs")

JOBS: Dict[str, Dict[str, Any]] = {}
QUEUE_LOCK = asyncio.Lock()


class BatchItem(BaseModel):
    topic: str
    level: str = "B1"
    category: str = "TIẾNG ANH THỰC TẾ"
    podcast_mode: str = "dialogue"
    custom_script: str = ""
    sensitivity_mode: str = "normal"


class BatchRequest(BaseModel):
    items: List[BatchItem]
    podcast_mode: str = "dialogue"
    subtitle_style: str = "teleprompter"
    sensitivity_mode: str = "normal"
    voice: str = "gemini:Aoede"
    voice_male: str = "gemini:Puck"
    rate: str = "-12%"
    target_sentences: int = 16
    channel_tag: str = "UNI PODCAST"
    gemini_api_key: str = DEFAULT_GEMINI_KEY


class ModeBDiscoverRequest(BaseModel):
    level: str = "B1"
    topic: str = ""
    description: str = ""
    cta: str = "Đăng ký kênh, ôn từ vựng & trả lời câu hỏi dưới bình luận"
    sensitivity_mode: str = "normal"
    gemini_api_key: str = DEFAULT_GEMINI_KEY


class ModeABCreateRequest(BaseModel):
    mode: str = "A"
    topic: str
    description: str = ""
    level: str = "B1"
    target_sentences: int = 16
    cta: str = "Đăng ký kênh, ôn từ vựng & trả lời câu hỏi dưới bình luận"
    sensitivity_mode: str = "normal"
    voice: str = "gemini:Aoede"
    voice_male: str = "gemini:Puck"
    rate: str = "-12%"
    channel_tag: str = "UNI PODCAST"
    gemini_api_key: str = DEFAULT_GEMINI_KEY



def slugify(text: str) -> str:
    s = text.lower().replace("đ", "d")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9\s-]", "", s).strip()
    return re.sub(r"[-\s]+", "_", s)[:32] or "video"


async def worker_run_jobs(job_ids: List[str], req: BatchRequest) -> None:
    async with QUEUE_LOCK:
        for jid in job_ids:
            job = JOBS.get(jid)
            if not job:
                continue
            job["status"] = "running"
            job["progress"] = 5
            job["message"] = "Đang khởi tạo tiến trình..."
            job_dir = OUTPUT_DIR / jid

            def on_prog(msg: str, pct: int, current_jid: str = jid) -> None:
                if current_jid in JOBS:
                    JOBS[current_jid]["message"] = msg
                    JOBS[current_jid]["progress"] = pct

            try:
                res = await build_video_job(
                    job_dir=job_dir,
                    topic=job["topic"],
                    level=job["level"],
                    category=job["category"],
                    podcast_mode=job.get("podcast_mode", req.podcast_mode),
                    voice=req.voice,
                    voice_male=req.voice_male,
                    rate=req.rate,
                    target_sentences=req.target_sentences,
                    channel_tag=req.channel_tag,
                    gemini_api_key=req.gemini_api_key or DEFAULT_GEMINI_KEY,
                    custom_script=job.get("custom_script", ""),
                    subtitle_style=req.subtitle_style,
                    sensitivity_mode=job.get("sensitivity_mode", req.sensitivity_mode),
                    on_progress=on_prog,
                )
                job["status"] = "done"
                job["progress"] = 100
                job["message"] = "Hoàn tất xuất video Ngang (16:9) & Dọc (9:16)!"
                job["result"] = res
                job["description_text"] = (job_dir / "youtube_description.txt").read_text(
                    encoding="utf-8"
                )
            except Exception as e:
                job["status"] = "error"
                job["message"] = f"Lỗi: {str(e)}"


@app.post("/api/batch")
async def create_batch(req: BatchRequest) -> Dict[str, Any]:
    created_ids: List[str] = []
    for item in req.items:
        if not item.topic.strip():
            continue
        ts = time.strftime("%H%M%S")
        mode_val = item.podcast_mode or req.podcast_mode
        sens_val = item.sensitivity_mode or req.sensitivity_mode or "normal"
        jid = f"{ts}_{item.level.upper()}_{slugify(item.topic)}_{uuid.uuid4().hex[:4]}"
        JOBS[jid] = {
            "id": jid,
            "topic": item.topic.strip(),
            "level": item.level.strip().upper(),
            "category": item.category.strip().upper(),
            "podcast_mode": mode_val,
            "sensitivity_mode": sens_val,
            "custom_script": item.custom_script,
            "status": "queued",
            "progress": 0,
            "message": "Đang chờ trong hàng đợi...",
            "created_at": time.strftime("%H:%M:%S"),
            "sort_ts": time.time(),
            "result": None,
            "description_text": "",
        }
        created_ids.append(jid)

    if created_ids:
        asyncio.create_task(worker_run_jobs(created_ids, req))
    return {"created": created_ids}


@app.post("/api/mode-b/discover")
async def api_mode_b_discover(req: ModeBDiscoverRequest) -> Dict[str, Any]:
    return await discover_hot_topics_mode_b(
        level=req.level,
        description_hint=req.description,
        cta=req.cta,
        gemini_api_key=req.gemini_api_key or DEFAULT_GEMINI_KEY,
        sensitivity_mode=req.sensitivity_mode,
        topic_hint=req.topic,
    )


@app.post("/api/mode-ab/create")
async def api_mode_ab_create(req: ModeABCreateRequest) -> Dict[str, Any]:
    raw_title = (req.topic or "CHỦ ĐỀ HOT").strip().upper()
    ts = time.strftime("%H%M%S")
    lvl = req.level.strip().upper() or "B1"
    jid = f"{ts}_{lvl}_{slugify(raw_title)}_{uuid.uuid4().hex[:4]}"

    job_dir = OUTPUT_DIR / jid
    job_dir.mkdir(parents=True, exist_ok=True)

    JOBS[jid] = {
        "id": jid,
        "topic": raw_title,
        "level": lvl,
        "category": f"CHẾ ĐỘ {req.mode.upper()} • BIÊN KỊCH 5 BƯỚC",
        "podcast_mode": "dialogue",
        "sensitivity_mode": req.sensitivity_mode,
        "custom_script": "",
        "dossier": {},
        "status": "running",
        "progress": 4,
        "message": f"Đang lập kịch bản 5 bước từ chủ đề '{raw_title}'...",
        "created_at": time.strftime("%H:%M:%S"),
        "sort_ts": time.time(),
        "result": None,
        "description_text": "",
    }

    async def _run_mode_ab() -> None:
        custom_script_str = ""
        short_title = raw_title
        try:
            dossier = await generate_5step_screenplay(
                topic=req.topic,
                description=req.description,
                level=req.level,
                target_turns=req.target_sentences,
                cta=req.cta,
                gemini_api_key=req.gemini_api_key or DEFAULT_GEMINI_KEY,
                sensitivity_mode=req.sensitivity_mode,
            )
            custom_script_str = dossier.get("step4_formatted", {}).get("custom_script_for_video", "")
            short_title = (dossier.get("title_vi") or raw_title).strip().upper()
            (job_dir / "kich_ban_5_buoc.json").write_text(
                json.dumps(dossier, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if jid in JOBS:
                JOBS[jid]["topic"] = short_title
                JOBS[jid]["custom_script"] = custom_script_str
                JOBS[jid]["dossier"] = dossier
                JOBS[jid]["progress"] = 8
                JOBS[jid]["message"] = "Đã lập kịch bản 5 bước, đang thu âm & dựng Video 3 phần..."
        except Exception as exc:
            if jid in JOBS:
                JOBS[jid]["message"] = f"Đang khởi tạo kịch bản tiêu chuẩn ({exc})..."

        batch_req = BatchRequest(
            items=[
                BatchItem(
                    topic=short_title,
                    level=lvl,
                    category=f"CHẾ ĐỘ {req.mode.upper()} • BIÊN KỊCH 5 BƯỚC",
                    podcast_mode="dialogue",
                    custom_script=custom_script_str,
                    sensitivity_mode=req.sensitivity_mode,
                )
            ],
            podcast_mode="dialogue",
            subtitle_style="teleprompter",
            sensitivity_mode=req.sensitivity_mode,
            voice=req.voice,
            voice_male=req.voice_male,
            rate=req.rate,
            target_sentences=req.target_sentences,
            channel_tag=req.channel_tag,
            gemini_api_key=req.gemini_api_key or DEFAULT_GEMINI_KEY,
        )
        await worker_run_jobs([jid], batch_req)

    asyncio.create_task(_run_mode_ab())
    return {"created_id": jid}



@app.get("/api/jobs")
async def list_jobs() -> List[Dict[str, Any]]:
    for folder in OUTPUT_DIR.iterdir():
        if not folder.is_dir():
            continue
        mp4 = folder / "video_3_phan.mp4"
        if folder.name in JOBS:
            if JOBS[folder.name].get("status") == "done" and mp4.exists():
                JOBS[folder.name]["sort_ts"] = mp4.stat().st_mtime
            continue
        script_f = folder / "script.json"
        desc_f = folder / "youtube_description.txt"
        dossier_f = folder / "kich_ban_5_buoc.json"
        if mp4.exists() and script_f.exists():
                sc = json.loads(script_f.read_text(encoding="utf-8"))
                desc_txt = desc_f.read_text(encoding="utf-8") if desc_f.exists() else ""
                dossier_data = (
                    json.loads(dossier_f.read_text(encoding="utf-8")) if dossier_f.exists() else None
                )
                found_times = re.findall(r"^(\d{2}:\d{2}(?::\d{2})?)\s*-", desc_txt, flags=re.MULTILINE)
                p1_t = found_times[0] if len(found_times) > 0 else "00:00"
                p2_t = found_times[1] if len(found_times) > 1 else "00:44"
                p3_t = found_times[2] if len(found_times) > 2 else "01:23"
                p_mode = sc.get("podcast_mode", "monologue")
                JOBS[folder.name] = {
                    "id": folder.name,
                    "topic": sc.get("title_vi", folder.name),
                    "level": sc.get("level", "B1"),
                    "category": sc.get("category", "TIẾNG ANH HỌC THUẬT"),
                    "podcast_mode": p_mode,
                    "sensitivity_mode": sc.get("sensitivity_mode", "normal"),
                    "dossier": dossier_data,
                    "status": "done",
                    "progress": 100,
                    "message": "Đã lưu trên ổ cứng",
                    "created_at": "Đã tạo",
                    "sort_ts": mp4.stat().st_mtime,
                    "description_text": desc_txt,
                    "result": {
                        "title": desc_txt.splitlines()[0] if desc_txt else folder.name,
                        "title_vi": sc.get("title_vi", folder.name),
                        "level": sc.get("level", "B1"),
                        "category": sc.get("category", "TIẾNG ANH HỌC THUẬT"),
                        "podcast_mode": p_mode,
                        "duration_formatted": "3 Phần",
                        "chapters": {"part1": p1_t, "part2": p2_t, "part3": p3_t},
                        "sentence_count": sum(len(x.get("lines", [])) for x in sc.get("scenes", [])),
                        "video_file": "video_3_phan.mp4",
                        "video_vertical_file": "video_3_phan_doc.mp4" if (folder / "video_3_phan_doc.mp4").exists() else "",
                        "tiktok_vertical_file": "video_tiktok_doc.mp4" if (folder / "video_tiktok_doc.mp4").exists() else "",
                        "tiktok_horizontal_file": "video_tiktok_ngang.mp4" if (folder / "video_tiktok_ngang.mp4").exists() else "",
                        "thumbnail_file": "thumbnail.jpg",
                        "description_file": "youtube_description.txt",
                        "srt_file": "subtitles_bilingual.srt",
                    },
                }
    return sorted(JOBS.values(), key=lambda x: x.get("sort_ts", 0), reverse=True)


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str) -> Dict[str, str]:
    JOBS.pop(job_id, None)
    target = (OUTPUT_DIR / job_id).resolve()
    if target.exists() and target.parent == OUTPUT_DIR.resolve():
        shutil.rmtree(target, ignore_errors=True)
    return {"status": "deleted"}


@app.post("/api/open-folder/{job_id}")
async def open_folder(job_id: str) -> Dict[str, str]:
    target = (OUTPUT_DIR / job_id).resolve()
    if not target.exists():
        target = OUTPUT_DIR
    subprocess.Popen(["explorer.exe", str(target)])
    return {"status": "opened"}


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>UNI Podcast Studio</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Be+Vietnam+Pro:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0f1117;
    --panel: #161922;
    --panel-alt: #1c202c;
    --input-bg: #10131a;
    --border: rgba(255, 255, 255, 0.08);
    --border-focus: #38bdf8;
    --text: #f1f5f9;
    --muted: #94a3b8;
    --accent: #0ea5e9;
    --accent-hover: #0284c7;
    --amber: #f59e0b;
    --emerald: #10b981;
    --danger: #ef4444;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Be Vietnam Pro', system-ui, -apple-system, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    padding: 20px 28px;
    line-height: 1.5;
  }

  /* Header */
  .topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding-bottom: 16px;
    margin-bottom: 22px;
    border-bottom: 1px solid var(--border);
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .brand-badge {
    width: 40px;
    height: 40px;
    border-radius: 10px;
    background: linear-gradient(135deg, #0ea5e9, #2563eb);
    display: grid;
    place-items: center;
    font-size: 20px;
  }
  .brand h1 {
    font-size: 18px;
    font-weight: 800;
    letter-spacing: -0.01em;
  }
  .brand p {
    font-size: 12.5px;
    color: var(--muted);
  }

  /* Layout */
  .workspace {
    display: grid;
    grid-template-columns: 400px 1fr;
    gap: 24px;
    align-items: start;
  }
  @media (max-width: 1024px) {
    .workspace { grid-template-columns: 1fr; }
  }

  .panel {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 20px;
  }
  .panel-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 16px;
  }
  .panel-title {
    font-size: 15px;
    font-weight: 700;
  }

  /* Mode Switcher */
  .mode-switch {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 6px;
    background: var(--input-bg);
    padding: 5px;
    border-radius: 11px;
    border: 1px solid var(--border);
    margin-bottom: 16px;
  }
  .mode-btn {
    background: transparent;
    color: var(--muted);
    border: none;
    padding: 9px 10px;
    border-radius: 8px;
    font-family: inherit;
    font-size: 13px;
    font-weight: 700;
    cursor: pointer;
    transition: all 0.15s;
  }
  .mode-btn:hover { color: var(--text); }
  .mode-btn.active {
    background: var(--panel-alt);
    color: #fff;
    box-shadow: 0 2px 8px rgba(0,0,0,0.25);
  }

  /* Form Controls */
  .field { margin-bottom: 14px; }
  .field-label {
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 12.5px;
    font-weight: 600;
    color: #cbd5e1;
    margin-bottom: 6px;
  }
  .field-hint {
    font-size: 11.5px;
    font-weight: 400;
    color: var(--muted);
  }
  textarea, input, select {
    width: 100%;
    background: var(--input-bg);
    border: 1px solid rgba(255,255,255,0.11);
    color: var(--text);
    border-radius: 10px;
    padding: 10px 12px;
    font-family: inherit;
    font-size: 13.5px;
    transition: border-color 0.15s;
  }
  textarea:focus, input:focus, select:focus {
    outline: none;
    border-color: var(--border-focus);
  }
  .grid-2 {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
  }

  /* Buttons */
  .primary-btn {
    width: 100%;
    padding: 13px 16px;
    border: none;
    border-radius: 11px;
    background: var(--accent);
    color: #fff;
    font-family: inherit;
    font-size: 14.5px;
    font-weight: 700;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    transition: background 0.15s, transform 0.1s;
    margin-top: 6px;
  }
  .primary-btn:hover { background: var(--accent-hover); }
  .primary-btn:active { transform: scale(0.99); }
  .primary-btn:disabled { opacity: 0.55; cursor: not-allowed; }

  .btn {
    background: var(--panel-alt);
    border: 1px solid rgba(255,255,255,0.1);
    color: var(--text);
    padding: 8px 13px;
    border-radius: 9px;
    font-family: inherit;
    font-size: 12.5px;
    font-weight: 600;
    text-decoration: none;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    transition: background 0.15s;
  }
  .btn:hover { background: #262c3d; }
  .btn-primary-soft {
    background: rgba(14, 165, 233, 0.15);
    border-color: rgba(14, 165, 233, 0.35);
    color: #7dd3fc;
  }
  .btn-primary-soft:hover { background: rgba(14, 165, 233, 0.25); }
  .btn-danger-soft {
    background: transparent;
    border-color: transparent;
    color: #94a3b8;
    padding: 6px 9px;
  }
  .btn-danger-soft:hover {
    background: rgba(239, 68, 68, 0.15);
    color: #fca5a5;
  }

  /* Quick Chips */
  .chips {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    margin-top: 7px;
  }
  .chip {
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid rgba(255, 255, 255, 0.1);
    color: #cbd5e1;
    font-family: inherit;
    font-size: 11.5px;
    font-weight: 600;
    padding: 4px 10px;
    border-radius: 7px;
    cursor: pointer;
  }
  .chip:hover {
    background: rgba(14, 165, 233, 0.15);
    border-color: rgba(14, 165, 233, 0.4);
    color: #fff;
  }
  .chip.active {
    background: var(--accent);
    border-color: var(--accent);
    color: #fff;
  }

  /* Advanced Drawer */
  details.adv-drawer {
    margin: 12px 0;
    background: var(--input-bg);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 9px 12px;
  }
  details.adv-drawer summary {
    cursor: pointer;
    font-size: 12.5px;
    font-weight: 600;
    color: var(--muted);
    user-select: none;
  }
  details.adv-drawer[open] summary {
    margin-bottom: 12px;
    color: var(--text);
  }

  /* Hot Candidates List */
  .cand-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin-top: 12px;
  }
  .cand-item {
    background: var(--input-bg);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 11px 12px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }
  .cand-item.best {
    border-color: rgba(16, 185, 129, 0.45);
    background: rgba(16, 185, 129, 0.06);
  }
  .cand-info h5 {
    font-size: 13px;
    font-weight: 700;
    margin-bottom: 3px;
  }
  .cand-info p {
    font-size: 11.5px;
    color: var(--muted);
  }

  /* Video Cards List */
  .jobs-list {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .job-card {
    background: var(--panel-alt);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 16px;
  }
  .job-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    margin-bottom: 12px;
  }
  .job-title-wrap {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
  .lvl-badge {
    background: var(--amber);
    color: #0f1117;
    font-weight: 800;
    font-size: 11.5px;
    padding: 2px 8px;
    border-radius: 6px;
  }
  .job-title {
    font-size: 15px;
    font-weight: 700;
  }
  .status-pill {
    font-size: 11.5px;
    font-weight: 700;
    padding: 3px 10px;
    border-radius: 99px;
  }
  .status-running { background: rgba(14, 165, 233, 0.18); color: #38bdf8; }
  .status-done { background: rgba(16, 185, 129, 0.18); color: #34d399; }
  .status-queued { background: rgba(255, 255, 255, 0.08); color: #94a3b8; }
  .status-error { background: rgba(239, 68, 68, 0.18); color: #f87171; }

  .progress-track {
    background: var(--input-bg);
    height: 8px;
    border-radius: 99px;
    overflow: hidden;
    margin: 8px 0 6px;
  }
  .progress-fill {
    height: 100%;
    background: linear-gradient(90deg, var(--accent), var(--emerald));
    transition: width 0.3s ease;
  }

  .media-row {
    display: grid;
    grid-template-columns: 260px 1fr;
    gap: 14px;
    align-items: start;
  }
  @media (max-width: 860px) {
    .media-row { grid-template-columns: 1fr; }
  }
  .thumb-box, .player-box {
    background: var(--input-bg);
    border: 1px solid var(--border);
    border-radius: 11px;
    padding: 10px;
  }
  .box-label {
    font-size: 11.5px;
    font-weight: 600;
    color: var(--muted);
    margin-bottom: 8px;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .thumb-box img {
    width: 100%;
    aspect-ratio: 16 / 9;
    object-fit: cover;
    border-radius: 7px;
    display: block;
  }
  .player-box video {
    width: 100%;
    aspect-ratio: 16 / 9;
    max-height: 340px;
    background: #000;
    border-radius: 7px;
    display: block;
  }
  .card-actions {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 12px;
    padding-top: 12px;
    border-top: 1px solid var(--border);
  }
  .card-actions-left {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }

  /* Toast Notification */
  #toast {
    position: fixed;
    bottom: 24px;
    right: 24px;
    background: #10b981;
    color: #052e16;
    font-weight: 700;
    font-size: 13px;
    padding: 10px 16px;
    border-radius: 10px;
    box-shadow: 0 10px 25px rgba(0,0,0,0.4);
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.2s;
    z-index: 9999;
  }
  #toast.show { opacity: 1; }
</style>
</head>
<body>

<header class="topbar">
  <div class="brand">
    <div class="brand-badge">🎧</div>
    <div>
      <h1>UNI Podcast Studio</h1>
      <p>Tạo Video Luyện Nghe Tiếng Anh 3 Phần & Thumbnail Tự Động</p>
    </div>
  </div>
  <button class="btn" onclick="openFolder('')">📂 Mở thư mục chứa tất cả Video</button>
</header>

<main class="workspace">
  <!-- CỘT TRÁI: BẢNG TẠO VIDEO GỌN NHẸ -->
  <section class="panel">
    <div class="mode-switch">
      <button type="button" class="mode-btn active" id="btnModeDirect" onclick="setMode('direct')">✍️ Nhập chủ đề</button>
      <button type="button" class="mode-btn" id="btnModeHot" onclick="setMode('hot')">🔥 Tìm chủ đề Hot</button>
    </div>

    <!-- CHẾ ĐỘ 1: NHẬP CHỦ ĐỀ TRỰC TIẾP -->
    <div id="paneDirect">
      <div class="field">
        <div class="field-label">
          <span>Chủ đề bài nghe</span>
          <span class="field-hint">Nhập 1 hoặc nhiều dòng</span>
        </div>
        <textarea id="topicInput" rows="2" placeholder="VD: Bạn thân mượn tiền mãi không trả...">THÍCH THẦM</textarea>
        <div class="chips">
          <button type="button" class="chip" onclick="pickSample('THÍCH THẦM', '')">Thích thầm</button>
          <button type="button" class="chip" onclick="pickSample('BẠN THÂN MƯỢN TIỀN KHÔNG TRẢ', 'Cho bạn vay tiền lúc khó khăn nhưng bạn khất lần 3 tháng dù vẫn đi cà phê sang chảnh.')">Bạn mượn tiền</button>
          <button type="button" class="chip" onclick="pickSample('CÔNG CHÚA PHÒNG TRỌ', 'Bạn cùng phòng ở bẩn nhưng ra đường luôn ăn mặc lộng lẫy.')">Bạn cùng phòng</button>
        </div>
      </div>

      <div class="field">
        <div class="field-label">
          <span>Tình huống / Ý tưởng chi tiết</span>
          <span class="field-hint">Không bắt buộc</span>
        </div>
        <textarea id="descInput" rows="2" placeholder="Để trống nếu muốn AI tự nghĩ tình huống, hoặc ghi ngắn gọn ý bạn muốn..."></textarea>
      </div>
    </div>

    <!-- CHẾ ĐỘ 2: AI SĂN CHỦ ĐỀ HOT -->
    <div id="paneHot" style="display:none;">
      <div class="field">
        <div class="field-label">
          <span>Từ khoá vụ việc / Drama đang Hot</span>
          <span class="field-hint">Nhấn Enter hoặc bấm Quét</span>
        </div>
        <input type="text" id="hotQueryInput" placeholder="VD: Ngoại tình công sở, drama bạn thân... (Để trống AI tự quét)" onkeydown="if(event.key==='Enter'){event.preventDefault();scanHotTopics();}" />
      </div>
      <button type="button" id="btnScanHot" class="btn btn-primary-soft" style="width:100%;justify-content:center;padding:10px;" onclick="scanHotTopics()">
        🔍 Quét Mới 5 Chủ Đề Đang Hot Trên Mạng
      </button>
      <div id="hotCandidatesList" class="cand-list"></div>
    </div>

    <hr style="border:none;border-top:1px solid var(--border);margin:14px 0;">

    <!-- CÀI ĐẶT CHUNG GỌN GÀNG (CHỈ 4 Ô CẦN THIẾT) -->
    <div class="grid-2 field">
      <div>
        <div class="field-label"><span>Trình độ (Level)</span></div>
        <select id="levelSelect">
          <option value="A1">A1 — Sơ cấp</option>
          <option value="A2">A2 — Căn bản</option>
          <option value="B1" selected>B1 — Trung cấp</option>
          <option value="B2">B2 — Khá giỏi</option>
          <option value="C1">C1 — Cao cấp</option>
          <option value="C2">C2 — Bản xứ</option>
        </select>
      </div>
      <div>
        <div class="field-label"><span>Thời lượng</span></div>
        <select id="durationSelect">
          <option value="8" selected>Ngắn (~2 - 3 phút)</option>
          <option value="18">Vừa (~6 - 8 phút)</option>
          <option value="32">Dài (~12 - 15 phút)</option>
          <option value="52">Tập lớn (~20 phút)</option>
        </select>
      </div>
    </div>

    <div class="grid-2 field">
      <div>
        <div class="field-label"><span>Giọng Nữ (Anna)</span></div>
        <select id="voiceFemaleSelect">
          <option value="gemini:Aoede" selected>Aoede (Truyền cảm)</option>
          <option value="gemini:Kore">Kore (Trầm ấm)</option>
          <option value="gemini:Leda">Leda (Trẻ trung)</option>
          <option value="gemini:Zephyr">Zephyr (Hiện đại)</option>
          <option value="gemini:Callirrhoe">Callirrhoe (Dịu dàng)</option>
          <option value="gemini:Despina">Despina (Kịch tính)</option>
          <option value="gemini:Autonoe">Autonoe (Rõ chữ)</option>
          <option value="gemini:Achernar">Achernar (Sâu sắc)</option>
          <option value="gemini:Sulafat">Sulafat (Kể chuyện)</option>
        </select>
      </div>
      <div>
        <div class="field-label"><span>Giọng Nam (Tom)</span></div>
        <select id="voiceMaleSelect">
          <option value="gemini:Puck" selected>Puck (Trẻ trung)</option>
          <option value="gemini:Fenrir">Fenrir (Trầm điện ảnh)</option>
          <option value="gemini:Charon">Charon (Điềm đạm)</option>
          <option value="gemini:Enceladus">Enceladus (Lãng mạn)</option>
          <option value="gemini:Orus">Orus (Dứt khoát)</option>
          <option value="gemini:Iapetus">Iapetus (Thân thiện)</option>
          <option value="gemini:Umbriel">Umbriel (Thư thái)</option>
          <option value="gemini:Achird">Achird (Năng động)</option>
          <option value="gemini:Algenib">Algenib (Trầm sâu)</option>
        </select>
      </div>
    </div>

    <!-- TUỲ CHỌN MỞ RỘNG (ẨN GỌN MẶC ĐỊNH) -->
    <details class="adv-drawer">
      <summary>⚙️ Tuỳ chọn nâng cao (Phong cách, Kịch bản có sẵn...)</summary>
      <div class="grid-2 field">
        <div>
          <div class="field-label"><span>Mức độ nội dung</span></div>
          <select id="sensitivitySelect">
            <option value="normal" selected>🙂 Đời thường tự nhiên</option>
            <option value="sensitive_ok">🌶️ Drama / Nhạy cảm</option>
            <option value="polite">😇 Lịch sự / Học đường</option>
          </select>
        </div>
        <div>
          <div class="field-label"><span>Số người đọc</span></div>
          <select id="podcastModeSelect">
            <option value="dialogue" selected>👫 2 Người (Anna & Tom)</option>
            <option value="monologue">🎙️ 1 Người (Độc thoại)</option>
          </select>
        </div>
      </div>
      <div class="field" style="margin-bottom:0;">
        <div class="field-label">
          <span>Tự dán kịch bản có sẵn</span>
          <span class="field-hint">Để trống nếu AI tự viết</span>
        </div>
        <textarea id="customScriptInput" rows="3" placeholder="Anna: Hello Tom! | Chào Tom!&#10;Tom: Hi Anna! | Chào Anna!"></textarea>
      </div>
    </details>

    <button id="createBtn" class="primary-btn" onclick="handleCreateVideo()">
      🚀 Tạo Video Ngay
    </button>
    <div id="createStatusMsg" style="font-size:12px;color:var(--muted);margin-top:8px;text-align:center;"></div>
  </section>

  <!-- CỘT PHẢI: KHO VIDEO CỦA BẠN -->
  <section class="panel">
    <div class="panel-header">
      <span class="panel-title">🎬 Kho Video Đã Tạo</span>
      <span id="jobCountBadge" style="font-size:12px;color:var(--muted);"></span>
    </div>
    <div id="jobsContainer" class="jobs-list"></div>
  </section>
</main>

<div id="toast">Đã sao chép!</div>

<script>
const DEFAULT_API_KEY = "";
let currentMode = 'direct';
let hotCandidates = [];
let isScanningHot = false;

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2200);
}

function setMode(m) {
  currentMode = m;
  document.getElementById('btnModeDirect').classList.toggle('active', m === 'direct');
  document.getElementById('btnModeHot').classList.toggle('active', m === 'hot');
  document.getElementById('paneDirect').style.display = (m === 'direct') ? 'block' : 'none';
  document.getElementById('paneHot').style.display = (m === 'hot') ? 'block' : 'none';
  const createBtn = document.getElementById('createBtn');
  if (m === 'hot') {
    createBtn.innerHTML = '🔍 Quét 5 Chủ Đề Đang Hot Trên Mạng';
    if (hotCandidates.length === 0 && !isScanningHot) {
      scanHotTopics();
    }
  } else {
    createBtn.innerHTML = '🚀 Tạo Video Ngay';
  }
}

function pickSample(topic, desc) {
  document.getElementById('topicInput').value = topic;
  document.getElementById('descInput').value = desc || '';
}

async function handleCreateVideo() {
  if (currentMode === 'hot') {
    await scanHotTopics();
    return;
  }
  const topicRaw = document.getElementById('topicInput').value.trim();
  const desc = document.getElementById('descInput').value.trim();
  if (!topicRaw) {
    alert('Vui lòng nhập chủ đề bài nghe!');
    return;
  }

  const level = document.getElementById('levelSelect').value;
  const target_sentences = parseInt(document.getElementById('durationSelect').value, 10) || 10;
  const voice = document.getElementById('voiceFemaleSelect').value;
  const voice_male = document.getElementById('voiceMaleSelect').value;
  const sensitivity_mode = document.getElementById('sensitivitySelect').value;
  const podcast_mode = document.getElementById('podcastModeSelect').value;
  const custom_script = document.getElementById('customScriptInput').value.trim();

  const btn = document.getElementById('createBtn');
  const statusEl = document.getElementById('createStatusMsg');
  btn.disabled = true;

  try {
    const lines = topicRaw.split('\n').map(l => l.trim()).filter(Boolean);
    // Nếu người dùng nhập 1 chủ đề kèm mô tả tình huống (và không dán kịch bản sẵn), dùng quy trình biên kịch sâu Mode A
    if (lines.length === 1 && desc && !custom_script && !lines[0].includes('|')) {
      statusEl.textContent = '⏳ Đang khởi tạo kịch bản 5 bước và dựng video...';
      await fetch('/api/mode-ab/create', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          mode: 'A',
          topic: lines[0],
          description: desc,
          level,
          target_sentences,
          cta: 'Đăng ký kênh UNI PODCAST và bình luận ý kiến của bạn bên dưới',
          sensitivity_mode,
          voice,
          voice_male,
          rate: '-12%',
          channel_tag: 'UNI PODCAST',
          gemini_api_key: DEFAULT_API_KEY
        })
      });
    } else {
      statusEl.textContent = '⏳ Đã đưa vào hàng đợi sản xuất video...';
      const items = lines.map((line, idx) => {
        const parts = line.split('|').map(x => x.trim());
        return {
          topic: parts[0] || 'CHỦ ĐỀ MỚI',
          level: parts[1] || level,
          category: parts[2] || 'TIẾNG ANH THỰC TẾ',
          podcast_mode,
          sensitivity_mode,
          custom_script: idx === 0 ? custom_script : ''
        };
      });
      await fetch('/api/batch', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          items,
          podcast_mode,
          subtitle_style: 'teleprompter',
          sensitivity_mode,
          voice,
          voice_male,
          rate: '-12%',
          target_sentences,
          channel_tag: 'UNI PODCAST',
          gemini_api_key: DEFAULT_API_KEY
        })
      });
    }
    statusEl.textContent = '';
    showToast('🚀 Đã bắt đầu dựng video! Xem tiến độ bên phải.');
    await refreshJobs();
  } catch (e) {
    statusEl.textContent = 'Lỗi: ' + e.message;
  } finally {
    btn.disabled = false;
  }
}

async function scanHotTopics() {
  if (isScanningHot) return;
  isScanningHot = true;
  const topic = document.getElementById('hotQueryInput').value.trim();
  const level = document.getElementById('levelSelect').value;
  const sensitivity_mode = document.getElementById('sensitivitySelect').value;
  const btn = document.getElementById('btnScanHot');
  const createBtn = document.getElementById('createBtn');
  const box = document.getElementById('hotCandidatesList');

  btn.disabled = true;
  btn.textContent = '⏳ AI đang quét xu hướng mạng xã hội...';
  if (currentMode === 'hot') {
    createBtn.disabled = true;
    createBtn.textContent = '⏳ Đang quét 5 chủ đề Hot...';
  }
  box.innerHTML = `<div style="font-size:12.5px;color:#38bdf8;padding:10px;background:rgba(14,165,233,0.08);border-radius:8px;text-align:center;">🔍 AI đang săn 5 chủ đề & drama hấp dẫn nhất (khoảng 2-3 giây)...</div>`;

  try {
    const res = await fetch('/api/mode-b/discover', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        level,
        topic,
        description: '',
        cta: 'Đăng ký kênh UNI PODCAST',
        sensitivity_mode,
        gemini_api_key: DEFAULT_API_KEY
      })
    });
    if (!res.ok) {
      throw new Error('Server trả về lỗi ' + res.status);
    }
    const data = await res.json();
    hotCandidates = data.candidates || [];
    const bestId = data.best_id || 1;

    if (!hotCandidates.length) {
      box.innerHTML = `<div style="color:#f87171;font-size:12.5px;padding:8px 0;">Không tìm thấy chủ đề nào, vui lòng thử lại.</div>`;
    } else {
      box.innerHTML = hotCandidates.map((c, idx) => {
        const isBest = (c.id === bestId);
        return `
          <div class="cand-item ${isBest ? 'best' : ''}">
            <div class="cand-info">
              <h5>${isBest ? '⭐ ' : '🔥 '}${c.topic || ''}</h5>
              <p>${c.description || ''}</p>
            </div>
            <button type="button" class="btn btn-primary-soft" style="flex-shrink:0;" onclick="selectHotCandidate(${idx})">
              🎬 Chọn & Tạo
            </button>
          </div>
        `;
      }).join('');
    }
  } catch (e) {
    box.innerHTML = `<div style="color:#f87171;font-size:12.5px;padding:8px 0;">Lỗi khi quét chủ đề: ${e.message}</div>`;
  } finally {
    isScanningHot = false;
    btn.disabled = false;
    btn.textContent = '🔍 Quét Mới 5 Chủ Đề Đang Hot Trên Mạng';
    if (currentMode === 'hot') {
      createBtn.disabled = false;
      createBtn.innerHTML = '🔍 Quét 5 Chủ Đề Đang Hot Trên Mạng';
    }
  }
}

async function selectHotCandidate(idx) {
  const c = hotCandidates[idx];
  if (!c) return;
  document.getElementById('topicInput').value = c.topic || '';
  document.getElementById('descInput').value = c.description || '';
  setMode('direct');
  await handleCreateVideo();
}

function mmssToSec(mmss) {
  if (!mmss || !mmss.includes(':')) return 0;
  const p = mmss.split(':').map(Number);
  return p.length === 3 ? p[0]*3600 + p[1]*60 + p[2] : p[0]*60 + p[1];
}

function seekVideo(vidId, sec) {
  const v = document.getElementById(vidId);
  if (v) {
    v.currentTime = sec;
    v.play();
  }
}

function switchVideoPreview(vidId, srcUrl, isVertical, btnEl) {
  const v = document.getElementById(vidId);
  if (!v) return;
  v.src = srcUrl;
  v.style.aspectRatio = isVertical ? '9 / 16' : '16 / 9';
  v.style.maxHeight = isVertical ? '380px' : '340px';
  v.style.margin = '0 auto';
  if (btnEl && btnEl.parentElement) {
    Array.from(btnEl.parentElement.children).forEach(b => b.classList.remove('active'));
    btnEl.classList.add('active');
  }
  v.play();
}

async function openFolder(jobId) {
  await fetch(`/api/open-folder/${jobId || '_root'}`, {method: 'POST'});
}

function copyText(id) {
  const el = document.getElementById(id);
  if (!el) return;
  navigator.clipboard.writeText(el.value);
  showToast('📋 Đã copy Mô tả YouTube + Chapters!');
}

async function deleteJob(jobId) {
  if (!confirm('Bạn có chắc muốn xoá video này?')) return;
  await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, {method: 'DELETE'});
  const card = document.getElementById(`card_${jobId}`);
  if (card) card.remove();
  delete cardStates[jobId];
}

const cardStates = {};
function renderJobCardInner(j) {
  const statusMap = {
    queued: ['Đang chờ', 'status-queued'],
    running: [`Đang tạo (${j.progress || 0}%)`, 'status-running'],
    done: ['Hoàn thành', 'status-done'],
    error: ['Lỗi', 'status-error']
  };
  const [stText, stClass] = statusMap[j.status] || ['...', 'status-queued'];

  let bodyHtml = `
    <div class="progress-track"><div class="progress-fill" style="width:${j.progress || 0}%"></div></div>
    <div style="font-size:12.5px;color:var(--muted);">${j.message || ''}</div>
  `;

  if (j.status === 'done' && j.result) {
    const r = j.result;
    const p2Sec = mmssToSec(r.chapters.part2);
    const p3Sec = mmssToSec(r.chapters.part3);
    const vidDomId = `vid_${j.id}`;
    const descDomId = `desc_${j.id}`;
    const encId = encodeURIComponent(j.id);

    const vVertUrl = r.video_vertical_file ? `/outputs/${encId}/${r.video_vertical_file}` : '';
    const tkVertUrl = r.tiktok_vertical_file ? `/outputs/${encId}/${r.tiktok_vertical_file}` : '';
    const tkHorizUrl = r.tiktok_horizontal_file ? `/outputs/${encId}/${r.tiktok_horizontal_file}` : '';

    bodyHtml = `
      <div class="media-row">
        <div class="thumb-box">
          <div class="box-label">
            <span>Ảnh bìa (Thumbnail)</span>
            <a href="/outputs/${encId}/${r.thumbnail_file}" download style="color:#38bdf8;text-decoration:none;">Tải ảnh</a>
          </div>
          <img src="/outputs/${encId}/${r.thumbnail_file}?t=${j.sort_ts || 1}" alt="Thumbnail" />
        </div>

        <div class="player-box">
          <div class="box-label">
            <div class="chips" style="margin-top:0;">
              <button class="chip active" onclick="switchVideoPreview('${vidDomId}', '/outputs/${encId}/${r.video_file}?v=${j.sort_ts || 1}', false, this)">🖥️ Ngang 16:9</button>
              ${vVertUrl ? `<button class="chip" onclick="switchVideoPreview('${vidDomId}', '${vVertUrl}?v=${j.sort_ts || 1}', true, this)">📱 Dọc 9:16</button>` : ''}
              ${tkVertUrl ? `<button class="chip" onclick="switchVideoPreview('${vidDomId}', '${tkVertUrl}?v=${j.sort_ts || 1}', true, this)">🔥 TikTok Dọc</button>` : ''}
              ${tkHorizUrl ? `<button class="chip" onclick="switchVideoPreview('${vidDomId}', '${tkHorizUrl}?v=${j.sort_ts || 1}', false, this)">🎬 TikTok Ngang</button>` : ''}
            </div>
            <span>${r.sentence_count} câu</span>
          </div>

          <video id="${vidDomId}" controls preload="metadata" src="/outputs/${encId}/${r.video_file}?v=${j.sort_ts || 1}"></video>

          <div class="chips" style="margin-top:8px;">
            <button class="chip" onclick="seekVideo('${vidDomId}', 5)">Phần 1: Sub Anh</button>
            <button class="chip" onclick="seekVideo('${vidDomId}', ${p2Sec + 2})">Phần 2: Song ngữ (${r.chapters.part2})</button>
            <button class="chip" onclick="seekVideo('${vidDomId}', ${p3Sec + 2})">Phần 3: Nghe ngấm (${r.chapters.part3})</button>
          </div>
        </div>
      </div>

      <textarea id="${descDomId}" style="display:none;" readonly>${j.description_text || ''}</textarea>

      <div class="card-actions">
        <div class="card-actions-left">
          <button class="btn btn-primary-soft" onclick="openFolder('${j.id}')">📂 Mở thư mục lấy Video</button>
          <button class="btn" onclick="copyText('${descDomId}')">📋 Copy Mô tả YouTube</button>
          <a class="btn" href="/outputs/${encId}/${r.video_file}?v=${j.sort_ts || 1}" download="${j.level}_${j.topic}_16x9.mp4">⬇️ Tải 16:9</a>
          ${vVertUrl ? `<a class="btn" href="${vVertUrl}?v=${j.sort_ts || 1}" download="${j.level}_${j.topic}_9x16.mp4">⬇️ Tải 9:16</a>` : ''}
        </div>
        <button class="btn btn-danger-soft" onclick="deleteJob('${j.id}')" title="Xoá video này">🗑️ Xoá</button>
      </div>
    `;
  }

  return `
    <div class="job-top">
      <div class="job-title-wrap">
        <span class="lvl-badge">${j.level}</span>
        <span class="job-title">${j.topic}</span>
      </div>
      <span class="status-pill ${stClass}">${stText}</span>
    </div>
    ${bodyHtml}
  `;
}

async function refreshJobs() {
  try {
    const res = await fetch('/api/jobs');
    const jobs = await res.json();
    const container = document.getElementById('jobsContainer');
    const countBadge = document.getElementById('jobCountBadge');
    if (countBadge) countBadge.textContent = jobs.length ? `${jobs.length} video` : '';

    if (!jobs.length) {
      container.innerHTML = `<p id="emptyMsg" style="color:var(--muted);font-size:13.5px;padding:16px 0;">Chưa có video nào. Hãy nhập chủ đề bên trái và bấm <b>"Tạo Video Ngay"</b>!</p>`;
      return;
    }
    const emptyEl = document.getElementById('emptyMsg');
    if (emptyEl) emptyEl.remove();

    jobs.forEach((j, idx) => {
      const stateKey = `${j.status}|${j.progress}|${j.message}|${j.sort_ts || 0}`;
      let card = document.getElementById(`card_${j.id}`);
      if (!card) {
        card = document.createElement('div');
        card.id = `card_${j.id}`;
        card.className = 'job-card';
        card.innerHTML = renderJobCardInner(j);
        if (idx === 0 && container.firstChild) {
          container.insertBefore(card, container.firstChild);
        } else {
          container.appendChild(card);
        }
        cardStates[j.id] = stateKey;
      } else if (cardStates[j.id] !== stateKey) {
        card.innerHTML = renderJobCardInner(j);
        cardStates[j.id] = stateKey;
      }
    });
  } catch (e) {
    console.error(e);
  }
}

setInterval(refreshJobs, 1500);
refreshJobs();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return INDEX_HTML


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8095"))
    uvicorn.run(app, host="127.0.0.1", port=port)

