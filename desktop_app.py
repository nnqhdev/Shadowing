import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

from PIL import Image, ImageTk

from generator import DEFAULT_GEMINI_KEY

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "outputs"
API_BASE = "http://127.0.0.1:8095"
DEFAULT_API_KEY = DEFAULT_GEMINI_KEY

FEMALE_VOICES = [
    ("Aoede (Tự nhiên, truyền cảm)", "gemini:Aoede"),
    ("Kore (Trầm ấm, sâu lắng)", "gemini:Kore"),
    ("Leda (Trẻ trung, ngọt ngào)", "gemini:Leda"),
    ("Zephyr (Hiện đại, sáng rõ)", "gemini:Zephyr"),
    ("Callirrhoe (Dịu dàng, thủ thỉ)", "gemini:Callirrhoe"),
    ("Despina (Mượt mà, kịch tính)", "gemini:Despina"),
    ("Autonoe (Rõ chữ, điềm tĩnh)", "gemini:Autonoe"),
    ("Achernar (Nhẹ nhàng, sâu sắc)", "gemini:Achernar"),
    ("Sulafat (Ấm áp, kể chuyện)", "gemini:Sulafat"),
]

MALE_VOICES = [
    ("Puck (Tự nhiên, trẻ trung)", "gemini:Puck"),
    ("Fenrir (Trầm khàn điện ảnh)", "gemini:Fenrir"),
    ("Charon (Trầm ấm, điềm đạm)", "gemini:Charon"),
    ("Enceladus (Trầm nhẹ, lãng mạn)", "gemini:Enceladus"),
    ("Orus (Rõ ràng, dứt khoát)", "gemini:Orus"),
    ("Iapetus (Thân thiện, gần gũi)", "gemini:Iapetus"),
    ("Umbriel (Thư thái, mượt mà)", "gemini:Umbriel"),
    ("Achird (Trẻ trung, năng động)", "gemini:Achird"),
    ("Algenib (Trầm sâu, kịch tính)", "gemini:Algenib"),
]

DURATIONS = [
    ("8 - 10 câu (Ngắn)", 10),
    ("18 - 25 câu (Vừa)", 22),
]

PAIR_TYPES = [
    ("👫 Nam – Nữ (Anna & Tom)", "male_female"),
    ("👬 Nam – Nam (Tom & Jack)", "male_male"),
    ("👭 Nữ – Nữ (Anna & Mia)", "female_female"),
]

VOICE_PAIRS_BY_TYPE = {
    "male_female": [
        ("Cặp 1: Aoede (Nữ truyền cảm) & Puck (Nam trẻ trung)", "gemini:Aoede|gemini:Puck"),
        ("Cặp 2: Kore (Nữ trầm ấm) & Fenrir (Nam điện ảnh)", "gemini:Kore|gemini:Fenrir"),
        ("Cặp 3: Leda (Nữ năng động) & Charon (Nam điềm đạm)", "gemini:Leda|gemini:Charon"),
        ("Cặp 4: Despina (Nữ kịch tính) & Enceladus (Nam lãng mạn)", "gemini:Despina|gemini:Enceladus"),
        ("Cặp 5: Callirrhoe (Nữ dịu dàng) & Orus (Nam dứt khoát)", "gemini:Callirrhoe|gemini:Orus"),
        ("Cặp 6: Zephyr (Nữ hiện đại) & Iapetus (Nam thân thiện)", "gemini:Zephyr|gemini:Iapetus"),
    ],
    "male_male": [
        ("Cặp 1: Puck (Nam trẻ trung) & Fenrir (Nam điện ảnh)", "gemini:Puck|gemini:Fenrir"),
        ("Cặp 2: Charon (Nam điềm đạm) & Orus (Nam dứt khoát)", "gemini:Charon|gemini:Orus"),
        ("Cặp 3: Enceladus (Nam ấm áp) & Iapetus (Nam thân thiện)", "gemini:Enceladus|gemini:Iapetus"),
        ("Cặp 4: Umbriel (Nam thư thái) & Achird (Nam năng động)", "gemini:Umbriel|gemini:Achird"),
        ("Cặp 5: Puck (Nam tự nhiên) & Algenib (Nam trầm sâu)", "gemini:Puck|gemini:Algenib"),
    ],
    "female_female": [
        ("Cặp 1: Aoede (Nữ truyền cảm) & Leda (Nữ trẻ trung)", "gemini:Aoede|gemini:Leda"),
        ("Cặp 2: Kore (Nữ trầm ấm) & Despina (Nữ kịch tính)", "gemini:Kore|gemini:Despina"),
        ("Cặp 3: Callirrhoe (Nữ dịu dàng) & Zephyr (Nữ hiện đại)", "gemini:Callirrhoe|gemini:Zephyr"),
        ("Cặp 4: Autonoe (Nữ rõ chữ) & Sulafat (Nữ kể chuyện)", "gemini:Autonoe|gemini:Sulafat"),
        ("Cặp 5: Aoede (Nữ tự nhiên) & Achernar (Nữ sâu sắc)", "gemini:Aoede|gemini:Achernar"),
    ],
}

MONO_VOICES = [
    ("♀️ Aoede (Nữ — Truyền cảm)", "gemini:Aoede"),
    ("♂️ Puck (Nam — Trẻ trung)", "gemini:Puck"),
    ("♀️ Kore (Nữ — Trầm ấm)", "gemini:Kore"),
    ("♂️ Fenrir (Nam — Trầm điện ảnh)", "gemini:Fenrir"),
    ("♀️ Leda (Nữ — Hiện đại)", "gemini:Leda"),
    ("♂️ Charon (Nam — Điềm đạm)", "gemini:Charon"),
]

LEVELS = [
    ("A1 — Sơ cấp", "A1"),
    ("A2 — Căn bản", "A2"),
    ("B1 — Trung cấp (Mặc định)", "B1"),
    ("B2 — Khá giỏi", "B2"),
    ("C1 — Cao cấp", "C1"),
    ("C2 — Bản xứ", "C2"),
]

SENSITIVITIES = [
    ("🙂 Đời thường tự nhiên", "normal"),
    ("🌶️ Drama / Nhạy cảm", "sensitive_ok"),
    ("😇 Lịch sự / Học đường", "polite"),
]

PODCAST_MODES = [
    ("👫 2 Người (Anna & Tom)", "dialogue"),
    ("🎙️ 1 Người (Độc thoại)", "monologue"),
]


_BACKEND_THREAD_LOCK = threading.Lock()
_BACKEND_SERVER_STARTING = False


