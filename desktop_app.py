import json
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
    ("Ngắn (~2 - 3 phút)", 8),
    ("Vừa (~6 - 8 phút)", 18),
    ("Dài (~12 - 15 phút)", 32),
    ("Tập lớn (~20 phút)", 52),
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


def ensure_backend_running() -> None:
    """Start FastAPI backend in a daemon thread if not already listening on 8095."""
    try:
        with urllib.request.urlopen(f"{API_BASE}/api/jobs", timeout=1.2) as r:
            if r.status == 200:
                return
    except Exception:
        pass

    def _run_server():
        import uvicorn
        from app import app
        uvicorn.run(app, host="127.0.0.1", port=8095, log_level="warning")

    t = threading.Thread(target=_run_server, daemon=True)
    t.start()
    for _ in range(25):
        time.sleep(0.25)
        try:
            with urllib.request.urlopen(f"{API_BASE}/api/jobs", timeout=1.0) as r:
                if r.status == 200:
                    return
        except Exception:
            pass


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

        # Mode Switcher (Direct vs Hot AI)
        mode_bar = tk.Frame(left, bg=self.c_input, padx=4, pady=4)
        mode_bar.pack(fill="x", pady=(0, 12))
        mode_bar.columnconfigure(0, weight=1)
        mode_bar.columnconfigure(1, weight=1)

        self.btn_mode_direct = tk.Button(
            mode_bar,
            text="✍️ Nhập chủ đề",
            font=("Segoe UI", 10, "bold"),
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
            text="🔥 Tìm chủ đề Hot (AI)",
            font=("Segoe UI", 10, "bold"),
            bg=self.c_input,
            fg=self.c_muted,
            activebackground=self.c_card,
            activeforeground="#ffffff",
            bd=0,
            pady=7,
            cursor="hand2",
            command=lambda: self._switch_mode("hot"),
        )
        self.btn_mode_hot.grid(row=0, column=1, sticky="ew", padx=(2, 0))

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
        self.txt_topic.insert("1.0", "THÍCH THẦM")

        # Quick sample chips
        chips_bar = tk.Frame(self.frame_direct, bg=self.c_panel)
        chips_bar.pack(fill="x", pady=(0, 10))
        tk.Label(chips_bar, text="Gợi ý nhanh:", font=("Segoe UI", 8), bg=self.c_panel, fg=self.c_muted).pack(side="left", padx=(0, 6))
        for label_txt, sample_t, sample_d in [
            ("Thích thầm", "THÍCH THẦM", ""),
            ("Bạn mượn tiền", "BẠN THÂN MƯỢN TIỀN KHÔNG TRẢ", "Cho bạn thân vay tiền lúc khó khăn nhưng bạn khất lần 3 tháng dù ngày nào cũng đi cà phê sang chảnh."),
            ("Bạn cùng phòng", "CÔNG CHÚA PHÒNG TRỌ", "Bạn cùng phòng ở bẩn nhưng ra đường luôn ăn mặc lộng lẫy."),
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

        # --- Shared Core Settings (4 Clean Dropdowns) ---
        tk.Frame(left, bg="#262b38", height=1).pack(fill="x", pady=8)

        grid4 = tk.Frame(left, bg=self.c_panel)
        grid4.pack(fill="x")
        grid4.columnconfigure(0, weight=1)
        grid4.columnconfigure(1, weight=1)

        # Row 1: Level + Duration
        f_lvl = tk.Frame(grid4, bg=self.c_panel)
        f_lvl.grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=4)
        self._label(f_lvl, "Trình độ (Level):").pack(anchor="w", pady=(0, 3))
        self.cb_level = ttk.Combobox(f_lvl, values=[x[0] for x in LEVELS], state="readonly", font=("Segoe UI", 9))
        self.cb_level.current(2)
        self.cb_level.pack(fill="x")

        f_dur = tk.Frame(grid4, bg=self.c_panel)
        f_dur.grid(row=0, column=1, sticky="ew", padx=(6, 0), pady=4)
        self._label(f_dur, "Thời lượng:").pack(anchor="w", pady=(0, 3))
        self.cb_duration = ttk.Combobox(f_dur, values=[x[0] for x in DURATIONS], state="readonly", font=("Segoe UI", 9))
        self.cb_duration.current(0)
        self.cb_duration.pack(fill="x")

        # Row 2: Female Voice + Male Voice
        f_vf = tk.Frame(grid4, bg=self.c_panel)
        f_vf.grid(row=1, column=0, sticky="ew", padx=(0, 6), pady=6)
        self._label(f_vf, "Giọng Nữ (Anna):").pack(anchor="w", pady=(0, 3))
        self.cb_voice_f = ttk.Combobox(f_vf, values=[x[0] for x in FEMALE_VOICES], state="readonly", font=("Segoe UI", 9))
        self.cb_voice_f.current(0)
        self.cb_voice_f.pack(fill="x")

        f_vm = tk.Frame(grid4, bg=self.c_panel)
        f_vm.grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=6)
        self._label(f_vm, "Giọng Nam (Tom):").pack(anchor="w", pady=(0, 3))
        self.cb_voice_m = ttk.Combobox(f_vm, values=[x[0] for x in MALE_VOICES], state="readonly", font=("Segoe UI", 9))
        self.cb_voice_m.current(0)
        self.cb_voice_m.pack(fill="x")

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
        adv_grid.columnconfigure(1, weight=1)

        f_sens = tk.Frame(adv_grid, bg=self.c_input)
        f_sens.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        tk.Label(f_sens, text="Mức độ nội dung:", font=("Segoe UI", 8, "bold"), bg=self.c_input, fg="#cbd5e1").pack(anchor="w", pady=(0, 2))
        self.cb_sens = ttk.Combobox(f_sens, values=[x[0] for x in SENSITIVITIES], state="readonly", font=("Segoe UI", 8))
        self.cb_sens.current(0)
        self.cb_sens.pack(fill="x")

        f_pmode = tk.Frame(adv_grid, bg=self.c_input)
        f_pmode.grid(row=0, column=1, sticky="ew", padx=(5, 0))
        tk.Label(f_pmode, text="Số người đọc:", font=("Segoe UI", 8, "bold"), bg=self.c_input, fg="#cbd5e1").pack(anchor="w", pady=(0, 2))
        self.cb_pmode = ttk.Combobox(f_pmode, values=[x[0] for x in PODCAST_MODES], state="readonly", font=("Segoe UI", 8))
        self.cb_pmode.current(0)
        self.cb_pmode.pack(fill="x")

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
        if mode == "direct":
            self.btn_mode_direct.configure(bg=self.c_card, fg="#ffffff")
            self.btn_mode_hot.configure(bg=self.c_input, fg=self.c_muted)
            self.frame_hot.pack_forget()
            self.frame_direct.pack(fill="x")
            self.btn_create.configure(text="🚀 TẠO VIDEO NGAY")
        else:
            self.btn_mode_hot.configure(bg=self.c_card, fg="#ffffff")
            self.btn_mode_direct.configure(bg=self.c_input, fg=self.c_muted)
            self.frame_direct.pack_forget()
            self.frame_hot.pack(fill="x")
            self.btn_create.configure(text="🔍 QUÉT 5 CHỦ ĐỀ ĐANG HOT TRÊN MẠNG")
            if not self.hot_candidates and not self.is_scanning_hot:
                self._scan_hot_topics()

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
                self.after(0, lambda: self.lbl_status.configure(text=f"Lỗi quét chủ đề: {e}", fg="#f87171"))
                self.after(0, lambda: messagebox.showerror("Lỗi quét chủ đề", str(e)))
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
        voice_f = FEMALE_VOICES[self.cb_voice_f.current()][1]
        voice_m = MALE_VOICES[self.cb_voice_m.current()][1]
        sens = SENSITIVITIES[self.cb_sens.current()][1]
        pmode = PODCAST_MODES[self.cb_pmode.current()][1]
        custom_script = self.txt_custom_script.get("1.0", "end").strip()

        self.btn_create.configure(state="disabled", text="⏳ ĐANG KHỞI TẠO VIDEO...")
        self.lbl_status.configure(text="Đang gửi yêu cầu tạo kịch bản & dựng video...", fg=self.c_amber)

        def _worker():
            try:
                lines = [l.strip() for l in topic_raw.splitlines() if l.strip()]
                new_id = None
                if len(lines) == 1 and desc and not custom_script and "|" not in lines[0]:
                    res = api_request(
                        "/api/mode-ab/create",
                        method="POST",
                        payload={
                            "mode": "A",
                            "topic": lines[0],
                            "description": desc,
                            "level": level,
                            "target_sentences": target_sentences,
                            "cta": "Đăng ký kênh UNI PODCAST và bình luận ý kiến của bạn bên dưới",
                            "sensitivity_mode": sens,
                            "voice": voice_f,
                            "voice_male": voice_m,
                            "rate": "-12%",
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
                            "subtitle_style": "teleprompter",
                            "sensitivity_mode": sens,
                            "voice": voice_f,
                            "voice_male": voice_m,
                            "rate": "-12%",
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
                self.after(0, lambda: self.lbl_status.configure(text=f"Lỗi: {e}", fg="#f87171"))
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
            except Exception:
                pass

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
            os.startfile(str(fpath))
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
        subprocess.Popen(["explorer.exe", str(path.resolve())])

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
        # Open native frameless Edge/Chrome --app window pointing to 127.0.0.1:8095
        try:
            subprocess.Popen(["cmd", "/c", "start", "msedge", f"--app={API_BASE}"])
        except Exception:
            import webbrowser
            webbrowser.open(API_BASE)


if __name__ == "__main__":
    app_win = UniPodcastDesktopApp()
    app_win.mainloop()

