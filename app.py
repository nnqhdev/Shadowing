import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from generator import DEFAULT_GEMINI_KEY, build_video_job, safe_err
from screenwriter import discover_hot_topics_mode_b, generate_5step_screenplay

logger = logging.getLogger(__name__)
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
    pair_type: str = "male_female"
    custom_script: str = ""
    sensitivity_mode: str = "normal"
    fast_test: bool = False


class BatchRequest(BaseModel):
    items: List[BatchItem]
    podcast_mode: str = "dialogue"
    pair_type: str = "male_female"
    subtitle_style: str = "teleprompter"
    sensitivity_mode: str = "normal"
    voice: str = "gemini:Aoede"
    voice_male: str = "gemini:Puck"
    rate: str = "+0%"
    target_sentences: int = 10
    channel_tag: str = "UNI PODCAST"
    gemini_api_key: str = DEFAULT_GEMINI_KEY
    fast_test: bool = False


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
    target_sentences: int = 10
    podcast_mode: str = "dialogue"
    pair_type: str = "male_female"
    cta: str = "Đăng ký kênh, ôn từ vựng & trả lời câu hỏi dưới bình luận"
    sensitivity_mode: str = "normal"
    voice: str = "gemini:Aoede"
    voice_male: str = "gemini:Puck"
    rate: str = "+0%"
    channel_tag: str = "UNI PODCAST"
    gemini_api_key: str = DEFAULT_GEMINI_KEY
    fast_test: bool = False



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
                is_fast_test = bool(job.get("fast_test", req.fast_test))
                res = await build_video_job(
                    job_dir=job_dir,
                    topic=job["topic"],
                    level=job["level"],
                    category=job["category"],
                    podcast_mode=job.get("podcast_mode", req.podcast_mode),
                    pair_type=job.get("pair_type", req.pair_type),
                    voice=req.voice,
                    voice_male=req.voice_male,
                    rate=req.rate,
                    target_sentences=req.target_sentences,
                    channel_tag=req.channel_tag,
                    gemini_api_key=req.gemini_api_key or DEFAULT_GEMINI_KEY,
                    custom_script=job.get("custom_script", ""),
                    subtitle_style=req.subtitle_style,
                    sensitivity_mode=job.get("sensitivity_mode", req.sensitivity_mode),
                    fast_test=is_fast_test,
                    on_progress=on_prog,
                )
                job["status"] = "done"
                job["progress"] = 100
                job["message"] = (
                    "Hoàn tất bản Test Nhanh (480p • 1 phần)!"
                    if is_fast_test
                    else "Hoàn tất xuất video Ngang (16:9) & Dọc (9:16)!"
                )
                job["result"] = res
                job["description_text"] = (job_dir / "youtube_description.txt").read_text(
                    encoding="utf-8"
                )
            except Exception as e:
                logger.exception("Job %s failed", jid)
                job["status"] = "error"
                job["message"] = f"Lỗi: {safe_err(e)}"