def ensure_backend_running() -> None:
    """Start FastAPI backend as an independent background process if not already listening on 8095."""
    global _BACKEND_SERVER_STARTING
    try:
        with urllib.request.urlopen(f"{API_BASE}/api/jobs", timeout=1.2) as r:
            if r.status == 200:
                return
    except Exception:
        pass

    with _BACKEND_THREAD_LOCK:
        if _BACKEND_SERVER_STARTING:
            return
        _BACKEND_SERVER_STARTING = True

    try:
        OUTPUT_DIR.mkdir(exist_ok=True)
        log_file = open(OUTPUT_DIR / "backend.log", "ab")
        popen_kwargs = {
            "cwd": str(BASE_DIR),
            "stdout": log_file,
            "stderr": subprocess.STDOUT,
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        else:
            popen_kwargs["start_new_session"] = True

        subprocess.Popen([sys.executable, "-X", "utf8", str(BASE_DIR / "app.py")], **popen_kwargs)

        for _ in range(25):
            time.sleep(0.25)
            try:
                with urllib.request.urlopen(f"{API_BASE}/api/jobs", timeout=1.0) as r:
                    if r.status == 200:
                        return
            except Exception:
                pass
    finally:
        _BACKEND_SERVER_STARTING = False


def api_request(path: str, method: str = "GET", payload: dict | None = None):
    url = f"{API_BASE}{path}"
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=180) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


class UniPodcastDesktopApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("UNI Podcast Studio — Bản Desktop")
        self.geometry("1220x780")
        self.minsize(1060, 680)
        self.configure(bg="#0f1117")

        # Colors
        self.c_bg = "#0f1117"
        self.c_panel = "#161922"
        self.c_card = "#1c202c"
        self.c_input = "#10131a"
        self.c_text = "#f1f5f9"
        self.c_muted = "#94a3b8"
        self.c_accent = "#0ea5e9"
        self.c_emerald = "#10b981"
        self.c_amber = "#f59e0b"

        self.jobs_data: list[dict] = []
        self.selected_job_id: str | None = None
        self.thumb_photo = None
        self.hot_candidates: list[dict] = []
        self.adv_visible = False
        self.current_mode = "direct"
        self.is_scanning_hot = False

        self._setup_styles()
        self._build_ui()
        # Bring window to front briefly on launch
        self.lift()
        self.attributes("-topmost", True)
        self.after(400, lambda: self.attributes("-topmost", False))
        threading.Thread(target=ensure_backend_running, daemon=True).start()
        self.after(500, self._poll_jobs)

    def _setup_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "TCombobox",
            fieldbackground=self.c_input,
            background=self.c_card,
            foreground=self.c_text,
            arrowcolor=self.c_text,
            borderwidth=1,
            padding=6,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", self.c_input)],
            selectbackground=[("readonly", self.c_input)],
            selectforeground=[("readonly", self.c_text)],
        )
        style.configure(
            "Studio.Horizontal.TProgressbar",
            troughcolor=self.c_input,
            background=self.c_accent,
            bordercolor=self.c_input,
            lightcolor=self.c_accent,
            darkcolor=self.c_accent,
            thickness=10,
        )

    def _build_ui(self):
        # Top Header Bar
        header = tk.Frame(self, bg=self.c_bg, padx=22, pady=14)
        header.pack(fill="x")

        title_box = tk.Frame(header, bg=self.c_bg)
        title_box.pack(side="left")

        badge = tk.Label(
            title_box,
            text="🎧",
            font=("Segoe UI Emoji", 18),
            bg="#0ea5e9",
            fg="#ffffff",
            width=2,
            padx=4,
            pady=2,
        )
        badge.pack(side="left", padx=(0, 12))

        t_col = tk.Frame(title_box, bg=self.c_bg)
        t_col.pack(side="left")
        tk.Label(
            t_col,
            text="UNI Podcast Studio",
            font=("Segoe UI", 15, "bold"),
            bg=self.c_bg,
            fg=self.c_text,
        ).pack(anchor="w")
        tk.Label(
            t_col,
            text="Ứng dụng Desktop tạo Video Luyện Nghe 3 Phần & Thumbnail tự động",
            font=("Segoe UI", 9),
            bg=self.c_bg,
            fg=self.c_muted,
        ).pack(anchor="w")

        hdr_btns = tk.Frame(header, bg=self.c_bg)
        hdr_btns.pack(side="right")

        self._make_btn(
            hdr_btns,
            "📂 Mở thư mục chứa tất cả Video",
            lambda: self._open_folder(OUTPUT_DIR),
            bg=self.c_card,
        ).pack(side="right", padx=(8, 0))

        self._make_btn(
            hdr_btns,
            "🌐 Mở cửa sổ Web App",
            self._open_web_app_window,
            bg=self.c_card,
        ).pack(side="right")

        self.lbl_sys_stats = tk.Label(
            hdr_btns,
            text="CPU: --%  |  RAM: --%  |  GPU: --%",
            font=("Consolas", 9, "bold"),
            bg=self.c_panel,
            fg="#38bdf8",
            padx=12,
            pady=6,
            highlightthickness=1,
            highlightbackground="#262b38",
        )
        self.lbl_sys_stats.pack(side="right", padx=(0, 10))

        sep = tk.Frame(self, bg="#262b38", height=1)
        sep.pack(fill="x", padx=22)

        # Main 2-column Workspace
        main = tk.Frame(self, bg=self.c_bg, padx=22, pady=16)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=0, minsize=430)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        # ================= LEFT PANEL: CREATOR =================
        left = tk.Frame(main, bg=self.c_panel, padx=18, pady=16, highlightthickness=1, highlightbackground="#262b38")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 16))

        # Mode Switcher (Direct vs Hot AI vs Dialogue)
        mode_bar = tk.Frame(left, bg=self.c_input, padx=4, pady=4)
        mode_bar.pack(fill="x", pady=(0, 12))
        mode_bar.columnconfigure(0, weight=1)
        mode_bar.columnconfigure(1, weight=1)
        mode_bar.columnconfigure(2, weight=1)

        self.btn_mode_direct = tk.Button(
            mode_bar,
            text="✍️ Nhập chủ đề",
            font=("Segoe UI", 9, "bold"),
            bg=self.c_card,
            fg="#ffffff",
            activebackground=self.c_card,
            activeforeground="#ffffff",
            bd=0,
            pady=7,
            cursor="hand2",
            command=lambda: self._switch_mode("direct"),
        )
        self.btn_mode_direct.grid(row=0, column=0, sticky="ew", padx=(0, 2))

        self.btn_mode_hot = tk.Button(
            mode_bar,
            text="🔥 Chủ đề Hot",
            font=("Segoe UI", 9, "bold"),
            bg=self.c_input,
            fg=self.c_muted,
            activebackground=self.c_card,
            activeforeground="#ffffff",
            bd=0,
            pady=7,
            cursor="hand2",
            command=lambda: self._switch_mode("hot"),
        )
        self.btn_mode_hot.grid(row=0, column=1, sticky="ew", padx=2)

        self.btn_mode_dialogue = tk.Button(
            mode_bar,
            text="💬 Hội thoại",
            font=("Segoe UI", 9, "bold"),
            bg=self.c_input,
            fg=self.c_muted,
            activebackground=self.c_card,
            activeforeground="#ffffff",
            bd=0,
            pady=7,
            cursor="hand2",
            command=lambda: self._switch_mode("dialogue"),
        )
        self.btn_mode_dialogue.grid(row=0, column=2, sticky="ew", padx=(2, 0))

        self.mode_container = tk.Frame(left, bg=self.c_panel)
        self.mode_container.pack(fill="x")

        # --- Direct Mode Frame ---
        self.frame_direct = tk.Frame(self.mode_container, bg=self.c_panel)
        self.frame_direct.pack(fill="x")

        self._label(self.frame_direct, "Chủ đề bài nghe (Mỗi dòng 1 video):").pack(anchor="w")
        self.txt_topic = tk.Text(
            self.frame_direct,
            height=2,
            font=("Segoe UI", 10),
            bg=self.c_input,
            fg=self.c_text,
            insertbackground=self.c_text,
            relief="flat",
            padx=10,
            pady=8,
            highlightthickness=1,
            highlightbackground="#2c3344",
            highlightcolor=self.c_accent,
        )
        self.txt_topic.pack(fill="x", pady=(4, 6))
        self.txt_topic.insert("1.0", "CHỈ LÀ LỐP DỰ PHÒNG")

        # Quick sample chips
        chips_bar = tk.Frame(self.frame_direct, bg=self.c_panel)
        chips_bar.pack(fill="x", pady=(0, 10))
        tk.Label(chips_bar, text="Gợi ý Gen Z:", font=("Segoe UI", 8), bg=self.c_panel, fg=self.c_muted).pack(side="left", padx=(0, 6))
        for label_txt, sample_t, sample_d in [
            ("🛞 Lốp dự phòng", "CHỈ LÀ LỐP DỰ PHÒNG", "Suốt 6 tháng ngày nào cũng nhắn tin ngọt ngào lúc 2h sáng nhưng khi hỏi danh phận thì chối, đến hôm nay phát hiện mình chỉ là lốp dự phòng."),
            ("🍵 Trà xanh cắm sừng", "BỊ TRÀ XANH CẮM SỪNG", "Cô bạn thân luôn miệng khuyên chia tay người yêu nhưng lại âm thầm nhắn tin thả thính và cắm sừng ngay sau lưng."),
            ("💅 Đụng độ BB3", "ĐỤNG ĐỘ BB3", "Người yêu báo bận tăng ca nhưng lại bị bắt quả tang đang đi ăn tối lãng mạn cùng bé ba (BB3)."),
        ]:
            b = tk.Button(
                chips_bar,
                text=label_txt,
                font=("Segoe UI", 8, "bold"),
                bg=self.c_card,
                fg="#cbd5e1",
                bd=0,
                padx=8,
                pady=3,
                cursor="hand2",
                command=lambda t=sample_t, d=sample_d: self._set_sample(t, d),
            )
            b.pack(side="left", padx=2)

        self._label(self.frame_direct, "Tình huống chi tiết (Không bắt buộc — Để trống AI tự viết):").pack(anchor="w")
        self.txt_desc = tk.Text(
            self.frame_direct,
            height=2,
            font=("Segoe UI", 9),
            bg=self.c_input,
            fg=self.c_text,
            insertbackground=self.c_text,
            relief="flat",
            padx=10,
            pady=6,
            highlightthickness=1,
            highlightbackground="#2c3344",
            highlightcolor=self.c_accent,
        )
        self.txt_desc.pack(fill="x", pady=(4, 8))

        # --- Hot AI Mode Frame ---
        self.frame_hot = tk.Frame(self.mode_container, bg=self.c_panel)
        self._label(self.frame_hot, "Từ khoá vụ việc / Drama đang Hot (Nhấn Enter hoặc để trống):").pack(anchor="w")
        self.ent_hot_query = tk.Entry(
            self.frame_hot,
            font=("Segoe UI", 10),
            bg=self.c_input,
            fg=self.c_text,
            insertbackground=self.c_text,
            relief="flat",
            highlightthickness=1,
            highlightbackground="#2c3344",
            highlightcolor=self.c_accent,
        )
        self.ent_hot_query.pack(fill="x", pady=(4, 6), ipady=5)
        self.ent_hot_query.bind("<Return>", lambda _e: self._scan_hot_topics())

        self.btn_scan_hot = self._make_btn(
            self.frame_hot,
            "🔍 Quét Mới 5 Chủ Đề Đang Hot Trên Mạng",
            self._scan_hot_topics,
            bg="#0284c7",
            fg="#ffffff",
        )
        self.btn_scan_hot.pack(fill="x", pady=(0, 6))

        self.hot_list_frame = tk.Frame(self.frame_hot, bg=self.c_panel)
        self.hot_list_frame.pack(fill="x", pady=(0, 4))

        # --- Dialogue Tab Frame (Độc thoại / Đối thoại & Cặp giọng) ---
        self.frame_dialogue = tk.Frame(self.mode_container, bg=self.c_panel)
        self._label(self.frame_dialogue, "Chế độ kể chuyện (Độc thoại / Đối thoại):").pack(anchor="w", pady=(0, 3))
        self.cb_pmode = ttk.Combobox(
            self.frame_dialogue,
            values=[x[0] for x in PODCAST_MODES],
            state="readonly",
            font=("Segoe UI", 9),
        )
        self.cb_pmode.current(0)
        self.cb_pmode.pack(fill="x", pady=(0, 8))
        self.cb_pmode.bind("<<ComboboxSelected>>", lambda _e: self._on_dialogue_cfg_change())

        self.box_pair_cfg = tk.Frame(self.frame_dialogue, bg=self.c_panel)
        self.box_pair_cfg.pack(fill="x")
        self._label(self.box_pair_cfg, "Loại cặp dòng (3 loại: Nam–Nữ, Nam–Nam, Nữ–Nữ):").pack(anchor="w", pady=(0, 3))
        self.cb_pair_type = ttk.Combobox(
            self.box_pair_cfg,
            values=[x[0] for x in PAIR_TYPES],
            state="readonly",
            font=("Segoe UI", 9),
        )
        self.cb_pair_type.current(0)
        self.cb_pair_type.pack(fill="x", pady=(0, 8))
        self.cb_pair_type.bind("<<ComboboxSelected>>", lambda _e: self._on_pair_type_change())

        self._label(self.box_pair_cfg, "Chọn 1 cặp giọng đọc (Gồm sẵn 2 giọng):").pack(anchor="w", pady=(0, 3))
        self.cb_voice_pair = ttk.Combobox(
            self.box_pair_cfg,
            values=[x[0] for x in VOICE_PAIRS_BY_TYPE["male_female"]],
            state="readonly",
            font=("Segoe UI", 9),
        )
        self.cb_voice_pair.current(0)
        self.cb_voice_pair.pack(fill="x", pady=(0, 6))
        self.cb_voice_pair.bind("<<ComboboxSelected>>", lambda _e: self._on_dialogue_cfg_change())

        self.box_mono_cfg = tk.Frame(self.frame_dialogue, bg=self.c_panel)
        self._label(self.box_mono_cfg, "Chọn giọng đọc Độc thoại (1 người):").pack(anchor="w", pady=(0, 3))
        self.cb_voice_mono = ttk.Combobox(
            self.box_mono_cfg,
            values=[x[0] for x in MONO_VOICES],
            state="readonly",
            font=("Segoe UI", 9),
        )
        self.cb_voice_mono.current(0)
        self.cb_voice_mono.pack(fill="x", pady=(0, 6))
        self.cb_voice_mono.bind("<<ComboboxSelected>>", lambda _e: self._on_dialogue_cfg_change())

        # --- Shared Core Settings ---
        tk.Frame(left, bg="#262b38", height=1).pack(fill="x", pady=8)

        grid4 = tk.Frame(left, bg=self.c_panel)
        grid4.pack(fill="x")
        grid4.columnconfigure(0, weight=1)
        grid4.columnconfigure(1, weight=1)

        # Row 1: Level + Duration (2 loại: 8-10 và 18-25)
        f_lvl = tk.Frame(grid4, bg=self.c_panel)
        f_lvl.grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=4)
        self._label(f_lvl, "Trình độ (Level):").pack(anchor="w", pady=(0, 3))
        self.cb_level = ttk.Combobox(f_lvl, values=[x[0] for x in LEVELS], state="readonly", font=("Segoe UI", 9))
        self.cb_level.current(2)
        self.cb_level.pack(fill="x")

        f_dur = tk.Frame(grid4, bg=self.c_panel)
        f_dur.grid(row=0, column=1, sticky="ew", padx=(6, 0), pady=4)
        self._label(f_dur, "Thời lượng (2 loại):").pack(anchor="w", pady=(0, 3))
        self.cb_duration = ttk.Combobox(f_dur, values=[x[0] for x in DURATIONS], state="readonly", font=("Segoe UI", 9))
        self.cb_duration.current(0)
        self.cb_duration.pack(fill="x")

        self.btn_dlg_summary = tk.Button(
            left,
            text="💬 Hội thoại: Đối thoại (Nam – Nữ) • Aoede & Puck  [Đổi ➔]",
            font=("Segoe UI", 9, "bold"),
            bg=self.c_input,
            fg="#38bdf8",
            activebackground=self.c_card,
            activeforeground="#ffffff",
            bd=0,
            pady=7,
            cursor="hand2",
            command=lambda: self._switch_mode("dialogue"),
        )
        self.btn_dlg_summary.pack(fill="x", pady=(6, 2))

        # Collapsible Advanced Options
        self.btn_toggle_adv = tk.Button(
            left,
            text="▸ ⚙️ Tuỳ chọn nâng cao (Phong cách, Kịch bản có sẵn...)",
            font=("Segoe UI", 9, "bold"),
            bg=self.c_panel,
            fg=self.c_muted,
            activebackground=self.c_panel,
            activeforeground=self.c_text,
            bd=0,
            anchor="w",
            cursor="hand2",
            command=self._toggle_advanced,
        )
        self.btn_toggle_adv.pack(fill="x", pady=(8, 4))

        self.frame_adv = tk.Frame(left, bg=self.c_input, padx=10, pady=10, highlightthickness=1, highlightbackground="#262b38")
        adv_grid = tk.Frame(self.frame_adv, bg=self.c_input)
        adv_grid.pack(fill="x")
        adv_grid.columnconfigure(0, weight=1)

        f_sens = tk.Frame(adv_grid, bg=self.c_input)
        f_sens.grid(row=0, column=0, sticky="ew")
        tk.Label(f_sens, text="Mức độ nội dung:", font=("Segoe UI", 8, "bold"), bg=self.c_input, fg="#cbd5e1").pack(anchor="w", pady=(0, 2))
        self.cb_sens = ttk.Combobox(f_sens, values=[x[0] for x in SENSITIVITIES], state="readonly", font=("Segoe UI", 8))
        self.cb_sens.current(0)
        self.cb_sens.pack(fill="x")

        tk.Label(
            self.frame_adv,
            text="Tự dán lời thoại có sẵn (Để trống nếu muốn AI tự viết):",
            font=("Segoe UI", 8, "bold"),
            bg=self.c_input,
            fg="#cbd5e1",
        ).pack(anchor="w", pady=(8, 2))
        self.txt_custom_script = tk.Text(
            self.frame_adv,
            height=3,
            font=("Consolas", 9),
            bg=self.c_bg,
            fg=self.c_text,
            insertbackground=self.c_text,
            relief="flat",
            padx=6,
            pady=4,
        )
        self.txt_custom_script.pack(fill="x")

        # Primary Create Button
        self.btn_create = tk.Button(
            left,
            text="🚀 TẠO VIDEO NGAY",
            font=("Segoe UI", 11, "bold"),
            bg=self.c_accent,
            fg="#ffffff",
            activebackground="#0284c7",
            activeforeground="#ffffff",
            bd=0,
            pady=12,
            cursor="hand2",
            command=self._handle_create,
        )
        self.btn_create.pack(fill="x", pady=(12, 6))

        self.lbl_status = tk.Label(
            left,
            text="Sẵn sàng tạo video.",
            font=("Segoe UI", 9),
            bg=self.c_panel,
            fg=self.c_muted,
            wraplength=380,
        )
        self.lbl_status.pack(fill="x")

        # ================= RIGHT PANEL: VIDEO LIBRARY & PREVIEW =================
        right = tk.Frame(main, bg=self.c_panel, padx=18, pady=16, highlightthickness=1, highlightbackground="#262b38")
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        r_top = tk.Frame(right, bg=self.c_panel)
        r_top.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        tk.Label(
            r_top,
            text="🎬 Kho Video Đã Tạo",
            font=("Segoe UI", 12, "bold"),
            bg=self.c_panel,
            fg=self.c_text,
        ).pack(side="left")
        self.lbl_count = tk.Label(
            r_top,
            text="",
            font=("Segoe UI", 9),
            bg=self.c_panel,
            fg=self.c_muted,
        )
        self.lbl_count.pack(side="right")

        # Split Right into Top List + Bottom Selected Video Inspector
        r_body = tk.Frame(right, bg=self.c_panel)
        r_body.grid(row=1, column=0, sticky="nsew")
        r_body.columnconfigure(0, weight=1)
        r_body.rowconfigure(0, weight=1)
        r_body.rowconfigure(1, weight=0)

        # Listbox of videos
        list_wrap = tk.Frame(r_body, bg=self.c_input, highlightthickness=1, highlightbackground="#262b38")
        list_wrap.grid(row=0, column=0, sticky="nsew", pady=(0, 12))

        scrollbar = tk.Scrollbar(list_wrap, orient="vertical")
        self.job_listbox = tk.Listbox(
            list_wrap,
            font=("Segoe UI", 10),
            bg=self.c_input,
            fg=self.c_text,
            selectbackground="#0ea5e9",
            selectforeground="#ffffff",
            activestyle="none",
            bd=0,
            highlightthickness=0,
            yscrollcommand=scrollbar.set,
        )
        scrollbar.config(command=self.job_listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self.job_listbox.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        self.job_listbox.bind("<<ListboxSelect>>", self._on_select_job)

        # Selected Video Detail Card
        self.detail_card = tk.Frame(r_body, bg=self.c_card, padx=16, pady=14, highlightthickness=1, highlightbackground="#2c3344")
        self.detail_card.grid(row=1, column=0, sticky="ew")
        self.detail_card.columnconfigure(1, weight=1)

        # Thumbnail Preview Label
        self.lbl_thumb = tk.Label(
            self.detail_card,
            text="Chưa chọn video",
            font=("Segoe UI", 9),
            bg=self.c_input,
            fg=self.c_muted,
            width=36,
            height=9,
        )
        self.lbl_thumb.grid(row=0, column=0, rowspan=4, sticky="nw", padx=(0, 16))

        # Video Title & Progress
        self.lbl_detail_title = tk.Label(
            self.detail_card,
            text="Hãy chọn một video ở danh sách trên",
            font=("Segoe UI", 12, "bold"),
            bg=self.c_card,
            fg=self.c_text,
            anchor="w",
        )
        self.lbl_detail_title.grid(row=0, column=1, sticky="ew")

        self.lbl_detail_sub = tk.Label(
            self.detail_card,
            text="",
            font=("Segoe UI", 9),
            bg=self.c_card,
            fg=self.c_muted,
            anchor="w",
        )
        self.lbl_detail_sub.grid(row=1, column=1, sticky="ew", pady=(2, 6))

        self.progress_bar = ttk.Progressbar(
            self.detail_card,
            style="Studio.Horizontal.TProgressbar",
            orient="horizontal",
            mode="determinate",
        )
        self.progress_bar.grid(row=2, column=1, sticky="ew", pady=(0, 10))

        # Action Buttons for Selected Video
        act_wrap = tk.Frame(self.detail_card, bg=self.c_card)
        act_wrap.grid(row=3, column=1, sticky="ew")

        row_play = tk.Frame(act_wrap, bg=self.c_card)
        row_play.pack(fill="x", pady=(0, 6))

        self.btn_play_169 = self._make_btn(row_play, "▶️ Phát Video 16:9", lambda: self._play_selected("video_file"), bg="#0ea5e9", fg="#ffffff")
        self.btn_play_169.pack(side="left", padx=(0, 6))

        self.btn_play_916 = self._make_btn(row_play, "📱 Phát Dọc 9:16", lambda: self._play_selected("video_vertical_file"), bg="#0284c7", fg="#ffffff")
        self.btn_play_916.pack(side="left", padx=(0, 6))

        self.btn_play_tk = self._make_btn(row_play, "🔥 Phát TikTok", lambda: self._play_selected("tiktok_vertical_file"), bg="#d97706", fg="#ffffff")
        self.btn_play_tk.pack(side="left", padx=(0, 6))

        row_tools = tk.Frame(act_wrap, bg=self.c_card)
        row_tools.pack(fill="x")

        self.btn_open_dir = self._make_btn(row_tools, "📂 Mở Thư Mục Lấy File", self._open_selected_folder, bg="#1e293b")
        self.btn_open_dir.pack(side="left", padx=(0, 6))

        self.btn_copy_desc = self._make_btn(row_tools, "📋 Copy Mô Tả YouTube", self._copy_selected_desc, bg="#1e293b")
        self.btn_copy_desc.pack(side="left", padx=(0, 6))

        self.btn_view_script = self._make_btn(row_tools, "📄 Xem Lời Thoại", self._view_selected_script, bg="#1e293b")
        self.btn_view_script.pack(side="left", padx=(0, 6))

        self.btn_delete = self._make_btn(row_tools, "🗑️ Xoá", self._delete_selected_job, bg="#3f1d24", fg="#fca5a5")
        self.btn_delete.pack(side="right")

    def _label(self, parent, text: str) -> tk.Label:
        return tk.Label(parent, text=text, font=("Segoe UI", 9, "bold"), bg=parent["bg"], fg="#cbd5e1")

    def _make_btn(self, parent, text: str, cmd, bg="#1c202c", fg="#f1f5f9") -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            font=("Segoe UI", 9, "bold"),
            bg=bg,
            fg=fg,
            activebackground=bg,
            activeforeground=fg,
            bd=0,
            padx=11,
            pady=6,
            cursor="hand2",
            command=cmd,
        )

    def _switch_mode(self, mode: str):
        self.current_mode = mode
        self.btn_mode_direct.configure(bg=self.c_card if mode == "direct" else self.c_input, fg="#ffffff" if mode == "direct" else self.c_muted)
        self.btn_mode_hot.configure(bg=self.c_card if mode == "hot" else self.c_input, fg="#ffffff" if mode == "hot" else self.c_muted)
        self.btn_mode_dialogue.configure(bg=self.c_card if mode == "dialogue" else self.c_input, fg="#ffffff" if mode == "dialogue" else self.c_muted)

        self.frame_direct.pack_forget()
        self.frame_hot.pack_forget()
        self.frame_dialogue.pack_forget()

        if mode == "direct":
            self.frame_direct.pack(fill="x")
            self.btn_create.configure(text="🚀 TẠO VIDEO NGAY")
        elif mode == "dialogue":
            self.frame_dialogue.pack(fill="x")
            self.btn_create.configure(text="🚀 TẠO VIDEO NGAY")
        else:
            self.frame_hot.pack(fill="x")
            self.btn_create.configure(text="🔍 QUÉT 5 CHỦ ĐỀ ĐANG HOT TRÊN MẠNG")
            if not self.hot_candidates and not self.is_scanning_hot:
                self._scan_hot_topics()

    def _on_pair_type_change(self):
        ptype = PAIR_TYPES[self.cb_pair_type.current()][1]
        pairs = VOICE_PAIRS_BY_TYPE.get(ptype, VOICE_PAIRS_BY_TYPE["male_female"])
        self.cb_voice_pair.configure(values=[x[0] for x in pairs])
        self.cb_voice_pair.current(0)
        self._on_dialogue_cfg_change()

    def _get_selected_voice_cfg(self):
        pmode = PODCAST_MODES[self.cb_pmode.current()][1]
        ptype = PAIR_TYPES[self.cb_pair_type.current()][1]
        if pmode == "monologue":
            mv = MONO_VOICES[self.cb_voice_mono.current()][1]
            return pmode, ptype, mv, mv
        pairs = VOICE_PAIRS_BY_TYPE.get(ptype, VOICE_PAIRS_BY_TYPE["male_female"])
        idx = max(0, min(self.cb_voice_pair.current(), len(pairs) - 1))
        raw_pair = pairs[idx][1]
        parts = raw_pair.split("|")
        v1 = parts[0] if parts else "gemini:Aoede"
        v2 = parts[1] if len(parts) > 1 else v1
        return pmode, ptype, v1, v2

    def _on_dialogue_cfg_change(self):
        pmode, ptype, v1, v2 = self._get_selected_voice_cfg()
        if pmode == "monologue":
            self.box_pair_cfg.pack_forget()
            self.box_mono_cfg.pack(fill="x")
            v_lbl = v1.replace("gemini:", "")
            self.btn_dlg_summary.configure(text=f"💬 Hội thoại: Độc thoại (1 người) • Giọng {v_lbl}  [Đổi ➔]")
        else:
            self.box_mono_cfg.pack_forget()
            self.box_pair_cfg.pack(fill="x")
            t_lbl = "Nam – Nam" if ptype == "male_male" else ("Nữ – Nữ" if ptype == "female_female" else "Nam – Nữ")
            self.btn_dlg_summary.configure(
                text=f"💬 Hội thoại: Đối thoại ({t_lbl}) • {v1.replace('gemini:', '')} & {v2.replace('gemini:', '')}  [Đổi ➔]"
            )

    def _toggle_advanced(self):
        self.adv_visible = not self.adv_visible
        if self.adv_visible:
            self.btn_toggle_adv.configure(text="▾ ⚙️ Tuỳ chọn nâng cao (Phong cách, Kịch bản có sẵn...)")
            self.frame_adv.pack(fill="x", before=self.btn_create, pady=(0, 8))
        else:
            self.btn_toggle_adv.configure(text="▸ ⚙️ Tuỳ chọn nâng cao (Phong cách, Kịch bản có sẵn...)")
            self.frame_adv.pack_forget()

    def _set_sample(self, topic: str, desc: str):
        self.txt_topic.delete("1.0", "end")
        self.txt_topic.insert("1.0", topic)
        self.txt_desc.delete("1.0", "end")
        if desc:
            self.txt_desc.insert("1.0", desc)

    def _scan_hot_topics(self):
        if self.is_scanning_hot:
            return
        self.is_scanning_hot = True
        query = self.ent_hot_query.get().strip()
        level = LEVELS[self.cb_level.current()][1]
        sens = SENSITIVITIES[self.cb_sens.current()][1]
        self.btn_scan_hot.configure(state="disabled", text="⏳ AI đang quét xu hướng mạng xã hội...")
        if self.current_mode == "hot":
            self.btn_create.configure(state="disabled", text="⏳ ĐANG QUÉT 5 CHỦ ĐỀ HOT...")
        self.lbl_status.configure(text="🔍 AI đang săn 5 chủ đề & drama hấp dẫn nhất (khoảng 2-3 giây)...", fg=self.c_accent)

        for w in self.hot_list_frame.winfo_children():
            w.destroy()
        loading_lbl = tk.Label(
            self.hot_list_frame,
            text="⏳ AI đang tìm 5 chủ đề hấp dẫn nhất...",
            font=("Segoe UI", 9, "italic"),
            bg=self.c_input,
            fg=self.c_accent,
            pady=10,
        )
        loading_lbl.pack(fill="x", pady=4)

        def _worker():
            try:
                data = api_request(
                    "/api/mode-b/discover",
                    method="POST",
                    payload={
                        "level": level,
                        "topic": query,
                        "description": "",
                        "cta": "Đăng ký kênh UNI PODCAST",
                        "sensitivity_mode": sens,
                        "gemini_api_key": DEFAULT_API_KEY,
                    },
                )
                cands = data.get("candidates", [])
                self.after(0, lambda: self._render_hot_candidates(cands))
                self.after(
                    0,
                    lambda: self.lbl_status.configure(
                        text="✓ Đã tìm thấy 5 chủ đề Hot! Bấm 'Chọn & Tạo' ở chủ đề bạn thích.",
                        fg=self.c_emerald,
                    ),
                )
            except Exception as e:
                logger.exception("Hot topic scan failed")
                err = str(e)
                self.after(0, lambda: self.lbl_status.configure(text=f"Lỗi quét chủ đề: {err}", fg="#f87171"))
                self.after(0, lambda: messagebox.showerror("Lỗi quét chủ đề", err))
            finally:
                self.is_scanning_hot = False
                self.after(
                    0,
                    lambda: self.btn_scan_hot.configure(
                        state="normal", text="🔍 Quét Mới 5 Chủ Đề Đang Hot Trên Mạng"
                    ),
                )
                self.after(
                    0,
                    lambda: self.btn_create.configure(
                        state="normal",
                        text="🔍 QUÉT 5 CHỦ ĐỀ ĐANG HOT TRÊN MẠNG"
                        if self.current_mode == "hot"
                        else "🚀 TẠO VIDEO NGAY",
                    ),
                )

        threading.Thread(target=_worker, daemon=True).start()

    def _render_hot_candidates(self, cands: list[dict]):
        self.hot_candidates = cands
        for w in self.hot_list_frame.winfo_children():
            w.destroy()
        if not cands:
            tk.Label(
                self.hot_list_frame,
                text="Không tìm thấy chủ đề nào, hãy thử lại.",
                font=("Segoe UI", 9),
                bg=self.c_panel,
                fg="#f87171",
            ).pack(anchor="w", pady=4)
            return

        for idx, c in enumerate(cands[:5]):
            row = tk.Frame(
                self.hot_list_frame,
                bg=self.c_input,
                padx=7,
                pady=4,
                highlightthickness=1,
                highlightbackground="#10b981" if idx == 0 else "#262b38",
            )
            row.pack(fill="x", pady=2)

            prefix = "⭐" if idx == 0 else "🔥"
            lbl = tk.Label(
                row,
                text=f"{prefix} {c.get('topic', '')}",
                font=("Segoe UI", 8, "bold"),
                bg=self.c_input,
                fg=self.c_text,
                anchor="w",
                wraplength=265,
                justify="left",
            )
            lbl.pack(side="left", fill="x", expand=True)

            btn = tk.Button(
                row,
                text="🎬 Chọn & Tạo",
                font=("Segoe UI", 8, "bold"),
                bg=self.c_accent,
                fg="#ffffff",
                activebackground="#0284c7",
                activeforeground="#ffffff",
                bd=0,
                padx=8,
                pady=4,
                cursor="hand2",
                command=lambda i=idx: self._choose_hot_candidate(i),
            )
            btn.pack(side="right", padx=(6, 0))

    def _choose_hot_candidate(self, idx: int):
        if idx >= len(self.hot_candidates):
            return
        c = self.hot_candidates[idx]
        self._switch_mode("direct")
        self._set_sample(c.get("topic", ""), c.get("description", ""))
        self._handle_create()

    def _handle_create(self):
        if self.current_mode == "hot":
            self._scan_hot_topics()
            return

        topic_raw = self.txt_topic.get("1.0", "end").strip()
        desc = self.txt_desc.get("1.0", "end").strip()
        if not topic_raw:
            messagebox.showwarning("Thiếu chủ đề", "Vui lòng nhập chủ đề bài nghe!")
            return

        level = LEVELS[self.cb_level.current()][1]
        target_sentences = DURATIONS[self.cb_duration.current()][1]
        pmode, pair_type, voice_f, voice_m = self._get_selected_voice_cfg()
        sens = SENSITIVITIES[self.cb_sens.current()][1]
        custom_script = self.txt_custom_script.get("1.0", "end").strip()

        self.btn_create.configure(state="disabled", text="⏳ ĐANG KHỞI TẠO VIDEO...")
        self.lbl_status.configure(text="Đang gửi yêu cầu tạo kịch bản & dựng video...", fg=self.c_amber)

        def _worker():
            try:
                lines = [l.strip() for l in topic_raw.splitlines() if l.strip()]
                new_id = None
                if len(lines) == 1 and not custom_script and "|" not in lines[0]:
                    res = api_request(
                        "/api/mode-ab/create",
                        method="POST",
                        payload={
                            "mode": "A",
                            "topic": lines[0],
                            "description": desc,
                            "level": level,
                            "target_sentences": target_sentences,
                            "podcast_mode": pmode,
                            "pair_type": pair_type,
                            "cta": "Đăng ký kênh UNI PODCAST và bình luận ý kiến của bạn bên dưới",
                            "sensitivity_mode": sens,
                            "voice": voice_f,
                            "voice_male": voice_m,
                            "rate": "+0%",
                            "channel_tag": "UNI PODCAST",
                            "gemini_api_key": DEFAULT_API_KEY,
                        },
                    )
                    new_id = res.get("created_id")
                else:
                    items = []
                    for i, line in enumerate(lines):
                        parts = [p.strip() for p in line.split("|")]
                        items.append(
                            {
                                "topic": parts[0] if parts else "CHỦ ĐỀ MỚI",
                                "level": parts[1] if len(parts) > 1 else level,
                                "category": parts[2] if len(parts) > 2 else "TIẾNG ANH THỰC TẾ",
                                "podcast_mode": pmode,
                                "pair_type": pair_type,
                                "sensitivity_mode": sens,
                                "custom_script": custom_script if i == 0 else "",
                            }
                        )
                    res = api_request(
                        "/api/batch",
                        method="POST",
                        payload={
                            "items": items,
                            "podcast_mode": pmode,
                            "pair_type": pair_type,
                            "subtitle_style": "teleprompter",
                            "sensitivity_mode": sens,
                            "voice": voice_f,
                            "voice_male": voice_m,
                            "rate": "+0%",
                            "target_sentences": target_sentences,
                            "channel_tag": "UNI PODCAST",
                            "gemini_api_key": DEFAULT_API_KEY,
                        },
                    )
                    c_ids = res.get("created_ids") or []
                    if c_ids:
                        new_id = c_ids[0]

                if new_id:
                    self.selected_job_id = new_id
                self._refresh_jobs_once()
                self.after(
                    0,
                    lambda: self.lbl_status.configure(
                        text="✓ Đã đưa vào tiến trình dựng video! Xem tiến độ ở bảng bên phải.",
                        fg=self.c_emerald,
                    ),
                )
            except Exception as e:
                logger.exception("Job submit failed")
                err = str(e)
                self.after(0, lambda: self.lbl_status.configure(text=f"Lỗi: {err}", fg="#f87171"))
            finally:
                self.after(
                    0,
                    lambda: self.btn_create.configure(
                        state="normal",
                        text="🔍 QUÉT 5 CHỦ ĐỀ ĐANG HOT TRÊN MẠNG"
                        if self.current_mode == "hot"
                        else "🚀 TẠO VIDEO NGAY",
                    ),
                )

        threading.Thread(target=_worker, daemon=True).start()

    def _refresh_jobs_once(self):
        def _fetch():
            try:
                jobs = api_request("/api/jobs", method="GET")
                if isinstance(jobs, list):
                    self.after(0, lambda: self._update_jobs_ui(jobs))
                stats = api_request("/api/system-stats", method="GET")
                if isinstance(stats, dict) and hasattr(self, "lbl_sys_stats"):
                    cpu_v = int(round(stats.get("cpu_percent", 0)))
                    ram_v = int(round(stats.get("ram_percent", 0)))
                    gpu_v = int(round(stats.get("gpu_percent", 0)))
                    ram_gb = f" ({stats.get('ram_used_gb', 0)}/{stats.get('ram_total_gb', 0)}GB)" if stats.get("ram_total_gb") else ""
                    txt = f"CPU: {cpu_v}%   |   RAM: {ram_v}%{ram_gb}   |   GPU: {gpu_v}%"
                    self.after(0, lambda t=txt: self.lbl_sys_stats.configure(text=t))
            except Exception:
                ensure_backend_running()

        threading.Thread(target=_fetch, daemon=True).start()

    def _poll_jobs(self):
        self._refresh_jobs_once()
        self.after(2000, self._poll_jobs)

    def _update_jobs_ui(self, jobs: list[dict]):
        self.jobs_data = jobs
        self.lbl_count.configure(text=f"Tổng cộng: {len(jobs)} video")

        # Keep current selection or select newest
        if not self.selected_job_id and jobs:
            self.selected_job_id = jobs[0].get("id")

        self.job_listbox.delete(0, "end")
        selected_idx = 0
        for idx, j in enumerate(jobs):
            st = j.get("status", "queued")
            prog = j.get("progress", 0)
            if st == "done":
                badge = "✅ Hoàn thành"
            elif st == "running":
                badge = f"⏳ Đang tạo {prog}%"
            elif st == "error":
                badge = "❌ Lỗi"
            else:
                badge = "🕒 Đang chờ"

            line = f" [{j.get('level', 'B1')}]  {j.get('topic', '')}   —   {badge}"
            self.job_listbox.insert("end", line)
            if j.get("id") == self.selected_job_id:
                selected_idx = idx

        if jobs:
            self.job_listbox.selection_set(selected_idx)
            self._render_selected_detail(jobs[selected_idx])

    def _on_select_job(self, _event=None):
        sel = self.job_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if 0 <= idx < len(self.jobs_data):
            job = self.jobs_data[idx]
            self.selected_job_id = job.get("id")
            self._render_selected_detail(job)

    def _render_selected_detail(self, job: dict):
        lvl = job.get("level", "B1")
        topic = job.get("topic", "")
        st = job.get("status", "queued")
        prog = job.get("progress", 0)
        msg = job.get("message", "")

        self.lbl_detail_title.configure(text=f"[{lvl}] {topic}")
        self.lbl_detail_sub.configure(text=f"Trạng thái: {msg} ({prog}%)")
        self.progress_bar["value"] = prog

        folder = OUTPUT_DIR / job.get("id", "")
        thumb_path = folder / "thumbnail.jpg"
        if thumb_path.exists():
            try:
                img = Image.open(thumb_path)
                img.thumbnail((272, 153), Image.Resampling.LANCZOS)
                self.thumb_photo = ImageTk.PhotoImage(img)
                self.lbl_thumb.configure(image=self.thumb_photo, text="", width=272, height=153)
            except Exception:
                self.lbl_thumb.configure(image="", text="Đang tạo ảnh bìa...", width=36, height=9)
        else:
            self.lbl_thumb.configure(image="", text="Đang chờ ảnh bìa...", width=36, height=9)

    def _get_current_job(self) -> dict | None:
        for j in self.jobs_data:
            if j.get("id") == self.selected_job_id:
                return j
        return None

    def _play_selected(self, file_key: str):
        j = self._get_current_job()
        if not j:
            return
        res = j.get("result") or {}
        fname = res.get(file_key)
        if not fname:
            messagebox.showinfo("Chưa sẵn sàng", "File video này đang được dựng hoặc không có sẵn.")
            return
        fpath = OUTPUT_DIR / j["id"] / fname
        if fpath.exists():
            if hasattr(os, "startfile"):
                os.startfile(str(fpath))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(fpath)])
            else:
                subprocess.Popen(["xdg-open", str(fpath)])
        else:
            messagebox.showinfo("Không tìm thấy file", f"Không tìm thấy: {fpath}")

    def _open_selected_folder(self):
        j = self._get_current_job()
        if not j:
            self._open_folder(OUTPUT_DIR)
            return
        folder = OUTPUT_DIR / j["id"]
        self._open_folder(folder if folder.exists() else OUTPUT_DIR)

    def _open_folder(self, path: Path):
        if sys.platform == "win32":
            subprocess.Popen(["explorer.exe", str(path.resolve())])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path.resolve())])
        else:
            subprocess.Popen(["xdg-open", str(path.resolve())])

    def _copy_selected_desc(self):
        j = self._get_current_job()
        if not j:
            return
        desc = j.get("description_text", "")
        if not desc:
            desc_file = OUTPUT_DIR / j["id"] / "youtube_description.txt"
            if desc_file.exists():
                desc = desc_file.read_text(encoding="utf-8")
        if not desc:
            messagebox.showinfo("Trống", "Video này chưa có nội dung mô tả YouTube.")
            return
        self.clipboard_clear()
        self.clipboard_append(desc)
        self.lbl_detail_sub.configure(text="✓ Đã copy Mô tả YouTube + Chapters vào Clipboard!", fg=self.c_emerald)

    def _view_selected_script(self):
        j = self._get_current_job()
        if not j:
            return
        script_file = OUTPUT_DIR / j["id"] / "script.json"
        if not script_file.exists():
            messagebox.showinfo("Chưa có kịch bản", "Kịch bản đang được AI khởi tạo.")
            return
        sc = json.loads(script_file.read_text(encoding="utf-8"))
        win = tk.Toplevel(self)
        win.title(f"Lời thoại Song ngữ — {j.get('topic', '')}")
        win.geometry("760x560")
        win.configure(bg=self.c_bg)

        txt = tk.Text(
            win,
            font=("Segoe UI", 10),
            bg=self.c_panel,
            fg=self.c_text,
            padx=14,
            pady=14,
            wrap="word",
            bd=0,
        )
        txt.pack(fill="both", expand=True, padx=14, pady=14)

        lines_out = [f"CHỦ ĐỀ: {sc.get('title_vi', '')} (Level {sc.get('level', 'B1')})\n" + "=" * 60 + "\n"]
        for scene in sc.get("scenes", []):
            for ln in scene.get("lines", []):
                spk = ln.get("speaker", "Anna")
                en = ln.get("en", "")
                vi = ln.get("vi", "")
                lines_out.append(f"{spk}: {en}\n   → {vi}\n")
        txt.insert("1.0", "\n".join(lines_out))
        txt.configure(state="disabled")

    def _delete_selected_job(self):
        j = self._get_current_job()
        if not j:
            return
        if not messagebox.askyesno("Xoá video", f"Bạn có chắc muốn xoá '{j.get('topic', '')}'?"):
            return
        job_id = urllib.parse.quote(j["id"])
        try:
            api_request(f"/api/jobs/{job_id}", method="DELETE")
            self.selected_job_id = None
            self._refresh_jobs_once()
        except Exception as e:
            messagebox.showerror("Lỗi xoá", str(e))

    def _open_web_app_window(self):
        # Ensure backend is alive before opening web window
        threading.Thread(target=ensure_backend_running, daemon=True).start()
        if sys.platform == "darwin":
            mac_chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            if os.path.exists(mac_chrome):
                subprocess.Popen([mac_chrome, f"--app={API_BASE}"])
                return
            subprocess.Popen(["open", API_BASE])
            return
        try:
            subprocess.Popen(["cmd", "/c", "start", "msedge", f"--app={API_BASE}"])
        except Exception:
            import webbrowser
            webbrowser.open(API_BASE)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app_win = UniPodcastDesktopApp()
    app_win.mainloop()

