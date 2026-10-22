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
            # ioreg reports a negative counter as an unsigned 64-bit wrap-around (~1.8e19); drop those
            mem_matches = [
                v
                for v in (
                    int(m.group(1))
                    for m in re.finditer(r'"inUse(?:Sys|Vid)MemoryBytes"\s*=\s*(\d+)', out)
                )
                if v < (1 << 48)
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


STATIC_DIR = BASE_DIR / "static"



@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    # Read per request so UI edits show up on refresh without restarting the server
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    import uvicorn

    port = int(os.environ.get("PORT", "8095"))
    uvicorn.run(app, host="127.0.0.1", port=port)

