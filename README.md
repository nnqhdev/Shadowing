# 🎧 UNI Podcast Studio (Reach Podcast Factory)

Hệ thống tự động sản xuất **Video Luyện Nghe Tiếng Anh 3 Phần** (16:9 Ngang, 9:16 Dọc, TikTok Highlight) & **Thumbnail 2D Webtoon** kèm giọng đọc biểu cảm tự nhiên (Gemini Live Native Audio `Aoede`, `Puck`, `Kore`, `Fenrir`...).

## ✨ Tính năng chính
- **2 giao diện sử dụng:**
  - 🖥️ **Ứng dụng Desktop (Windows Native GUI):** Giao diện tối giản, dễ dùng (`desktop_app.py` / `Chay_Ung_Dung_Desktop.bat`).
  - 🌐 **Giao diện Web Studio:** Chạy trên trình duyệt tại `http://127.0.0.1:8095` (`app.py` / `run_factory.bat`).
- **2 chế độ sáng tạo kịch bản:**
  - ✍️ **Nhập chủ đề trực tiếp:** Hỗ trợ tạo lẻ hoặc hàng loạt nhiều chủ đề, tuỳ chỉnh tình huống hoặc dán kịch bản có sẵn.
  - 🔥 **Tìm chủ đề Hot (AI):** Tự động quét xu hướng mạng xã hội Việt Nam và đề xuất 5 chủ đề/drama hấp dẫn nhất để dựng video chỉ với 1 cú nhấp chuột.
- **Giọng đọc AI biểu cảm & mượt mà:**
  - Tích hợp 18 giọng đọc Gemini Live Native Audio (9 giọng Nữ, 9 giọng Nam) với bộ lọc chống vấp chữ và chuẩn hoá nhịp thở tự nhiên.
- **Xuất bản đa nền tảng:**
  - Video 3 Phần chuẩn YouTube (`16:9`), Shorts/Reels (`9:16`), TikTok Highlight, ảnh bìa Thumbnail và file Mô tả YouTube kèm Chapters tự động.

## 🚀 Cài đặt & chạy nhanh

Yêu cầu: **Python 3.10+** (đã thử trên 3.14). FFmpeg được cài tự động qua `imageio-ffmpeg`.

### 1. Cấu hình API key Gemini

```bash
cp .env.example .env      # Windows: copy .env.example .env
# mở .env và điền GEMINI_API_KEY=...
```

Có thể bỏ qua bước này và nhập key trực tiếp trong giao diện (key được lưu vào `.env`, file này đã nằm trong `.gitignore`). Biến môi trường `GEMINI_API_KEY` cũng được hỗ trợ.

### 2. Chạy ứng dụng

| | macOS | Windows |
|---|---|---|
| **Desktop App** | `./Chay_Ung_Dung_Desktop.command` | `Chay_Ung_Dung_Desktop.bat` |
| **Web Studio** (cổng 8095) | `./run_factory.command` | `run_factory.bat` |

Lần chạy đầu tiên, script tự tạo `.venv` và cài thư viện. Làm thủ công:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt     # Windows: .venv\Scripts\pip
.venv/bin/python app.py                       # Web Studio tại http://127.0.0.1:8095
.venv/bin/python desktop_app.py               # Desktop App (tự khởi động backend)
```

> macOS: nếu bị chặn khi mở file `.command`, chạy `chmod +x *.command` một lần.

## 🗂️ Cấu trúc dự án

```
app.py             FastAPI backend + API (jobs, batch, storage, system-stats)
static/index.html  Giao diện Web Studio
desktop_app.py     Giao diện Desktop (Tkinter), gọi backend qua HTTP
screenwriter.py    Biên kịch 5 bước + tìm chủ đề Hot
generator.py       Facade tương thích, re-export từ package studio
studio/
  common.py        Hằng số, API key, safe_err (che key trong log)
  script.py        Parse/sinh kịch bản, kịch bản dự phòng
  emotion.py       18 cảm xúc, cường độ, prosody
  tts.py           Giọng đọc Gemini Live / REST, xử lý PCM
  render.py        Slide, teleprompter, video cuộn, ring âm thanh
  thumbnail.py     Thumbnail 2D Webtoon + sinh ảnh nền
  video.py         build_video_job: ghép toàn bộ pipeline
  storage.py       Dọn dẹp thư mục outputs/
tests/             pytest
```

## 🧪 Kiểm thử

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

## 🧹 Dọn dẹp video cũ

Thư mục `outputs/` tăng nhanh (vài trăm MB mỗi video). Dùng nút **🧹 Dọn dẹp** trong giao diện (giữ 10 video mới nhất, không bao giờ xoá job đang chạy), hoặc dòng lệnh:

```bash
.venv/bin/python clean_outputs.py --keep 10 --dry-run   # xem trước
.venv/bin/python clean_outputs.py --keep 10
```

## 🪵 Log

Backend khi được Desktop App khởi động ghi log vào `outputs/backend.log`. Key API luôn được che (`key=***`) trong log.
