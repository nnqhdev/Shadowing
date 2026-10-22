import logging
import os
import re
from pathlib import Path

import imageio_ffmpeg

logger = logging.getLogger(__name__)

_KEY_PARAM_RE = re.compile(r"(key=)[^&\s'\"]+")


def safe_err(exc: BaseException) -> str:
    """Format an exception for logs with API keys in URLs redacted."""
    return f"{type(exc).__name__}: {_KEY_PARAM_RE.sub(r'\1***', str(exc))}"


ROOT_DIR = Path(__file__).resolve().parent.parent
ASSETS_DIR = ROOT_DIR / "assets"


def _load_default_gemini_key() -> str:
    env_val = os.environ.get("GEMINI_API_KEY", "").strip()
    if env_val:
        return env_val
    env_file = ROOT_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip().startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
SAMPLE_RATE = 24000
DEFAULT_GEMINI_KEY = _load_default_gemini_key()
