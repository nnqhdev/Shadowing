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

## 🚀 Cách chạy nhanh (Windows)

```bash
# 1. Tạo môi trường ảo & cài đặt thư viện
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt

# 2. Chạy bản Desktop App
Chay_Ung_Dung_Desktop.bat

# Hoặc chạy bản Web Server (cổng 8095)
run_factory.bat
```