@app.post("/api/batch")
async def create_batch(req: BatchRequest) -> Dict[str, Any]:
    created_ids: List[str] = []
    now_ts = time.time()
    for item in req.items:
        if not item.topic.strip():
            continue
        item_slug = slugify(item.topic)
        item_lvl = item.level.strip().upper() or "B1"
        dup_id = next(
            (
                j["id"]
                for j in JOBS.values()
                if j.get("status") in ("queued", "running")
                and slugify(j.get("topic", "")) == item_slug
                and j.get("level", "").upper() == item_lvl
                and (now_ts - float(j.get("sort_ts", 0))) < 5.0
            ),
            None,
        )
        if dup_id:
            continue
        ts = time.strftime("%H%M%S")
        mode_val = item.podcast_mode or req.podcast_mode
        pair_val = item.pair_type or req.pair_type or "male_female"
        sens_val = item.sensitivity_mode or req.sensitivity_mode or "normal"
        jid = f"{ts}_{item.level.upper()}_{item_slug}_{uuid.uuid4().hex[:4]}"
        JOBS[jid] = {
            "id": jid,
            "topic": item.topic.strip(),
            "level": item.level.strip().upper(),
            "category": item.category.strip().upper(),
            "podcast_mode": mode_val,
            "pair_type": pair_val,
            "sensitivity_mode": sens_val,
            "custom_script": item.custom_script,
            "fast_test": bool(item.fast_test or req.fast_test),
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
    title_slug = slugify(raw_title)
    lvl = req.level.strip().upper() or "B1"
    now_ts = time.time()
    existing_jid = next(
        (
            j["id"]
            for j in JOBS.values()
            if j.get("status") in ("queued", "running")
            and slugify(j.get("topic", "")) == title_slug
            and j.get("level", "").upper() == lvl
            and (now_ts - float(j.get("sort_ts", 0))) < 5.0
        ),
        None,
    )
    if existing_jid:
        return {"created_id": existing_jid, "deduplicated": True}

    ts = time.strftime("%H%M%S")
    jid = f"{ts}_{lvl}_{title_slug}_{uuid.uuid4().hex[:4]}"

    job_dir = OUTPUT_DIR / jid
    job_dir.mkdir(parents=True, exist_ok=True)

    pmode = (req.podcast_mode or "dialogue").strip().lower()
    if pmode not in ("dialogue", "monologue"):
        pmode = "dialogue"
    ptype = (req.pair_type or "male_female").strip().lower()
    if ptype not in ("male_female", "male_male", "female_female"):
        ptype = "male_female"

    if req.gemini_api_key and req.gemini_api_key.strip():
        try:
            env_path = BASE_DIR / ".env"
            env_path.write_text(f"GEMINI_API_KEY={req.gemini_api_key.strip()}\n", encoding="utf-8")
            env_path.chmod(0o600)
        except OSError as exc:
            logger.warning("Could not persist API key to .env: %s", safe_err(exc))

    JOBS[jid] = {
        "id": jid,
        "topic": raw_title,
        "level": lvl,
        "category": "⚡ TEST NHANH (480P)" if req.fast_test else f"CHẾ ĐỘ {req.mode.upper()} • BIÊN KỊCH 5 BƯỚC",
        "podcast_mode": pmode,
        "pair_type": ptype,
        "sensitivity_mode": req.sensitivity_mode,
        "custom_script": "",
        "fast_test": bool(req.fast_test),
        "dossier": {},
        "status": "running",
        "progress": 4,
        "message": f"Đang lập kịch bản 5 bước ({lvl}, {req.target_sentences} câu) từ chủ đề '{raw_title}'...",
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
                podcast_mode=pmode,
                pair_type=ptype,
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
                JOBS[jid]["message"] = (
                    "Đã lập kịch bản, đang thu âm & xuất bản Test Nhanh 480p..."
                    if req.fast_test
                    else "Đã lập kịch bản 5 bước, đang thu âm & dựng Video 3 phần..."
                )
        except Exception as exc:
            logger.warning("5-step screenplay failed, using standard script: %s", safe_err(exc))
            if jid in JOBS:
                JOBS[jid]["message"] = f"Đang khởi tạo kịch bản tiêu chuẩn ({safe_err(exc)})..."

        batch_req = BatchRequest(
            items=[
                BatchItem(
                    topic=short_title,
                    level=lvl,
                    category="⚡ TEST NHANH (480P)" if req.fast_test else f"CHẾ ĐỘ {req.mode.upper()} • BIÊN KỊCH 5 BƯỚC",
                    podcast_mode=pmode,
                    pair_type=ptype,
                    custom_script=custom_script_str,
                    sensitivity_mode=req.sensitivity_mode,
                    fast_test=bool(req.fast_test),
                )
            ],
            podcast_mode=pmode,
            pair_type=ptype,
            subtitle_style="teleprompter",
            sensitivity_mode=req.sensitivity_mode,
            voice=req.voice,
            voice_male=req.voice_male,
            rate=req.rate,
            target_sentences=req.target_sentences,
            channel_tag=req.channel_tag,
            gemini_api_key=req.gemini_api_key or DEFAULT_GEMINI_KEY,
            fast_test=bool(req.fast_test),
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
    if os.name == "nt":
        subprocess.Popen(["explorer.exe", str(target)])
    elif os.uname().sysname == "Darwin":
        subprocess.Popen(["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])
    return {"status": "opened"}


try:
    import psutil as _psutil
    _psutil.cpu_percent(interval=None)
except Exception:
    _psutil = None


def _collect_system_stats() -> Dict[str, Any]:
    cpu_pct = 0.0
    ram_pct = 0.0
    ram_used_gb = 0.0
    ram_total_gb = 0.0
    gpu_pct = 0.0
    gpu_mem_mb = 0.0

    if _psutil is not None:
        try:
            cpu_pct = round(float(_psutil.cpu_percent(interval=None)), 1)
            vm = _psutil.virtual_memory()
            ram_pct = round(float(vm.percent), 1)
            ram_used_gb = round((vm.total - vm.available) / (1024 ** 3), 1)
            ram_total_gb = round(vm.total / (1024 ** 3), 1)
        except Exception as exc:
            logger.debug("RAM stats unavailable: %s", exc)

    if os.name == "nt":
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits"],
                timeout=0.8,
                stderr=subprocess.DEVNULL,
            ).decode("utf-8", errors="ignore").strip()
            if out:
                first_line = out.splitlines()[0].split(",")
                gpu_pct = round(float(first_line[0].strip()), 1)
                if len(first_line) > 1:
                    gpu_mem_mb = round(float(first_line[1].strip()), 0)
        except Exception as exc:
            logger.debug("nvidia-smi stats unavailable: %s", exc)
    elif hasattr(os, "uname") and os.uname().sysname == "Darwin":
        try:
            out = subprocess.check_output(
                ["ioreg", "-r", "-d", "1", "-c", "IOAccelerator"],
                timeout=0.8,
                stderr=subprocess.DEVNULL,
            ).decode("utf-8", errors="ignore")
            utils = [
                int(m.group(1))
                for m in re.finditer(r'"(?:Device|Renderer|Tiler|Device Unit \d+) Utilization %"\s*=\s*(\d+)', out)
            ]
            if utils:
                gpu_pct = float(max(utils))
            mem_matches = [
                int(m.group(1))
                for m in re.finditer(r'"inUse(?:Sys|Vid)MemoryBytes"\s*=\s*(\d+)', out)
            ]
            if mem_matches:
                gpu_mem_mb = round(max(mem_matches) / (1024 ** 2), 0)
        except Exception as exc:
            logger.debug("macOS GPU stats unavailable: %s", exc)

    return {
        "cpu_percent": cpu_pct,
        "ram_percent": ram_pct,
        "ram_used_gb": ram_used_gb,
        "ram_total_gb": ram_total_gb,
        "gpu_percent": gpu_pct,
        "gpu_mem_mb": gpu_mem_mb,
    }


@app.get("/api/system-stats")
async def api_system_stats() -> Dict[str, Any]:
    return await asyncio.to_thread(_collect_system_stats)


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

  /* System Telemetry Bar (CPU / RAM / GPU) */
  .sys-telemetry {
    display: flex;
    align-items: center;
    gap: 10px;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 7px 14px;
  }
  .sys-metric {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 2px 6px;
  }
  .sys-metric + .sys-metric {
    border-left: 1px solid var(--border);
    padding-left: 12px;
  }
  .sys-label {
    font-size: 11px;
    font-weight: 700;
    color: var(--muted);
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }
  .sys-val {
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
    font-weight: 600;
    color: var(--text);
    min-width: 38px;
    text-align: right;
  }
  .sys-sub {
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    color: var(--muted);
  }
  .sys-bar {
    width: 54px;
    height: 6px;
    background: rgba(255, 255, 255, 0.08);
    border-radius: 999px;
    overflow: hidden;
  }
  .sys-bar-fill {
    height: 100%;
    width: 0%;
    border-radius: 999px;
    background: var(--accent);
    transition: width 0.35s ease, background-color 0.35s ease;
  }
  @media (max-width: 960px) {
    .topbar { flex-wrap: wrap; gap: 12px; }
    .sys-telemetry { order: 3; width: 100%; justify-content: space-around; }
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
    grid-template-columns: repeat(6, 1fr);
    gap: 5px;
    background: var(--input-bg);
    padding: 5px;
    border-radius: 11px;
    border: 1px solid var(--border);
    margin-bottom: 16px;
  }
  .mode-btn {
    grid-column: span 2;
    background: transparent;
    color: var(--muted);
    border: none;
    padding: 8px 6px;
    border-radius: 8px;
    font-family: inherit;
    font-size: 12px;
    font-weight: 700;
    cursor: pointer;
    transition: all 0.15s;
    white-space: nowrap;
  }
  .mode-btn.span-half {
    grid-column: span 3;
  }
  .mode-btn:hover { color: var(--text); }
  .mode-btn.active {
    background: var(--panel-alt);
    color: #fff;
    box-shadow: 0 2px 8px rgba(0,0,0,0.25);
  }
  .dlg-sub-tabs {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 6px;
    margin-bottom: 12px;
  }
  .pair-type-tabs {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 6px;
    margin-bottom: 12px;
  }
  .sub-tab-btn {
    background: var(--input-bg);
    border: 1px solid var(--border);
    color: #cbd5e1;
    border-radius: 9px;
    padding: 9px 8px;
    font-family: inherit;
    font-size: 12.5px;
    font-weight: 700;
    cursor: pointer;
    transition: all 0.15s;
    text-align: center;
  }
  .sub-tab-btn:hover {
    border-color: rgba(14, 165, 233, 0.45);
    color: #fff;
  }
  .sub-tab-btn.active {
    background: rgba(14, 165, 233, 0.18);
    border-color: var(--accent);
    color: #38bdf8;
    box-shadow: 0 0 0 1px rgba(14, 165, 233, 0.25);
  }
  .dialogue-summary-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    background: rgba(14, 165, 233, 0.08);
    border: 1px solid rgba(14, 165, 233, 0.25);
    border-radius: 10px;
    padding: 9px 12px;
    margin-bottom: 12px;
    cursor: pointer;
    transition: background 0.15s;
  }
  .dialogue-summary-bar:hover {
    background: rgba(14, 165, 233, 0.14);
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

  <div class="sys-telemetry" title="Thông số tài nguyên hệ thống theo thời gian thực">
    <div class="sys-metric">
      <span class="sys-label">CPU</span>
      <div class="sys-bar"><div class="sys-bar-fill" id="barCpu"></div></div>
      <span class="sys-val" id="valCpu">--%</span>
    </div>
    <div class="sys-metric">
      <span class="sys-label">RAM</span>
      <div class="sys-bar"><div class="sys-bar-fill" id="barRam"></div></div>
      <span class="sys-val" id="valRam">--%</span>
      <span class="sys-sub" id="subRam"></span>
    </div>
    <div class="sys-metric">
      <span class="sys-label">GPU</span>
      <div class="sys-bar"><div class="sys-bar-fill" id="barGpu"></div></div>
      <span class="sys-val" id="valGpu">--%</span>
      <span class="sys-sub" id="subGpu"></span>
    </div>
  </div>

  <button class="btn" onclick="openFolder('')">📂 Mở thư mục chứa tất cả Video</button>
</header>

<main class="workspace">
  <!-- CỘT TRÁI: BẢNG TẠO VIDEO GỌN NHẸ -->
  <section class="panel">
    <div class="mode-switch">
      <button type="button" class="mode-btn active" id="btnModeDirect" onclick="setMode('direct')">✍️ Nhập chủ đề</button>
      <button type="button" class="mode-btn" id="btnModeHot" onclick="setMode('hot')">🔥 Chủ đề Hot</button>
      <button type="button" class="mode-btn" id="btnModeDialogue" onclick="setMode('dialogue')">💬 Hội thoại</button>
      <button type="button" class="mode-btn span-half" id="btnModeScript" onclick="setMode('script')">📋 Kịch bản sẵn</button>
      <button type="button" class="mode-btn span-half" id="btnModeTest" onclick="setMode('test')">⚡ Test Nhanh</button>
    </div>

    <!-- CHẾ ĐỘ 1: NHẬP CHỦ ĐỀ TRỰC TIẾP -->
    <div id="paneDirect">
      <div class="field">
        <div class="field-label">
          <span>Chủ đề bài nghe</span>
          <span class="field-hint">Kể chuyện 4 chặng + Hook ấn tượng đầu tiên</span>
        </div>
        <textarea id="topicInput" rows="2" placeholder="VD: Làm lốp dự phòng, bị trà xanh cắm sừng, đụng độ bb3...">CHỈ LÀ LỐP DỰ PHÒNG</textarea>
        <div class="chips">
          <button type="button" class="chip" onclick="pickSample('CHỈ LÀ LỐP DỰ PHÒNG', 'Suốt 6 tháng ngày nào cũng nhắn tin ngọt ngào lúc 2h sáng nhưng khi hỏi danh phận thì chối, đến hôm nay phát hiện mình chỉ là lốp dự phòng.')">🛞 Lốp dự phòng</button>
          <button type="button" class="chip" onclick="pickSample('BỊ TRÀ XANH CẮM SỪNG', 'Cô bạn thân luôn miệng khuyên chia tay người yêu nhưng lại âm thầm nhắn tin thả thính và cắm sừng ngay sau lưng.')">🍵 Trà xanh cắm sừng</button>
          <button type="button" class="chip" onclick="pickSample('ĐỤNG ĐỘ BB3', 'Người yêu báo bận tăng ca nhưng lại bị bắt quả tang đang đi ăn tối lãng mạn cùng bé ba (BB3).')">💅 Đụng độ BB3</button>
          <button type="button" class="chip" onclick="pickSample('MẬP MỜ RỒI BỊ GHOST', 'Đi chơi nắm tay như người yêu suốt 4 tháng nhưng vừa tỏ tình xong thì đối phương bỗng dưng ghost biến mất.')">👻 Mập mờ bị Ghost</button>
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

    <!-- CHẾ ĐỘ 3: TAB HỘI THOẠI (ĐỘC THOẠI / ĐỐI THOẠI & CHỌN CẶP GIỌNG) -->
    <div id="paneDialogue" style="display:none;">
      <div class="field">
        <div class="field-label">
          <span>Chế độ kể chuyện</span>
          <span class="field-hint">Chọn Độc thoại hoặc Đối thoại</span>
        </div>
        <div class="dlg-sub-tabs">
          <button type="button" class="sub-tab-btn active" id="btnSubDialogue" onclick="setPodcastMode('dialogue')">👫 Đối thoại (2 người)</button>
          <button type="button" class="sub-tab-btn" id="btnSubMonologue" onclick="setPodcastMode('monologue')">🎙️ Độc thoại (1 người)</button>
        </div>
        <input type="hidden" id="podcastModeSelect" value="dialogue" />
      </div>

      <!-- KHI CHỌN ĐỐI THOẠI: CHỌN 1 TRONG 3 LOẠI CẶP + 1 CẶP GIỌNG DUY NHẤT -->
      <div id="boxPairVoiceConfig">
        <div class="field">
          <div class="field-label">
            <span>Loại cặp giọng (3 loại)</span>
            <span class="field-hint" id="pairSpeakerHint">Anna & Tom</span>
          </div>
          <div class="pair-type-tabs">
            <button type="button" class="sub-tab-btn active" id="btnPairMF" onclick="setPairType('male_female')">👫 Nam – Nữ</button>
            <button type="button" class="sub-tab-btn" id="btnPairMM" onclick="setPairType('male_male')">👬 Nam – Nam</button>
            <button type="button" class="sub-tab-btn" id="btnPairFF" onclick="setPairType('female_female')">👭 Nữ – Nữ</button>
          </div>
          <input type="hidden" id="pairTypeSelect" value="male_female" />
        </div>

        <div class="field">
          <div class="field-label">
            <span>Chọn cặp giọng đọc (1 cài đặt cho cả 2 giọng)</span>
          </div>
          <select id="voicePairSelect" onchange="updateDialogueSummary()"></select>
        </div>
      </div>

      <!-- KHI CHỌN ĐỘC THOẠI: CHỌN 1 GIỌNG KỂ CHUYỆN -->
      <div id="boxMonoVoiceConfig" style="display:none;">
        <div class="field">
          <div class="field-label">
            <span>Chọn giọng đọc Độc thoại (1 người kể chuyện)</span>
          </div>
          <select id="voiceMonoSelect" onchange="updateDialogueSummary()">
            <option value="gemini:Aoede" selected>♀️ Aoede (Nữ — Truyền cảm, kể chuyện cuốn)</option>
            <option value="gemini:Puck">♂️ Puck (Nam — Trẻ trung, tự nhiên)</option>
            <option value="gemini:Kore">♀️ Kore (Nữ — Trầm ấm, sâu sắc)</option>
            <option value="gemini:Fenrir">♂️ Fenrir (Nam — Trầm điện ảnh)</option>
            <option value="gemini:Leda">♀️ Leda (Nữ — Hiện đại, năng động)</option>
            <option value="gemini:Charon">♂️ Charon (Nam — Điềm đạm, rõ chữ)</option>
            <option value="gemini:Despina">♀️ Despina (Nữ — Kịch tính, cảm xúc)</option>
            <option value="gemini:Enceladus">♂️ Enceladus (Nam — Lãng mạn, ấm áp)</option>
          </select>
        </div>
      </div>
    </div>

    <!-- CHẾ ĐỘ 4: TAB CHUYÊN TẠO VIDEO TỪ KỊCH BẢN CÓ SẴN -->
    <div id="paneScript" style="display:none;">
      <div class="field">
        <div class="field-label">
          <span>📋 Dán kịch bản có sẵn (Song ngữ Anh | Việt)</span>
          <span class="field-hint">Tự khớp số câu, nhân vật, cảm xúc & Level</span>
        </div>
        <textarea id="customScriptInput" rows="7" oninput="onCustomScriptChange('customScriptInput','customScriptAutoBadge')" placeholder="Title: CHỈ LÀ LỐP DỰ PHÒNG&#10;Anna [sad:60%]: Why do you only text me at two in the morning? | Sao anh chỉ nhắn tin cho em lúc hai giờ sáng?&#10;Tom [anxious:60%]: I was just really busy with work all day. | Cả ngày anh bận việc quá thôi mà.&#10;Anna [sad+angry:100%]: Yet you had time to post stories with her! | Vậy mà anh lại có thời gian đăng story với cô ta!"></textarea>
        <div class="chips">
          <button type="button" class="chip" onclick="fillSampleScript('dialogue')">👫 Mẫu Đối thoại (Anna & Tom)</button>
          <button type="button" class="chip" onclick="fillSampleScript('monologue')">🎙️ Mẫu Độc thoại (1 người)</button>
          <button type="button" class="chip" onclick="document.getElementById('customScriptInput').value='';onCustomScriptChange('customScriptInput','customScriptAutoBadge');">🗑️ Xoá trắng</button>
        </div>
        <div id="customScriptAutoBadge" style="display:none;margin-top:8px;padding:8px 11px;border-radius:8px;background:rgba(16,185,129,0.12);border:1px solid rgba(16,185,129,0.35);color:#34d399;font-size:11.5px;font-weight:600;"></div>
      </div>
      <div class="field" style="margin-bottom:0;">
        <div class="field-label">
          <span>Tiêu đề bìa Thumbnail</span>
          <span class="field-hint">Tự lấy từ kịch bản nếu để trống</span>
        </div>
        <input type="text" id="scriptTitleInput" placeholder="Để trống nếu kịch bản đã có dòng Title: ... hoặc tự lấy từ câu đầu" />
      </div>
    </div>

    <!-- CHẾ ĐỘ 5: TAB TEST NHANH (CHẤT LƯỢNG VIDEO TỐI THIỂU 480P SIÊU TỐC) -->
    <div id="paneTest" style="display:none;">
      <div style="margin-bottom:12px;padding:9px 11px;border-radius:9px;background:rgba(14,165,233,0.1);border:1px solid rgba(56,189,248,0.3);font-size:11.5px;color:#bae6fd;line-height:1.45;">
        ⚡ <strong>Chế độ Test Tối Thiểu (480p • 1 Phần Song Ngữ):</strong> Bỏ qua vẽ ảnh AI, bỏ qua 4K 3 phần & bản dọc/TikTok để xuất nhanh 1 video 480p (kèm nhãn <code>[cảm xúc]</code> trên màn hình) giúp bạn kiểm tra giọng đọc & kịch bản chỉ trong vài giây!
      </div>
      <div class="field">
        <div class="field-label">
          <span>Kịch bản cần Test nhanh</span>
          <span class="field-hint">Dán kịch bản hoặc bấm nút nạp mẫu Test</span>
        </div>
        <textarea id="testScriptInput" rows="6" oninput="onCustomScriptChange('testScriptInput','testScriptAutoBadge')" placeholder="Dán kịch bản cần test giọng vào đây (hoặc để trống nếu muốn AI tự viết 4 câu ngắn theo chủ đề bên dưới)..."></textarea>
        <div class="chips">
          <button type="button" class="chip" onclick="fillTestSample('emotions')">🎭 Test 4 sắc thái (Shy / Sad / Angry / Surprised)</button>
          <button type="button" class="chip" onclick="fillTestSample('copy_script')">📋 Lấy từ tab Kịch bản sẵn</button>
          <button type="button" class="chip" onclick="document.getElementById('testScriptInput').value='';onCustomScriptChange('testScriptInput','testScriptAutoBadge');">🗑️ Xoá trắng</button>
        </div>
        <div id="testScriptAutoBadge" style="display:none;margin-top:8px;padding:8px 11px;border-radius:8px;background:rgba(16,185,129,0.12);border:1px solid rgba(16,185,129,0.35);color:#34d399;font-size:11.5px;font-weight:600;"></div>
      </div>
      <div class="field" style="margin-bottom:0;">
        <div class="field-label">
          <span>Chủ đề Test (nếu không dán kịch bản)</span>
          <span class="field-hint">Mặc định tạo 4 câu ngắn</span>
        </div>
        <input type="text" id="testTopicInput" value="TEST GIỌNG ĐỌC CẢM XÚC" placeholder="Nhập chủ đề ngắn để test..." />
      </div>
    </div>

    <hr style="border:none;border-top:1px solid var(--border);margin:14px 0;">

    <!-- CÀI ĐẶT CHUNG GỌN GÀNG (ẨN KHI Ở TAB KỊCH BẢN SẴN / TEST NHANH) -->
    <div class="grid-2 field" id="boxCommonSettings">
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
        <div class="field-label"><span>Thời lượng (2 loại)</span></div>
        <select id="durationSelect">
          <option value="10" selected>8 - 10 câu (Ngắn)</option>
          <option value="22">18 - 25 câu (Vừa)</option>
        </select>
      </div>
    </div>

    <!-- THANH TÓM TẮT HỘI THOẠI & CẶP GIỌNG (BẤM ĐỂ MỞ TAB HỘI THOẠI) -->
    <div class="dialogue-summary-bar" onclick="setMode('dialogue')" title="Bấm để mở Tab Hội thoại & đổi cặp giọng">
      <div style="font-size:12.5px;">
        <span style="color:var(--muted);font-weight:600;">💬 Giọng đọc:</span>
        <strong id="dialogueSummaryText" style="color:#f1f5f9;margin-left:4px;">Đối thoại (Nam – Nữ) • Aoede & Puck</strong>
      </div>
      <span style="font-size:11.5px;color:#38bdf8;font-weight:700;">Đổi giọng ➔</span>
    </div>

    <!-- TUỲ CHỌN MỞ RỘNG (ẨN GỌN MẶC ĐỊNH) -->
    <details class="adv-drawer">
      <summary>⚙️ Tuỳ chọn nâng cao (Phong cách, API Key...)</summary>
      <div class="field">
        <div class="field-label"><span>Mức độ nội dung</span></div>
        <select id="sensitivitySelect">
          <option value="normal" selected>🙂 Đời thường tự nhiên</option>
          <option value="sensitive_ok">🌶️ Drama / Nhạy cảm</option>
          <option value="polite">😇 Lịch sự / Học đường</option>
        </select>
      </div>
      <div class="field" style="margin-bottom:0;">
        <div class="field-label">
          <span>🔑 Gemini API Key</span>
          <span class="field-hint">Tự động lưu vào file .env</span>
        </div>
        <input type="password" id="geminiKeyInput" placeholder="Dán mã AIzaSy... (nếu có) để bật giọng Gemini Live & AI tốc độ cao" />
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
let isCreatingVideo = false;

const VOICE_PAIRS = {
  male_female: [
    { value: "gemini:Aoede|gemini:Puck", label: "Cặp 1: Aoede (Nữ truyền cảm) & Puck (Nam trẻ trung)" },
    { value: "gemini:Kore|gemini:Fenrir", label: "Cặp 2: Kore (Nữ trầm ấm) & Fenrir (Nam điện ảnh)" },
    { value: "gemini:Leda|gemini:Charon", label: "Cặp 3: Leda (Nữ năng động) & Charon (Nam điềm đạm)" },
    { value: "gemini:Despina|gemini:Enceladus", label: "Cặp 4: Despina (Nữ kịch tính) & Enceladus (Nam lãng mạn)" },
    { value: "gemini:Callirrhoe|gemini:Orus", label: "Cặp 5: Callirrhoe (Nữ dịu dàng) & Orus (Nam dứt khoát)" },
    { value: "gemini:Zephyr|gemini:Iapetus", label: "Cặp 6: Zephyr (Nữ hiện đại) & Iapetus (Nam thân thiện)" }
  ],
  male_male: [
    { value: "gemini:Puck|gemini:Fenrir", label: "Cặp 1: Puck (Nam trẻ trung) & Fenrir (Nam trầm điện ảnh)" },
    { value: "gemini:Charon|gemini:Orus", label: "Cặp 2: Charon (Nam điềm đạm) & Orus (Nam dứt khoát)" },
    { value: "gemini:Enceladus|gemini:Iapetus", label: "Cặp 3: Enceladus (Nam ấm áp) & Iapetus (Nam thân thiện)" },
    { value: "gemini:Umbriel|gemini:Achird", label: "Cặp 4: Umbriel (Nam thư thái) & Achird (Nam năng động)" },
    { value: "gemini:Puck|gemini:Algenib", label: "Cặp 5: Puck (Nam tự nhiên) & Algenib (Nam trầm sâu)" }
  ],
  female_female: [
    { value: "gemini:Aoede|gemini:Leda", label: "Cặp 1: Aoede (Nữ truyền cảm) & Leda (Nữ trẻ trung)" },
    { value: "gemini:Kore|gemini:Despina", label: "Cặp 2: Kore (Nữ trầm ấm) & Despina (Nữ kịch tính)" },
    { value: "gemini:Callirrhoe|gemini:Zephyr", label: "Cặp 3: Callirrhoe (Nữ dịu dàng) & Zephyr (Nữ hiện đại)" },
    { value: "gemini:Autonoe|gemini:Sulafat", label: "Cặp 4: Autonoe (Nữ rõ chữ) & Sulafat (Nữ kể chuyện)" },
    { value: "gemini:Aoede|gemini:Achernar", label: "Cặp 5: Aoede (Nữ tự nhiên) & Achernar (Nữ sâu sắc)" }
  ]
};

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
  document.getElementById('btnModeDialogue').classList.toggle('active', m === 'dialogue');
  document.getElementById('btnModeScript').classList.toggle('active', m === 'script');
  document.getElementById('btnModeTest').classList.toggle('active', m === 'test');

  document.getElementById('paneDirect').style.display = (m === 'direct') ? 'block' : 'none';
  document.getElementById('paneHot').style.display = (m === 'hot') ? 'block' : 'none';
  document.getElementById('paneDialogue').style.display = (m === 'dialogue') ? 'block' : 'none';
  document.getElementById('paneScript').style.display = (m === 'script') ? 'block' : 'none';
  document.getElementById('paneTest').style.display = (m === 'test') ? 'block' : 'none';

  const commonBox = document.getElementById('boxCommonSettings');
  if (commonBox) {
    commonBox.style.display = (m === 'script' || m === 'test') ? 'none' : 'grid';
  }

  const createBtn = document.getElementById('createBtn');
  if (m === 'hot') {
    createBtn.innerHTML = '🔍 Quét 5 Chủ Đề Đang Hot Trên Mạng';
    if (hotCandidates.length === 0 && !isScanningHot) {
      scanHotTopics();
    }
  } else if (m === 'script') {
    createBtn.innerHTML = '📋 Tạo Video Từ Kịch Bản Sẵn';
  } else if (m === 'test') {
    createBtn.innerHTML = '⚡ Tạo Video Test Tối Thiểu (480p Siêu Tốc)';
  } else {
    createBtn.innerHTML = '🚀 Tạo Video Ngay';
  }
}

function setPodcastMode(pmode) {
  document.getElementById('podcastModeSelect').value = pmode;
  document.getElementById('btnSubDialogue').classList.toggle('active', pmode === 'dialogue');
  document.getElementById('btnSubMonologue').classList.toggle('active', pmode === 'monologue');
  document.getElementById('boxPairVoiceConfig').style.display = (pmode === 'dialogue') ? 'block' : 'none';
  document.getElementById('boxMonoVoiceConfig').style.display = (pmode === 'monologue') ? 'block' : 'none';
  updateDialogueSummary();
}

function setPairType(ptype) {
  document.getElementById('pairTypeSelect').value = ptype;
  document.getElementById('btnPairMF').classList.toggle('active', ptype === 'male_female');
  document.getElementById('btnPairMM').classList.toggle('active', ptype === 'male_male');
  document.getElementById('btnPairFF').classList.toggle('active', ptype === 'female_female');

  const hintEl = document.getElementById('pairSpeakerHint');
  if (ptype === 'male_male') hintEl.textContent = '2 Giọng Nam (Tom & Jack)';
  else if (ptype === 'female_female') hintEl.textContent = '2 Giọng Nữ (Anna & Mia)';
  else hintEl.textContent = 'Nam & Nữ (Anna & Tom)';

  const sel = document.getElementById('voicePairSelect');
  const pairs = VOICE_PAIRS[ptype] || VOICE_PAIRS.male_female;
  sel.innerHTML = pairs.map((p, idx) => `<option value="${p.value}" ${idx === 0 ? 'selected' : ''}>${p.label}</option>`).join('');
  updateDialogueSummary();
}

function getSelectedVoiceConfig() {
  const podcast_mode = document.getElementById('podcastModeSelect').value || 'dialogue';
  const pair_type = document.getElementById('pairTypeSelect').value || 'male_female';
  if (podcast_mode === 'monologue') {
    const monoVoice = document.getElementById('voiceMonoSelect').value || 'gemini:Aoede';
    return { podcast_mode, pair_type, voice: monoVoice, voice_male: monoVoice };
  }
  const rawPair = document.getElementById('voicePairSelect').value || 'gemini:Aoede|gemini:Puck';
  const parts = rawPair.split('|');
  return {
    podcast_mode,
    pair_type,
    voice: parts[0] || 'gemini:Aoede',
    voice_male: parts[1] || parts[0] || 'gemini:Puck'
  };
}

function updateDialogueSummary() {
  const cfg = getSelectedVoiceConfig();
  const sumEl = document.getElementById('dialogueSummaryText');
  if (!sumEl) return;
  if (cfg.podcast_mode === 'monologue') {
    const vName = cfg.voice.replace('gemini:', '');
    sumEl.textContent = `Độc thoại (1 người) • Giọng ${vName}`;
  } else {
    const typeLabel = cfg.pair_type === 'male_male' ? 'Nam – Nam' : (cfg.pair_type === 'female_female' ? 'Nữ – Nữ' : 'Nam – Nữ');
    const v1 = cfg.voice.replace('gemini:', '');
    const v2 = cfg.voice_male.replace('gemini:', '');
    sumEl.textContent = `Đối thoại (${typeLabel}) • ${v1} & ${v2}`;
  }
}

// Khởi tạo danh sách cặp giọng mặc định
setPairType('male_female');

function pickSample(topic, desc) {
  document.getElementById('topicInput').value = topic;
  document.getElementById('descInput').value = desc || '';
}

function fillSampleScript(type) {
  const el = document.getElementById('customScriptInput');
  if (type === 'monologue') {
    el.value = [
      "Title: MẬP MỜ RỒI BỊ GHOST",
      "[sad:60%]: For four months, we walked home together holding hands every evening. | Suốt bốn tháng trời, tối nào tụi mình cũng nắm tay nhau đi bộ về nhà.",
      "[happy+anxious:60%]: Last night, I finally told him that I wanted to be his girlfriend. | Tối hôm qua, cuối cùng mình cũng lấy hết can đảm nói rằng muốn làm bạn gái anh ấy.",
      "[surprised:100%]: This morning, his account was gone and my number was blocked. | Sáng nay tỉnh dậy, tài khoản của anh ấy đã biến mất và số của mình cũng bị chặn.",
      "[sad+angry:60%]: I was never his love, just someone to fill his lonely nights. | Hoá ra mình chưa bao giờ là tình yêu, chỉ là người lấp đầy những đêm cô đơn của anh ta."
    ].join('\n');
  } else {
    el.value = [
      "Title: CHỈ LÀ LỐP DỰ PHÒNG",
      "Anna [shy:60%]: Tom, why do you only text me late at night when you feel lonely? | Tom này, sao anh chỉ nhắn tin cho em vào đêm muộn mỗi khi anh thấy cô đơn?",
      "Tom [anxious:60%]: That is not true, I am just really busy with work during the day. | Không phải vậy đâu, chỉ là ban ngày anh bận việc quá thôi mà.",
      "Anna [surprised:100%]: Then why did I see you holding Mia's hand at the coffee shop today? | Vậy tại sao hôm nay em lại thấy anh nắm tay Mia ở quán cà phê?",
      "Tom [shy:60%]: Anna, please listen, I did not want to hurt your feelings. | Anna, nghe anh giải thích đã, anh không muốn làm tổn thương cảm xúc của em.",
      "Anna [sad+angry:100%]: Stop lying! I am done being your backup plan! | Đừng nói dối nữa! Em chấm dứt cảnh làm lốp dự phòng của anh tại đây!"
    ].join('\n');
  }
  onCustomScriptChange('customScriptInput', 'customScriptAutoBadge');
}

function fillTestSample(type) {
  const testEl = document.getElementById('testScriptInput');
  if (type === 'copy_script') {
    const src = (document.getElementById('customScriptInput').value || '').trim();
    if (!src) {
      showToast('⚠️ Ô Kịch bản sẵn đang trống, đã nạp mẫu Test 4 câu!');
      fillTestSample('emotions');
      return;
    }
    testEl.value = src;
  } else {
    testEl.value = [
      "Title: TEST 4 SẮC THÁI GIỌNG ĐỌC",
      "Anna [shy:60%]: I think it's Jack from class B. He always smiles at me. | Tớ nghĩ là Jack lớp B. Cậu ấy lúc nào cũng cười với tớ.",
      "Tom [sad:60%]: Oh... I see. I hope he makes you really happy. | Ồ... tớ hiểu rồi. Tớ mong cậu ấy sẽ làm cậu thật hạnh phúc.",
      "Anna [surprised:100%]: Wait, Tom! Why are you holding that yellow note in your hand? | Khoan đã Tom! Sao cậu lại cầm tờ giấy nhắn màu vàng đó trên tay?",
      "Tom [happy+anxious:60%]: Because I was the one leaving notes in your umbrella every rainy day. | Vì chính tớ là người đã để giấy nhắn vào ô của cậu mỗi ngày mưa đấy."
    ].join('\n');
  }
  onCustomScriptChange('testScriptInput', 'testScriptAutoBadge');
}

function onCustomScriptChange(inputId = 'customScriptInput', badgeId = 'customScriptAutoBadge') {
  const inp = document.getElementById(inputId);
  const raw = (inp ? inp.value : '').trim();
  const badge = document.getElementById(badgeId);
  if (!raw) {
    if (badge) badge.style.display = 'none';
    return;
  }

  let detectedTitle = '';
  let explicitLevel = '';
  const speakers = new Set();
  let lineCount = 0;
  let totalWords = 0;

  for (const rawLine of raw.split('\n')) {
    const line = rawLine.trim();
    if (!line) continue;
    const mTitle = line.match(/^(?:title|tiêu\s*đề|chủ\s*đề|#)\s*[:\-]?\s*(.+)$/i);
    if (mTitle && lineCount === 0 && !line.includes('|')) {
      detectedTitle = mTitle[1].trim();
      continue;
    }
    const mLvl = line.match(/^(?:level|trình\s*độ)\s*[:\-]\s*(A1|A2|B1|B2|C1|C2)\b/i);
    if (mLvl && lineCount === 0) {
      explicitLevel = mLvl[1].toUpperCase();
      continue;
    }
    const enPart = line.split('|')[0].trim();
    const mSp = enPart.match(/^(Anna|Tom|Jack|Mia|Speaker\s*[AB12]|Nữ|Nam)\s*(?:[\[\(][^\]\)]+[\]\)])?\s*:\s*(.+)$/i);
    let cleanEn = enPart;
    if (mSp) {
      const spRaw = mSp[1].trim().toLowerCase();
      if (spRaw === 'jack') speakers.add('Jack');
      else if (spRaw === 'mia') speakers.add('Mia');
      else if (['tom', 'speaker b', 'speaker 2', 'nam'].includes(spRaw)) speakers.add('Tom');
      else speakers.add('Anna');
      cleanEn = mSp[2].trim();
    } else {
      cleanEn = enPart.replace(/^[\[\(][^\]\)]+[\]\)]\s*:?\s*/, '').trim();
    }
    if (cleanEn) {
      lineCount++;
      totalWords += cleanEn.split(/\s+/).filter(Boolean).length;
    }
  }

  if (detectedTitle) {
    if (inputId === 'customScriptInput') {
      const stEl = document.getElementById('scriptTitleInput');
      if (stEl && !stEl.value.trim()) stEl.value = detectedTitle;
    } else if (inputId === 'testScriptInput') {
      const ttEl = document.getElementById('testTopicInput');
      if (ttEl) ttEl.value = detectedTitle;
    }
  }

  let autoLevel = explicitLevel;
  if (!autoLevel && lineCount > 0) {
    const avg = totalWords / lineCount;
    if (avg <= 6.5) autoLevel = 'A1';
    else if (avg <= 8.5) autoLevel = 'A2';
    else if (avg <= 11.5) autoLevel = 'B1';
    else if (avg <= 14.0) autoLevel = 'B2';
    else if (avg <= 16.5) autoLevel = 'C1';
    else autoLevel = 'C2';
  }
  if (autoLevel) {
    document.getElementById('levelSelect').value = autoLevel;
  }

  let modeLabel = 'Đối thoại (Anna & Tom)';
  if (speakers.size <= 1) {
    setPodcastMode('monologue');
    modeLabel = 'Độc thoại (1 người)';
  } else {
    setPodcastMode('dialogue');
    if (speakers.has('Tom') && speakers.has('Jack')) {
      setPairType('male_male');
      modeLabel = 'Đối thoại Nam – Nam (Tom & Jack)';
    } else if (speakers.has('Anna') && speakers.has('Mia')) {
      setPairType('female_female');
      modeLabel = 'Đối thoại Nữ – Nữ (Anna & Mia)';
    } else {
      setPairType('male_female');
      modeLabel = 'Đối thoại Nam – Nữ (Anna & Tom)';
    }
  }

  if (badge && lineCount > 0) {
    badge.style.display = 'block';
    badge.textContent = `✅ Đã khớp từ kịch bản: ${lineCount} câu • ${modeLabel} • Trình độ ${autoLevel || 'B1'} — Bạn chỉ cần chọn Giọng đọc!`;
  }
}

async function handleCreateVideo() {
  if (currentMode === 'hot') {
    await scanHotTopics();
    return;
  }
  if (isCreatingVideo) return;

  const isFastTest = (currentMode === 'test');
  let custom_script = '';
  let topicRaw = '';
  let desc = '';

  if (currentMode === 'script') {
    custom_script = (document.getElementById('customScriptInput').value || '').trim();
    if (!custom_script) {
      alert('Vui lòng dán kịch bản có sẵn vào ô Kịch bản (hoặc bấm nút Mẫu Đối thoại / Độc thoại)!');
      return;
    }
    onCustomScriptChange('customScriptInput', 'customScriptAutoBadge');
    topicRaw = (document.getElementById('scriptTitleInput').value || '').trim() || 'KỊCH BẢN CÓ SẴN';
  } else if (currentMode === 'test') {
    custom_script = (document.getElementById('testScriptInput').value || '').trim();
    if (custom_script) {
      onCustomScriptChange('testScriptInput', 'testScriptAutoBadge');
    }
    topicRaw = (document.getElementById('testTopicInput').value || '').trim() || 'BẢN TEST NHANH';
  } else {
    custom_script = (document.getElementById('customScriptInput').value || '').trim();
    if (custom_script) {
      onCustomScriptChange('customScriptInput', 'customScriptAutoBadge');
    }
    topicRaw = (document.getElementById('topicInput').value || '').trim();
    desc = (document.getElementById('descInput').value || '').trim();
    if (!topicRaw && custom_script) {
      topicRaw = 'KỊCH BẢN CÓ SẴN';
    }
    if (!topicRaw) {
      setMode('direct');
      alert('Vui lòng nhập chủ đề bài nghe!');
      return;
    }
  }

  isCreatingVideo = true;

  const level = document.getElementById('levelSelect').value;
  const target_sentences = isFastTest ? 4 : (parseInt(document.getElementById('durationSelect').value, 10) || 10);
  const voiceCfg = getSelectedVoiceConfig();
  const podcast_mode = voiceCfg.podcast_mode;
  const pair_type = voiceCfg.pair_type;
  const voice = voiceCfg.voice;
  const voice_male = voiceCfg.voice_male;
  const sensitivity_mode = document.getElementById('sensitivitySelect').value;
  const keyEl = document.getElementById('geminiKeyInput');
  const gemini_api_key = (keyEl && keyEl.value.trim()) ? keyEl.value.trim() : DEFAULT_API_KEY;

  const btn = document.getElementById('createBtn');
  const statusEl = document.getElementById('createStatusMsg');
  btn.disabled = true;

  try {
    const lines = topicRaw.split('\n').map(l => l.trim()).filter(Boolean);
    if (lines.length === 1 && !custom_script && !lines[0].includes('|')) {
      statusEl.textContent = isFastTest
        ? `⚡ Đang tạo nhanh 4 câu test & dựng video 480p tối thiểu...`
        : `⏳ Đang khởi tạo kịch bản (${level}, ${target_sentences} câu, ${podcast_mode === 'monologue' ? 'Độc thoại' : 'Đối thoại'})...`;
      await fetch('/api/mode-ab/create', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          mode: 'A',
          topic: lines[0],
          description: desc,
          level,
          target_sentences,
          podcast_mode,
          pair_type,
          cta: 'Đăng ký kênh UNI PODCAST và bình luận ý kiến của bạn bên dưới',
          sensitivity_mode,
          voice,
          voice_male,
          rate: '+0%',
          channel_tag: 'UNI PODCAST',
          gemini_api_key,
          fast_test: isFastTest
        })
      });
    } else {
      statusEl.textContent = isFastTest
        ? '⚡ Đang thu âm & xuất video 480p tối thiểu để test...'
        : '⏳ Đã đưa vào hàng đợi sản xuất video từ kịch bản sẵn...';
      const items = lines.map((line, idx) => {
        const parts = line.split('|').map(x => x.trim());
        return {
          topic: parts[0] || 'CHỦ ĐỀ MỚI',
          level: parts[1] || level,
          category: isFastTest ? '⚡ TEST NHANH (480P)' : (parts[2] || 'KỊCH BẢN CÓ SẴN'),
          podcast_mode,
          pair_type,
          sensitivity_mode,
          custom_script: idx === 0 ? custom_script : '',
          fast_test: isFastTest
        };
      });
      await fetch('/api/batch', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          items,
          podcast_mode,
          pair_type,
          subtitle_style: isFastTest ? 'static' : 'teleprompter',
          sensitivity_mode,
          voice,
          voice_male,
          rate: '+0%',
          target_sentences,
          channel_tag: 'UNI PODCAST',
          gemini_api_key,
          fast_test: isFastTest
        })
      });
    }
    statusEl.textContent = '';
    showToast(isFastTest ? '⚡ Đã bắt đầu dựng Video Test 480p siêu tốc!' : '🚀 Đã bắt đầu dựng video! Xem tiến độ bên phải.');
    await refreshJobs();
  } catch (e) {
    statusEl.textContent = 'Lỗi: ' + e.message;
  } finally {
    isCreatingVideo = false;
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
              ✍️ Chọn chủ đề
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
  showToast('✍️ Đã điền chủ đề! Chỉnh cài đặt bên dưới rồi bấm "Tạo Video Ngay".');
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

function pickBarColor(pct) {
  if (pct >= 85) return '#ef4444';
  if (pct >= 65) return '#f59e0b';
  return '#0ea5e9';
}

async function refreshSystemStats() {
  try {
    const res = await fetch('/api/system-stats');
    if (!res.ok) return;
    const s = await res.json();
    const cpu = Math.min(100, Math.max(0, Number(s.cpu_percent || 0)));
    const ram = Math.min(100, Math.max(0, Number(s.ram_percent || 0)));
    const gpu = Math.min(100, Math.max(0, Number(s.gpu_percent || 0)));

    const valCpu = document.getElementById('valCpu');
    const barCpu = document.getElementById('barCpu');
    if (valCpu) valCpu.textContent = `${cpu.toFixed(0)}%`;
    if (barCpu) {
      barCpu.style.width = `${cpu}%`;
      barCpu.style.backgroundColor = pickBarColor(cpu);
    }

    const valRam = document.getElementById('valRam');
    const barRam = document.getElementById('barRam');
    const subRam = document.getElementById('subRam');
    if (valRam) valRam.textContent = `${ram.toFixed(0)}%`;
    if (barRam) {
      barRam.style.width = `${ram}%`;
      barRam.style.backgroundColor = pickBarColor(ram);
    }
    if (subRam && s.ram_total_gb > 0) {
      subRam.textContent = `(${s.ram_used_gb}/${s.ram_total_gb}GB)`;
    }

    const valGpu = document.getElementById('valGpu');
    const barGpu = document.getElementById('barGpu');
    const subGpu = document.getElementById('subGpu');
    if (valGpu) valGpu.textContent = `${gpu.toFixed(0)}%`;
    if (barGpu) {
      barGpu.style.width = `${gpu}%`;
      barGpu.style.backgroundColor = pickBarColor(gpu);
    }
    if (subGpu && s.gpu_mem_mb > 0) {
      subGpu.textContent = s.gpu_mem_mb >= 1024
        ? `(${(s.gpu_mem_mb / 1024).toFixed(1)}GB)`
        : `(${Math.round(s.gpu_mem_mb)}MB)`;
    }
  } catch (_) {}
}

setInterval(refreshJobs, 1500);
setInterval(refreshSystemStats, 2000);
refreshJobs();
refreshSystemStats();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return INDEX_HTML


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    import uvicorn

    port = int(os.environ.get("PORT", "8095"))
    uvicorn.run(app, host="127.0.0.1", port=port)

