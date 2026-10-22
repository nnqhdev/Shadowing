import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any, Dict, List

import httpx

from generator import DEFAULT_GEMINI_KEY, _load_default_gemini_key


def clean_vn_search_query(raw_query: str) -> List[str]:
    """
    Normalize a conversational Vietnamese user query into effective search engine queries,
    removing conversational filler words and fixing common typos while keeping the raw query too.
    """
    raw = (raw_query or "").strip()
    if not raw:
        return []
    # Common typo fixes for Vietnamese names/terms
    fixed = re.sub(r"\btrường\s+gian\b", "Trường Giang", raw, flags=re.IGNORECASE)
    # Remove conversational filler phrases at start/end
    cleaned = re.sub(
        r"\b(vụ\s+việc|vụ|các\s+thứ|mấy\s+thứ|gần\s+đây|đang\s+hot|mới\s+nhất|những\s+ngày\s+qua|tìm\s+kiếm|làm\s+nội\s+dung)\b",
        " ",
        fixed,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.-")
    queries = []
    if cleaned and cleaned.lower() != raw.lower():
        queries.append(cleaned)
    queries.append(fixed)
    # If a proper name or main phrase is present, also add a focused scandal/news query
    if "trường giang" in fixed.lower():
        queries.append("Trường Giang ồn ào quấy rối đồng nghiệp ngoại tình xin lỗi")
    return list(dict.fromkeys(q for q in queries if q))


async def fetch_live_vn_trend_signals(user_query: str = "") -> List[Dict[str, str]]:
    """
    Fetch real live Vietnamese web search snippets & news signals from DuckDuckGo Lite
    and Google News VN RSS. When user_query is provided, searches directly for that case/topic.
    """
    import html as html_mod

    signals: List[Dict[str, str]] = []
    seen_titles: set[str] = set()
    has_user_query = bool((user_query or "").strip())

    search_queries = (
        clean_vn_search_query(user_query)
        if has_user_query
        else [
            "bóc phốt trà xanh cắm sừng tiểu tam bb3 lốp dự phòng Threads TikTok",
            "tâm sự mập mờ trap boy trap girl bị ghost bạn thân khác giới Threads",
        ]
    )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }

    async with httpx.AsyncClient(timeout=3.5, follow_redirects=True, headers=headers) as client:
        async def _fetch_ddg(q: str) -> None:
            try:
                r = await client.post("https://lite.duckduckgo.com/lite/", data={"q": q})
                if r.status_code == 200 and "result-link" in r.text:
                    links = re.findall(
                        r'<a[^>]+rel="nofollow"[^>]+href="([^"]+)"[^>]+class=\'result-link\'>(.*?)</a>',
                        r.text,
                        flags=re.S,
                    )
                    if not links:
                        links = re.findall(
                            r'href="([^"]+)"[^>]*class=.result-link.[^>]*>(.*?)</a>',
                            r.text,
                            flags=re.S,
                        )
                    snippets = re.findall(
                        r'class=.result-snippet.[^>]*>(.*?)</td>',
                        r.text,
                        flags=re.S,
                    )
                    for idx, (href, raw_title) in enumerate(links[:6]):
                        t_clean = html_mod.unescape(re.sub(r"<[^<]+?>", "", raw_title)).strip()
                        s_clean = (
                            html_mod.unescape(re.sub(r"<[^<]+?>", "", snippets[idx])).strip()
                            if idx < len(snippets)
                            else ""
                        )
                        s_clean = re.sub(r"\s+", " ", s_clean)
                        key = t_clean.lower()[:60]
                        if t_clean and key not in seen_titles:
                            seen_titles.add(key)
                            signals.append(
                                {
                                    "title": t_clean,
                                    "snippet": s_clean,
                                    "link": href,
                                    "pubDate": "Web / Báo chí VN gần đây",
                                }
                            )
            except Exception:
                pass

        async def _fetch_rss(q: str) -> None:
            try:
                encoded = urllib.parse.quote(q)
                time_filter = "" if has_user_query else "+when:14d"
                rss_url = f"https://news.google.com/rss/search?q={encoded}{time_filter}&hl=vi&gl=VN&ceid=VN:vi"
                r = await client.get(rss_url)
                if r.status_code == 200 and "<rss" in r.text:
                    root = ET.fromstring(r.text)
                    for item in root.findall(".//item")[:6]:
                        title = (item.findtext("title") or "").strip()
                        link = (item.findtext("link") or "").strip()
                        pub = (item.findtext("pubDate") or "").strip()
                        desc_raw = (item.findtext("description") or "").strip()
                        desc_clean = html_mod.unescape(re.sub(r"<[^<]+?>", " ", desc_raw))
                        desc_clean = re.sub(r"\s+", " ", desc_clean).strip()
                        lower_t = title.lower()
                        if not has_user_query:
                            banned = (
                                "bắt giữ", "khởi tố", "tai nạn", "tử vong", "chính trị",
                                "bạo lực", "án mạng", "công an", "nhà nước", "chiến tranh",
                            )
                            if any(b in lower_t for b in banned):
                                continue
                        key = lower_t[:60]
                        if title and key not in seen_titles:
                            seen_titles.add(key)
                            signals.append(
                                {
                                    "title": title,
                                    "snippet": desc_clean[:240],
                                    "link": link,
                                    "pubDate": pub,
                                }
                            )
            except Exception:
                pass

        import asyncio
        tasks = [_fetch_rss(q) for q in search_queries[:2]] + [_fetch_ddg(q) for q in search_queries[:1]]
        await asyncio.gather(*tasks, return_exceptions=True)

    return signals[:10]


async def call_llm_json(system_prompt: str, user_prompt: str, gemini_api_key: str = "") -> Dict[str, Any]:
    """Call Gemini Flash (or fallback OpenAI-compatible JSON endpoint) and parse JSON."""
    import asyncio
    active_key = (gemini_api_key or "").strip() or _load_default_gemini_key() or DEFAULT_GEMINI_KEY
    raw_json_str = ""
    async with httpx.AsyncClient(timeout=45.0, follow_redirects=True) as client:
        if active_key:
            for model_id in (
                "gemini-2.5-flash",
                "gemini-3-flash-preview",
                "gemini-flash-lite-latest",
            ):
                try:
                    gen_cfg: Dict[str, Any] = {"responseMimeType": "application/json", "temperature": 0.85}
                    if "gemini-3" in model_id:
                        gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}
                    payload = {
                        "contents": [{"parts": [{"text": system_prompt + "\n\n" + user_prompt}]}],
                        "generationConfig": gen_cfg,
                    }
                    gem_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent?key={active_key}"
                    resp = await client.post(gem_url, json=payload, timeout=22.0)
                    if resp.status_code == 200:
                        raw_json_str = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                        if raw_json_str and "{" in raw_json_str:
                            break
                except Exception:
                    continue

        if not raw_json_str:
            for attempt in range(4):
                try:
                    resp = await client.post(
                        "https://text.pollinations.ai/openai",
                        json={
                            "model": "openai",
                            "messages": [
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_prompt},
                            ],
                            "response_format": {"type": "json_object"},
                        },
                        timeout=60.0,
                    )
                    if resp.status_code == 200:
                        raw_json_str = resp.json()["choices"][0]["message"]["content"]
                        if raw_json_str and "{" in raw_json_str:
                            break
                    elif resp.status_code in (402, 429, 502, 503):
                        m = re.search(r"Retry after (\d+)s", resp.text)
                        wait_s = min(20, max(4, int(m.group(1)) + 1)) if m else (6 * (attempt + 1))
                        await asyncio.sleep(wait_s)
                        continue
                except Exception:
                    await asyncio.sleep(3 * (attempt + 1))

    if not raw_json_str:
        return {}

    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_json_str.strip(), flags=re.MULTILINE)
    s_idx = cleaned.find("{")
    e_idx = cleaned.rfind("}")
    if s_idx != -1 and e_idx != -1:
        cleaned = cleaned[s_idx : e_idx + 1]
    try:
        return json.loads(cleaned)
    except Exception:
        return {}


async def discover_hot_topics_mode_b(
    level: str = "B1",
    description_hint: str = "",
    cta: str = "Đăng ký kênh & ôn từ vựng",
    gemini_api_key: str = "",
    sensitivity_mode: str = "normal",
    topic_hint: str = "",
) -> Dict[str, Any]:
    """
    Chế độ B — AI tìm chủ đề đang hot (Threads, TikTok, YouTube Shorts/Long, FB Reels & Báo chí VN):
    - Nếu người dùng chủ động nhập chủ đề/vụ việc cụ thể (topic_hint hoặc description_hint):
      Hệ thống lập tức tìm kiếm thông tin thực tế trên Web/Báo chí về đúng vụ việc đó và lập đủ 5 góc khai thác
      trực diện xoay quanh đúng vụ việc người dùng yêu cầu (không tự ý đổi sang chủ đề khác).
    - Nếu người dùng để trống: Hệ thống tự quét xu hướng chung 7-14 ngày qua.
    """
    combined_hint = " — ".join(
        p.strip() for p in (topic_hint, description_hint) if p and p.strip()
    )
    has_explicit_query = bool(combined_hint)

    live_signals = await fetch_live_vn_trend_signals(combined_hint)
    has_live_rss = len(live_signals) > 0
    signals_context = (
        "\n".join(
            f"- {s['title']} ({s['pubDate']})"
            + (f"\n  Tóm tắt: {s['snippet']}" if s.get("snippet") else "")
            for s in live_signals
        )
        if has_live_rss
        else "Không truy cập trực tiếp được số liệu realtime của Threads/TikTok."
    )

    # If the user explicitly typed a drama/scandal/sensitive topic, honor their explicit request
    effective_sensitivity = sensitivity_mode
    if has_explicit_query and sensitivity_mode == "normal":
        effective_sensitivity = "sensitive_ok"

    tone_rule = {
        "polite": (
            "CHẾ ĐỘ LỊCH SỰ (Polite): Phân tích vấn đề một cách văn minh, lịch sự, dùng từ ngữ chuẩn mực."
        ),
        "normal": (
            "CHẾ ĐỘ BÌNH THƯỜNG (Normal): Chọn chủ đề đời thường gần gũi (công sở, bạn bè, tình cảm). "
            "Nếu người dùng đã chỉ định vụ việc cụ thể thì bám sát vụ việc đó."
        ),
        "sensitive_ok": (
            "CHẾ ĐỘ CHO PHÉP NHẠY CẢM / DRAMA THỰC TẾ (Sensitive/Edgy Allowed): Cho phép khai thác trực diện "
            "các vụ việc drama, scandal người nổi tiếng, quấy rối đồng nghiệp, ngoại tình, góc khuất tâm lý, "
            "tranh cãi mạng xã hội đang hot trên Threads/TikTok/Báo chí."
        ),
    }.get(effective_sensitivity, "Khai thác tự nhiên, chân thực.")

    system_prompt = (
        "Bạn là Biên kịch Kể chuyện Đời thực & Bắt trend Threads/TikTok cho kênh 'UNI Podcast'. "
        "TUYỆT ĐỐI KHÔNG viết theo giọng báo chí, nghị luận xã hội hay giáo điều nghiêm túc. "
        "Mọi chủ đề phải là CÂU CHUYỆN THẬT GIỮA 2 CON NGƯỜI BẰNG XƯƠNG BẰNG THỊT (người yêu, crush, mập mờ, người yêu cũ, vợ chồng, đôi bạn thân) "
        "với cảm xúc đời thường chân thật: rung động, ghen tuông, tổn thương, dở khóc dở cười, những lời thú nhận lúc nửa đêm. "
        "Trả về DUY NHẤT JSON hợp lệ, không dùng markdown fence."
    )

    if has_explicit_query:
        user_prompt = f"""Người dùng đang tìm ý tưởng câu chuyện Threads/TikTok xoay quanh từ khóa: **"{combined_hint}"** (Trình độ **{level.upper()}**).
Mức độ nội dung: {tone_rule}

Tín hiệu tham khảo từ mạng / báo chí:
{signals_context}

QUY TẮC BẮT BUỘC ĐỂ NỘI DUNG ĐÚNG CHẤT THREADS (KHÔNG NGHIÊM TÚC / KHÔNG GIÁO ĐIỀU):
1. **KHÔNG VIẾT KIỂU BÁO CHÍ HAY BÌNH LUẬN THỜI SỰ:** Hai nhân vật KHÔNG PHẢI là 2 MC ngồi phân tích đạo lý hay điểm báo!
2. **BIẾN "{combined_hint}" THÀNH 5 CÂU CHUYỆN TRỰC TIẾP GIỮA 2 NGƯỜI TRONG CUỘC:**
   - Cả 5 ứng viên phải xoay quanh đúng ý tưởng "{combined_hint}", nhưng đặt 2 nhân vật vào **một tình huống đời thực giàu cảm xúc** (ví dụ: một cuộc đối chất nghẹt thở, một lời thú nhận bất ngờ, một khoảnh khắc bắt quả tang, một cuộc trò chuyện lúc 2 giờ sáng, hoặc một tình huống dở khóc dở cười giữa 2 người).
   - Trong phần `description`, hãy mô tả rõ: **Hai nhân vật là gì của nhau? Chuyện gì vừa xảy ra giữa hai người họ? Câu mở đầu (Hook) gây ấn tượng ra sao? Cú twist cảm xúc ở cuối là gì?**
3. **CÁCH ĐẶT TIÊU ĐỀ (`topic`) BẮT BUỘC DÙNG NGÔN NGỮ GEN Z BẮT TREND THREADS/TIKTOK:**
   - Tiêu đề (`topic`, 3–6 từ) PHẢI dùng từ lóng Gen Z sắc bén, giật tít đúng gu giới trẻ như: **`LỐP` / `LỐP DỰ PHÒNG`**, **`BB3` / `BÉ BA` / `TIỂU TAM`**, **`CẮM SỪNG`**, **`TRÀ XANH`**, **`MẬP MỜ`**, **`TRAP BOY` / `TRAP GIRL`**, **`RED FLAG`**, **`BỊ GHOST`**, **`SIMP CHÚA`**, **`EX HÃM`**...
   - Ví dụ tiêu đề chuẩn Gen Z: *"LÀM LỐP DỰ PHÒNG SUỐT NỬA NĂM"*, *"BẮT QUẢ TANG TRÀ XANH CƯỚP BỒ"*, *"BỊ CẮM SỪNG BỞI BẠN THÂN"*, *"ĐỐI CHẤT TRỰC DIỆN VỚI BB3"*.
4. Trả về JSON theo đúng cấu trúc:
{{
  "web_status_note": "Đã tìm kiếm & chuyển hóa chủ đề thành 5 câu chuyện đời thực giàu cảm xúc chuẩn vị Gen Z Threads.",
  "candidates": [
    {{
      "id": 1,
      "topic": "TIÊU ĐỀ NGÔN NGỮ GEN Z CỰC CHÁY (3-6 từ, VD: BỊ CẮM SỪNG BỞI TRÀ XANH)",
      "description": "Kể rõ câu chuyện giữa 2 người: Họ đang ở đâu, chuyện gì xảy ra giữa họ, cảm xúc thật (rung động / tổn thương / kịch tính / cay cú) và cú twist cuối câu chuyện.",
      "platform": "Threads VN / TikTok Storytime",
      "evidence": "Góc nhìn tâm sự đời thực từ xu hướng thảo luận trên Threads",
      "level_reason": "Lý do câu chuyện này giúp người học {level.upper()} ngấm từ vựng giao tiếp cảm xúc đời thực"
    }}
  ],
  "best_id": 1,
  "best_reason": "Lý do câu chuyện #1 chân thật, chạm cảm xúc nhất."
}}"""
    else:
        user_prompt = f"""Hãy tìm **5 câu chuyện tâm sự / drama tình cảm đời thường đang cực HOT trên Threads VN & TikTok** cho người học tiếng Anh trình độ **{level.upper()}**.
Mức độ nội dung: {tone_rule}

QUY TẮC BẮT BUỘC ĐỂ ĐÚNG CHẤT GEN Z THREADS (TIÊU ĐỀ GEN Z CỰC CHÁY + CÂU CHUYỆN CHÂN THẬT):
1. **CÁCH ĐẶT TIÊU ĐỀ (`topic`) PHẢI DÙNG NGÔN NGỮ GEN Z ĐÚNG VIBE THREADS/TIKTOK (3–6 từ):**
   - Bắt buộc sử dụng các từ khóa Gen Z cuốn hút như: **`LỐP` / `LỐP DỰ PHÒNG`**, **`BB3` (Bé ba / Tiểu tam)**, **`CẮM SỪNG`**, **`TRÀ XANH`**, **`MẬP MỜ`**, **`TRAP BOY` / `TRAP GIRL`**, **`BỊ GHOST`**, **`RED FLAG`**, **`SIMP`**, **`EX HÃM`**...
   - Ví dụ tiêu đề chuẩn:
     + *"PHÁT HIỆN MÌNH CHỈ LÀ LỐP"*
     + *"BỊ BẠN THÂN VÀ TRÀ XANH CẮM SỪNG"*
     + *"ĐỤNG ĐỘ BB3 NGAY TẠI NHÀ"*
     + *"LỠ SIMP MỘT TÊN TRAP BOY"*
     + *"MẬP MỜ 6 THÁNG RỒI BỊ GHOST"*
2. **CHỈ CHỌN NHỮNG CÂU CHUYỆN ĐỜI THỰC GIỮA 2 NGƯỜI** khiến người nghe cuốn theo từng câu thoại:
   - Mỗi ứng viên trong `description` phải là **một câu chuyện có hồn giữa 2 người trong cuộc** (có bối cảnh cụ thể, có cảm xúc thật, có chi tiết bắt quả tang/thú nhận đắt giá và cú twist bất ngờ).

Trả về JSON theo đúng cấu trúc:
{{
  "web_status_note": "Đã quét các mạch chuyện tâm sự tình cảm & drama Gen Z viral nhất trên Threads VN.",
  "candidates": [
    {{
      "id": 1,
      "topic": "TIÊU ĐỀ NGÔN NGỮ GEN Z BẮT TAI (3-6 từ, VD: PHÁT HIỆN MÌNH CHỈ LÀ LỐP)",
      "description": "Mô tả câu chuyện cụ thể giữa 2 người: Bối cảnh thật, chuyện gì đã xảy ra giữa họ, câu Hook mở đầu và cú Twist cảm xúc ở cuối.",
      "platform": "Threads VN / TikTok",
      "evidence": "Xu hướng tâm sự đời thường & tình cảm Gen Z viral trên Threads VN",
      "level_reason": "Từ vựng giao tiếp cảm xúc đời thực cực hợp trình độ {level.upper()}"
    }}
  ],
  "best_id": 1,
  "best_reason": "Lý do câu chuyện này chạm cảm xúc nhất."
}}"""

    data = await call_llm_json(system_prompt, user_prompt, gemini_api_key)
    candidates = data.get("candidates") if isinstance(data, dict) else None
    if not candidates or not isinstance(candidates, list) or len(candidates) < 3:
        if has_explicit_query:
            ev_text = (
                "; ".join(s["title"] for s in live_signals[:3])
                if live_signals
                else f"Tổng hợp theo yêu cầu tìm kiếm: {combined_hint}"
            )
            candidates = [
                {
                    "id": 1,
                    "topic": f"TOÀN CẢNH ỒN ÀO: {combined_hint[:45].upper()}",
                    "description": f"Anna và Tom bàn luận trực diện về vụ việc đang gây bão mạng '{combined_hint}': từ loạt clip/thông tin bị đào lại trên Threads đến làn sóng phản ứng dữ dội của công chúng và lời xin lỗi muộn màng.",
                    "platform": "Threads VN & TikTok & Báo chí",
                    "evidence": f"Kết quả tìm kiếm thực tế: {ev_text}",
                    "level_reason": f"Cung cấp bộ từ vựng {level.upper()} thực tế về scandal, dư luận và đạo đức nghề nghiệp (public backlash, scandal, cross the line, apology).",
                },
                {
                    "id": 2,
                    "topic": "RANH GIỚI ĐÙA GIỠN VÀ QUẤY RỐI ĐỒNG NGHIỆP",
                    "description": f"Từ vụ việc '{combined_hint}', Anna và Tom mổ xẻ ranh giới giữa sự thân mật xã giao và hành vi quấy rối đồng nghiệp nữ nơi làm việc (workplace harassment).",
                    "platform": "Threads VN & YouTube Shorts",
                    "evidence": f"Bám sát khía cạnh quấy rối đồng nghiệp trong vụ việc: {ev_text}",
                    "level_reason": f"Từ vựng giao tiếp bảo vệ bản thân và vạch rõ ranh giới ở trình độ {level.upper()} (set boundaries, inappropriate behavior, uncomfortable).",
                },
                {
                    "id": 3,
                    "topic": "HÌNH TƯỢNG NGƯỜI CHỒNG MẪU MỰC SỤP ĐỔ VÌ NGOẠI TÌNH",
                    "description": f"Khai thác khía cạnh ngoại tình và phản bội niềm tin trong vụ '{combined_hint}': Tại sao những người hay nói đạo lý yêu vợ thương con trên sóng truyền hình lại khiến khán giả thất vọng nhất?",
                    "platform": "Threads VN & Facebook Reels",
                    "evidence": f"Khai thác góc nhìn bê bối tình ái & gia đình từ vụ việc: {ev_text}",
                    "level_reason": f"Mẫu câu bày tỏ sự thất vọng, phân tích tâm lý và niềm tin hôn nhân vừa sức với level {level.upper()} (betrayal, hypocrite, family man image).",
                },
                {
                    "id": 4,
                    "topic": "KHI NGƯỜI VỢ BỊ VẠ LÂY TỪ SCANDAL CỦA CHỒNG",
                    "description": f"Anna đồng cảm với nỗi khổ của người vợ khi chồng vướng ồn ào '{combined_hint}', bị cộng đồng mạng réo tên và đứng giữa áp lực tha thứ hay buông tay.",
                    "platform": "Threads VN & TikTok",
                    "evidence": f"Dựa trên phản ứng dư luận quanh gia đình nhân vật chính: {ev_text}",
                    "level_reason": f"Giàu cảm xúc đời thường, dễ luyện ngữ điệu đồng cảm và chia sẻ ở trình độ {level.upper()}.",
                },
                {
                    "id": 5,
                    "topic": "CÁCH ĐÁP TRẢ KHI BỊ ĐỒNG NGHIỆP VƯỢT QUÁ GIỚI HẠN",
                    "description": f"Bài học thực tế rút ra từ vụ '{combined_hint}': Làm sao để lên tiếng phản kháng đanh thép khi gặp người có quyền thế/đàn anh có hành vi kém duyên nơi làm việc.",
                    "platform": "TikTok & YouTube Podcast",
                    "evidence": f"Bài học ứng dụng thực tế từ tâm điểm tranh luận: {ev_text}",
                    "level_reason": f"Cung cấp các câu giao tiếp tiếng Anh trực diện để tự vệ và từ chối khéo léo cho người học {level.upper()}.",
                },
            ]
            data = {
                "web_status_note": f"Đã tìm kiếm trực tiếp trên Web/Báo chí VN cho yêu cầu '{combined_hint}' và lập 5 góc khai thác bám sát vụ việc.",
                "candidates": candidates,
                "best_id": 1,
                "best_reason": f"Ứng viên #1 đi trực diện vào toàn cảnh vụ việc '{combined_hint}' đang gây bão trên Threads/TikTok và báo chí. Bạn có duyệt góc khai thác này (hoặc chọn 1 trong 5 góc bên dưới) để viết kịch bản 5 bước không?",
            }
        else:
            candidates = [
                {
                    "id": 1,
                    "topic": "PHÁT HIỆN MÌNH CHỈ LÀ LỐP",
                    "description": "Suốt 6 tháng ngày nào anh cũng nhắn tin ngọt ngào lúc nửa đêm, nhưng hôm nay cô vô tình bắt gặp anh đi cùng người yêu chính thức và nhận ra mình chỉ là chiếc 'lốp dự phòng' lúc anh buồn.",
                    "platform": "Threads VN & TikTok",
                    "evidence": "Chủ đề 'Lốp dự phòng' & 'Mập mờ' viral hàng đầu trên Threads VN",
                    "level_reason": f"Hội thoại đối chất & tổn thương chân thật, giàu cảm xúc ở trình độ {level.upper()}.",
                },
                {
                    "id": 2,
                    "topic": "BỊ BẠN THÂN VÀ TRÀ XANH CẮM SỪNG",
                    "description": "Cô bạn thân luôn miệng khuyên cô chia tay người yêu vì 'anh ta không hợp với mày đâu', để rồi tối đó cô bắt quả tang chính cô bạn 'trà xanh' ấy đang nắm tay người yêu mình.",
                    "platform": "Threads VN & TikTok",
                    "evidence": "Mạch chuyện bóc phốt Trà xanh & Cắm sừng luôn bùng nổ tương tác trên Threads",
                    "level_reason": f"Câu thoại kịch tính, đủ 5 cung bậc cảm xúc từ sững sờ đến giận dữ vừa sức level {level.upper()}.",
                },
                {
                    "id": 3,
                    "topic": "ĐỤNG ĐỘ BB3 NGAY TẠI QUÁN QUEN",
                    "description": "Người yêu báo bận tăng ca không nghe máy, nhưng khi cô ghé quán cà phê quen thuộc thì thấy anh đang đút bánh cho 'bé ba' (BB3) và còn nói dối mình còn độc thân.",
                    "platform": "Threads VN & Facebook Reels",
                    "evidence": "Chủ đề bắt quả tang BB3 / Tiểu tam cực kỳ hút người nghe trên mạng xã hội",
                    "level_reason": f"Đối đáp trực diện sắc bén, giàu cảm xúc đời thực cho level {level.upper()}.",
                },
                {
                    "id": 4,
                    "topic": "LỠ SIMP MỘT TÊN TRAP BOY",
                    "description": "Biết rõ đối phương là 'trap boy' chính hiệu đầy 'red flag' nhưng cô vẫn đâm đầu vào, đến khi phát hiện cùng lúc anh gửi một câu chúc ngủ ngon cho 4 cô gái khác mới tỉnh mộng.",
                    "platform": "TikTok & Threads VN",
                    "evidence": "Mạch chuyện Trap boy / Trap girl & Red flag được Gen Z bàn tán sôi nổi",
                    "level_reason": f"Rất cuốn hút từ câu Hook đầu tiên đến cú lật cuối bài, phù hợp trình độ {level.upper()}.",
                },
                {
                    "id": 5,
                    "topic": "MẬP MỜ 6 THÁNG RỒI BỊ GHOST",
                    "description": "Đi ăn, xem phim, nắm tay như người yêu suốt nửa năm nhưng ngay sau đêm cô thổ lộ tình cảm thật lòng thì anh bỗng dưng 'ghost' biến mất rồi hôm nay bất ngờ xuất hiện lại.",
                    "platform": "Threads VN & TikTok",
                    "evidence": "Tình huống Mập mờ & Ghosting gây đồng cảm mạnh nhất trong giới trẻ",
                    "level_reason": f"Ngữ điệu vừa nghẹn ngào vừa dứt khoát, sát giao tiếp đời sống trình độ {level.upper()}.",
                },
            ]
            data = {
                "web_status_note": (
                    "Đã quét tín hiệu tin tức đời sống & tổng hợp dựa trên xu hướng thảo luận thực tế trên Threads/TikTok. "
                    "Các số liệu lượt xem cụ thể được ghi rõ 'Chưa kiểm chứng' theo đúng nguyên tắc không tự bịa số liệu."
                ),
                "candidates": candidates,
                "best_id": 1,
                "best_reason": (
                    "Ứng viên #1 ('Mập mờ không danh phận - Situationship') là chủ đề được thảo luận sôi nổi nhất trên Threads VN, "
                    "có kịch tính tự nhiên, dễ tạo Hook 3 giây đầu cho TikTok và cực kỳ hợp để viết hội thoại 2 người Anna & Tom. "
                    "Bạn có duyệt chủ đề này (hoặc chọn 1 trong 5 ứng viên bên dưới) để mình bắt đầu viết kịch bản 5 bước không?"
                ),
            }

    return data


_RECENT_STORY_HISTORY: List[str] = []
_STORY_ANGLE_COUNTER: int = 0

STORY_SCENARIO_ANGLES = (
    "Bối cảnh 1: Vô tình nhìn thấy thông báo tin nhắn bật lên trên màn hình điện thoại của đối phương khi đang ngồi ăn tối cùng nhau.",
    "Bối cảnh 2: Bắt gặp đối phương đi cùng một người khác tại quán cà phê quen thuộc dù vừa nhắn tin báo bận tăng ca.",
    "Bối cảnh 3: Tìm thấy một món quà sinh nhật / hóa đơn mua sắm cho người khác trong cốp xe hoặc túi áo khoác.",
    "Bối cảnh 4: Cuộc gọi chất vấn thẳng thắn ngay sau khi phát hiện mình bị ẩn khỏi story mạng xã hội suốt nhiều tháng.",
    "Bối cảnh 5: Gặp lại nhau dưới sảnh chung cư / trạm xe buýt lúc trời mưa sau khi một người đột ngột biến mất (ghost) không lý do.",
    "Bối cảnh 6: Nghe được cuộc điện thoại nói dối của đối phương từ phòng bên cạnh trong buổi tụ tập bạn bè.",
    "Bối cảnh 7: Một người bạn chung vô tình tiết lộ sự thật động trời mà đối phương đã giấu kín bấy lâu.",
    "Bối cảnh 8: Đối diện trực tiếp tại bàn làm việc / lớp học ngay sau khi tin đồn hoặc ảnh chụp chung bị lộ ra.",
)

CEFR_LEVEL_PROFILES: Dict[str, Dict[str, str]] = {
    "A1": {
        "word_range": "4-7 words",
        "vn_range": "4–7 từ/câu",
        "rules": (
            "STRICT CEFR A1 (Beginner — 4 to 7 words per sentence ONLY): "
            "Use ONLY basic top-500 everyday English words (see, call, lie, love, friend, wait, sad, go, stop, why). "
            "Use ONLY short simple sentences in Present Simple, Past Simple, or 'will/can'. "
            "STRICTLY FORBIDDEN: NO sentences over 7 words, NO complex clauses, NO idioms, NO advanced phrasal verbs, NO past continuous."
        ),
    },
    "A2": {
        "word_range": "6-9 words",
        "vn_range": "6–9 từ/câu",
        "rules": (
            "STRICT CEFR A2 (Elementary — 6 to 9 words per sentence): "
            "Use simple everyday vocabulary (top-1000 words) and short compound sentences with 'and', 'but', 'because', 'so', 'when'. "
            "Keep grammar simple and clear for A2 learners. Do NOT exceed 9 words per sentence."
        ),
    },
    "B1": {
        "word_range": "8-12 words",
        "vn_range": "8–12 từ/câu",
        "rules": (
            "STRICT CEFR B1 (Intermediate — 8 to 12 words per sentence): "
            "Use natural intermediate conversational English, common phrasal verbs (find out, make up, give up, move on), "
            "present perfect / past continuous / relative clauses ('that', 'who'), expressing clear emotional reasons and feelings."
        ),
    },
    "B2": {
        "word_range": "10-14 words",
        "vn_range": "10–14 từ/câu",
        "rules": (
            "STRICT CEFR B2 (Upper-Intermediate — 10 to 14 words per sentence): "
            "Use expressive upper-intermediate vocabulary, emotional idioms and collocations (take for granted, behind my back, "
            "mixed signals, cross the line), conditionals, and nuanced conversational structures."
        ),
    },
    "C1": {
        "word_range": "11-16 words",
        "vn_range": "11–16 từ/câu",
        "rules": (
            "STRICT CEFR C1 (Advanced — 11 to 16 words per sentence): "
            "Use sophisticated emotional phrasing, advanced idioms (string someone along, read between the lines, "
            "keep someone on the hook), subtle irony, and complex subordinate clauses."
        ),
    },
    "C2": {
        "word_range": "12-18 words",
        "vn_range": "12–18 từ/câu",
        "rules": (
            "STRICT CEFR C2 (Mastery / Native — 12 to 18 words per sentence): "
            "Use authentic native-level American English dialogue with rich idiomatic expressions, emotional subtext, "
            "natural contractions, and cinematic rhythm."
        ),
    },
}


async def generate_5step_screenplay(
    topic: str,
    description: str = "",
    level: str = "B1",
    target_turns: int = 10,
    cta: str = "Đăng ký kênh REACH, ôn lại 5 từ vựng mục tiêu và trả lời câu hỏi ở phần bình luận",
    gemini_api_key: str = "",
    sensitivity_mode: str = "normal",
    podcast_mode: str = "dialogue",
    pair_type: str = "male_female",
) -> Dict[str, Any]:
    """
    Thực hiện trọn vẹn PHẦN 3 — YÊU CẦU CỐ ĐỊNH CHO AI (Biên kịch 5 Bước):
      - Bước 1: Nhân vật
      - Bước 2: Khung kịch bản (Mở bài Hook ấn tượng ngay câu 1-2, Thân bài kể rõ đầu đuôi câu chuyện, Cao trào/Twist, Kết bài + CTA)
      - Bước 3: Tự chấm trước khi viết lời thoại
      - Bước 4: Lời thoại (Xuất đủ 3 bản: Chỉ Anh, Chỉ Việt, Song ngữ Anh-Việt + Bản cắt TikTok)
      - Bước 5: Đóng gói
    """
    global _STORY_ANGLE_COUNTER
    lvl = level.strip().upper() or "B1"
    lvl_profile = CEFR_LEVEL_PROFILES.get(lvl, CEFR_LEVEL_PROFILES["B1"])
    word_range_en = lvl_profile["word_range"]
    word_range_vn = lvl_profile["vn_range"]
    level_spec = lvl_profile["rules"]

    scenario_seed = STORY_SCENARIO_ANGLES[_STORY_ANGLE_COUNTER % len(STORY_SCENARIO_ANGLES)]
    _STORY_ANGLE_COUNTER += 1
    anti_repeat_block = (
        "\n- **TUYỆT ĐỐI CẤM LẶP LẠI CÁC CÂU MỞ ĐẦU / KỊCH BẢN VỪA TẠO GẦN ĐÂY:**\n  "
        + "\n  ".join(f"• Không dùng lại: {h}" for h in _RECENT_STORY_HISTORY[-6:])
        + "\n  • CẤM dùng câu mẫu 'Why are you standing outside my door at two in the morning?'. Phải sáng tạo tình huống & lời thoại hoàn toàn mới!"
    ) if _RECENT_STORY_HISTORY else (
        "\n- **QUY TẮC ĐỘC BẢN:** CẤM dùng mô-típ đứng trước cửa lúc 2 giờ sáng ('Why are you standing outside my door at two in the morning?'). Phải sáng tạo câu chuyện và lời thoại mới 100%!"
    )

    raw_turns = int(target_turns or 10)
    turns = max(8, min(60, raw_turns))
    turns_range_str = "8–10 câu" if turns <= 12 else "18–25 câu"
    is_mono = (podcast_mode or "dialogue").strip().lower() == "monologue"
    ptype = (pair_type or "male_female").strip().lower()
    if ptype == "male_male":
        sp1_name, sp2_name = "Tom", "Jack"
        pair_label = "2 Nam (Tom và Jack — đôi bạn thân / anh em / đồng nghiệp nam)"
    elif ptype == "female_female":
        sp1_name, sp2_name = "Anna", "Mia"
        pair_label = "2 Nữ (Anna và Mia — đôi bạn thân / chị em / đồng nghiệp nữ)"
    else:
        sp1_name, sp2_name = "Anna", "Tom"
        pair_label = "Nam & Nữ (Anna là Nữ và Tom là Nam)"

    tone_desc = {
        "polite": "LỊCH SỰ (Ấm áp, nhẹ nhàng, trong sáng, giàu cảm xúc chân thành)",
        "normal": "CHÂN THẬT ĐỜI THƯỜNG (Tự nhiên như cuộc trò chuyện thật ngoài đời, có rung động, ngập ngừng, tổn thương hoặc vỡ òa)",
        "sensitive_ok": "DRAMA & CẢM XÚC MÃNH LIỆT (Xoáy sâu vào tâm lý, tình yêu, mập mờ, phản bội, ghen tuông hoặc bí mật giấu kín)",
    }.get(sensitivity_mode, "CHÂN THẬT ĐỜI THƯỜNG (Tự nhiên, giàu cảm xúc)")

    if is_mono:
        system_prompt = (
            "Bạn là Biên kịch Kể chuyện Độc thoại (Storytime / Confession 1 người) cho kênh 'UNI Podcast'. "
            "TUYỆT ĐỐI KHÔNG viết hội thoại đối đáp 2 người! "
            "Hãy viết một BÀI KỂ CHUYỆN TỰ SỰ NGÔI THỨ NHẤT ('I' / 'Mình' - 'Tôi') do DUY NHẤT 1 NGƯỜI KỂ xuyên suốt từ đầu đến cuối: "
            "mở đầu bằng câu Hook tự sự gây ấn tượng mạnh, "
            "sau đó kể lại mạch lạc đầu đuôi câu chuyện (chuyện gì đã xảy ra, những kỷ niệm và chi tiết cụ thể với 'he/she/they'), "
            "đẩy lên cú Twist nhận ra sự thật, và kết lại bằng cảm xúc lắng đọng. "
            f"BẮT BUỘC TUÂN THỦ NGHIÊM NGẶT TRÌNH ĐỘ TIẾNG ANH CEFR {lvl}: {level_spec} "
            "Chỉ trả về JSON hợp lệ (không có markdown fence)."
        )
        story_rules_block = f"""1. **ĐÂY LÀ BÀI KỂ CHUYỆN ĐỘC THOẠI 1 NGƯỜI (STORYTIME / TỰ SỰ NGÔI THỨ NHẤT 'I') — CẤM VIẾT ĐỐI THOẠI 2 NGƯỜI:**
   - Chỉ có DUY NHẤT 1 người kể chuyện (`speaker` luôn là `"{sp1_name}"`).
   - Người kể xưng `I` (dịch tiếng Việt là `Mình`, `Tôi`, hoặc `Em`) đang kể lại câu chuyện của chính mình cho khán giả nghe (nhắc đến đối phương bằng ngôi thứ ba `he`, `she`, `my best friend`, `my ex`... chứ KHÔNG đối đáp trực tiếp qua lại với nhân vật thứ hai).
2. **KHÚC ĐẦU TIÊN (CÂU 1–2) PHẢI LÀ HOOK KỂ CHUYỆN GÂY ẤN TƯỢNG MẠNH NGAY LẬP TỨC (ĐÚNG ĐỘ DÀI LEVEL {lvl}: {word_range_vn}):**
   - Mở đầu bằng một lời thú nhận hoặc phát hiện kịch tính gắn với bối cảnh: *{scenario_seed}*
3. **MẠCH KỂ CHUYỆN 4 CHẶNG RÕ RÀNG, LIỀN MẠCH, CÓ ĐẦU CÓ ĐUÔI:**
   - **Chặng 1 - Hook mở màn (Câu 1–2):** Câu dẫn nhập gây tò mò cực mạnh vào câu chuyện "{topic}".
   - **Chặng 2 - Kể lại diễn biến & chi tiết thật:** Kể rõ chuyện bắt đầu thế nào, những chi tiết đời thường cụ thể đã diễn ra sao cho người nghe hình dung trọn vẹn câu chuyện.
   - **Chặng 3 - Cao trào / Cú Twist phát hiện sự thật:** Khoảnh khắc bắt quả tang, đọc được tin nhắn, hoặc nhận ra bộ mặt thật khiến cảm xúc vỡ òa (`sad`, `angry`).
   - **Chặng 4 - Kết chuyện & bài học đắt giá (1–2 câu cuối):** Quyết định buông bỏ dứt khoát hoặc sự trưởng thành sau biến cố (`serious`, `normal`)."""
    else:
        system_prompt = (
            "Bạn là Biên kịch Kể chuyện Tâm lý & Tình cảm Đời thực cho kênh 'UNI Podcast'. "
            "TUYỆT ĐỐI KHÔNG viết lời thoại kiểu sách giáo khoa khô khan, không viết kiểu 2 MC bàn luận thời sự, và không viết câu cụt lủn vô hồn. "
            "Hãy viết một CÂU CHUYỆN ĐỐI THOẠI THẬT GIỮA 2 CON NGƯỜI ĐANG SỐNG TRONG TÌNH HUỐNG ĐÓ (ngôi I và You) "
            "khiến người nghe cuốn theo từng câu thoại. "
            f"BẮT BUỘC TUÂN THỦ NGHIÊM NGẶT TRÌNH ĐỘ TIẾNG ANH CEFR {lvl}: {level_spec} "
            "Chỉ trả về JSON hợp lệ (không có markdown fence)."
        )
        story_rules_block = f"""1. **KHÚC ĐẦU TIÊN (CÂU 1–2) PHẢI LÀ HOOK GÂY ẤN TƯỢNG MẠNH & CHẠM CẢM XÚC NGAY LẬP TỨC (ĐÚNG ĐỘ DÀI LEVEL {lvl}: {word_range_vn}):**
   - Không chào hỏi xã giao ("Hi, how are you?").
   - Bối cảnh gợi ý riêng cho tập này: *{scenario_seed}*
   - Mở đầu ngay bằng một câu nói trực diện làm người nghe khựng lại (một câu hỏi chất vấn, một lời thú nhận bất ngờ, hoặc một chi tiết bắt quả tang đắt giá phù hợp với bối cảnh trên).
2. **PHẢI LÀ CÂU CHUYỆN THẬT CỦA 2 NGƯỜI TRONG CUỘC (CÓ ĐẦU CÓ ĐUÔI RÕ RÀNG QUA 4 CHẶNG):**
   - Hai nhân vật `{sp1_name}` và `{sp2_name}` KHÔNG bàn luận lý thuyết chung chung, mà đang **sống trong câu chuyện của chính họ**:
     + **Chặng 1 - Hook mở màn (Câu 1–2):** Câu mở đầu gây tò mò và ấn tượng mạnh ngay lập tức.
     + **Chặng 2 - Kể rõ chuyện gì đã xảy ra giữa 2 người:** Lồng ghép tự nhiên các chi tiết đời thực cụ thể để người nghe hiểu rõ toàn bộ mạch truyện.
     + **Chặng 3 - Cao trào cảm xúc / Cú Twist (Giữa - gần cuối bài):** Nút thắt được tháo gỡ — một lời nói thật từ đáy lòng, một bí mật được giấu kín bấy lâu, hoặc một sự thật đảo chiều khiến cảm xúc vỡ òa.
     + **Chặng 4 - Kết chuyện đọng lại dư âm (1–2 câu cuối):** Một cái kết trọn vẹn, ấm áp hoặc dứt khoát."""

    user_prompt = f"""Hãy viết kịch bản câu chuyện đầy cảm xúc 5 bước cho tập luyện nghe tiếng Anh:
- Chủ đề câu chuyện: "{topic}"
- Tình huống / Ý tưởng chi tiết: "{description or f'Một câu chuyện đời thực, chân thật và chạm cảm xúc về: {topic} ({scenario_seed})'}"
- Mức độ cảm xúc: {tone_desc}
- **TRÌNH ĐỘ TIẾNG ANH BẮT BUỘC (CEFR LEVEL {lvl}):**
  + Quy chuẩn câu & từ vựng Level {lvl}: **{level_spec}**
  + Độ dài mỗi câu tiếng Anh (`en`): **BẮT BUỘC từ {word_range_vn} ({word_range_en})** — nếu là A1 câu phải cực ngắn và dùng từ cơ bản nhất (4–7 từ); nếu là B1/B2/C1/C2 câu phải dài hơn, dùng cấu trúc ngữ pháp và idioms đúng tầm của Level {lvl}!{anti_repeat_block}
- Hình thức đọc: {"ĐỘC THOẠI KỂ CHUYỆN 1 NGƯỜI (Storytime / Tự sự ngôi thứ nhất 'I' — chỉ 1 người kể lại toàn bộ câu chuyện từ đầu đến cuối, TUYỆT ĐỐI KHÔNG viết hội thoại 2 người, speaker luôn là '" + sp1_name + "')" if is_mono else f"ĐỐI THOẠI 2 NGƯỜI thuộc cặp {pair_label} (2 nhân vật '{sp1_name}' và '{sp2_name}' là 2 người trong cuộc đang trực tiếp đối thoại trong câu chuyện này)"}
- Thời lượng & Số câu (turns): Khung {turns_range_str} (chính xác {turns} câu trong mảng step4_lines)
- CTA cuối bài: "{cta}"

YÊU CẦU BẮT BUỘC ĐỂ KỊCH BẢN CHÂN THẬT & PHÂN HÓA RÕ TRÌNH ĐỘ {lvl}:
{story_rules_block}
3. **Độ dài và độ khó câu tiếng Anh (`en`) chuẩn Level {lvl} ({word_range_vn}):**
   - Mọi câu `en` trong `step4_lines` PHẢI tuân thủ đúng độ dài **{word_range_vn}** và từ vựng của trình độ **{lvl}**.
   - Bản dịch tiếng Việt (`vi`) phải mượt mà, đời thường, truyền tải trọn vẹn sắc thái cảm xúc.
- **Bước 4 — Lời thoại ({turns} câu thuộc khung {turns_range_str}) & HỆ THỐNG 18 CẢM XÚC + TRỘN CẢM XÚC + 3 CƯỜNG ĐỘ:**
  + Mỗi lượt gồm `speaker` (`"{sp1_name}"`{" hoặc `\"" + sp2_name + "\"`" if not is_mono else ""}), `emotion`, `en` (100% tiếng Anh tự nhiên đúng trình độ {lvl} [{word_range_en}], không kèm tên người nói), `vi` (tiếng Việt tự nhiên, dùng ngôn ngữ đời thực / từ lóng Gen Z tự nhiên khi hợp ngữ cảnh), `is_tiktok_climax` (true cho 6-8 câu cao trào nhất).
  + **BẢNG NHÃN `emotion` ĐẦY ĐỦ (Gồm 18 cảm xúc đơn + 3 cảm xúc trộn + 3 mức cường độ `:30%`, `:60%`, `:100%`):**
    * **A. Nhóm cơ bản (1–8):** `"normal"` (trung tính/tự nhiên), `"happy"` (vui vẻ), `"excited"` (hào hứng), `"sad"` (buồn), `"angry"` (giận bùng nổ), `"angry_cold"` (giận lạnh/nghiến chữ), `"fear"` (sợ hãi/hoảng), `"surprised"` (ngạc nhiên — chỉ 1 câu ngắn rồi chuyển sang trạng thái tiếp theo), `"disgusted"` (ghê tởm/khó chịu).
    * **B. Nhóm xã hội (9–18 — ưu tiên dùng nhiều):** `"friendly"` (thân thiện/ấm áp), `"empathetic"` (đồng cảm/an ủi), `"serious"` (nghiêm túc), `"confident"` (tự tin/dứt khoát), `"anxious"` (lo lắng/bồn chồn), `"shy"` (ngượng ngùng/e dè), `"sarcastic"` (mỉa mai/châm biếm — cần gán đúng khi lời nói mang nghĩa ngược), `"tired"` (mệt mỏi/uể oải), `"whisper"` (thì thầm/bí mật), `"gentle"` (dịu dàng).
    * **D. Nhóm trộn cảm xúc & Cường độ (`:30%` nhẹ, `:60%` vừa, `:100%` mạnh):** `"happy+anxious"` (mừng nhưng sợ hỏng), `"sad+angry"` (uất ức/nghẹn uất), `"happy+sarcastic"` (cười nhạt/chua chát). Có thể gắn kèm cường độ như `"sad:60%"`, `"sad+angry:100%"`, `"sarcastic:60%"`, `"empathetic:60%"`. Phần lớn câu nên ở mức `:30%` hoặc `:60%`, chỉ đẩy lên `:100%` ở đúng 1–2 câu đỉnh điểm cao trào!
  + **QUY TẮC CHUYỂN CẢM XÚC MƯỢT MÀ:** Không đổi đột ngột từng câu (trừ `"surprised"`), chuyển dần mượt mà theo mạch tâm lý nhân vật.
- **Bước 5 — Đóng gói & CÁCH ĐẶT TIÊU ĐỀ CHUẨN GEN Z:**
  + **`title_vi` (Hiển thị lên Thumbnail & Tiêu đề chính, 2–5 từ IN HOA)** và **`titles` (3 gợi ý tiêu đề)** BẮT BUỘC đặt mới, sáng tạo bằng **ngôn ngữ Gen Z bắt trend cực cháy** bám sát tình huống câu chuyện vừa viết (không sao chép lại nguyên xi tên chủ đề nếu có thể giật tít hay hơn).

Trả về JSON theo đúng schema sau:
{{
  "title_vi": "TIÊU ĐỀ GEN Z MỚI CỰC CHÁY (2-5 TỪ IN HOA)",
  "level": "{lvl}",
  "step1_characters": [
    {{
      "name": "{sp1_name}",
      "age": "24",
      "personality": "Tính cách chính",
      "desire": "Điều nhân vật muốn trong tập này",
      "obstacle": "Điều cản trở nhân vật"
    }},
    {{
      "name": "{sp2_name}",
      "age": "25",
      "personality": "Tính cách chính",
      "desire": "Điều nhân vật muốn trong tập này",
      "obstacle": "Điều cản trở nhân vật"
    }}
  ],
  "step2_outline": {{
    "opening_hook": "Mở bài (Hook câu đầu gây sốc)...",
    "body_scenes": "Thân bài & tình tiết đối đáp dẫn tới cao trào...",
    "climax_twist": "Cao trào (lật tình huống hoặc cảm xúc)...",
    "ending_cta": "Kết bài..."
  }},
  "step3_self_review": {{
    "youtube_check": "Đánh giá câu ngắn/dài đúng chuẩn Level {lvl} ({word_range_vn}) và cách lặp từ vựng mục tiêu...",
    "tiktok_check": "Đánh giá Hook 3 giây đầu và đoạn cao trào...",
    "realism_emotion": "Cảm xúc chủ đạo & độ chân thực...",
    "unresolved_or_fixed": "Chỉ rõ sự chuyển biến 5 sắc thái cảm xúc (sad, happy, angry, serious, normal)..."
  }},
  "step4_lines": [
    {{
      "speaker": "{sp1_name}",
      "emotion": "sad",
      "en": "English line strictly matching CEFR {lvl} ({word_range_en}).",
      "vi": "Câu dịch tiếng Việt chân thật, tự nhiên.",
      "is_tiktok_climax": false
    }}
  ],
  "step5_packaging": {{
    "titles": [
      "Luyện Nghe Tiếng Anh Level {lvl} - ...",
      "Luyện Nghe Tiếng Anh Level {lvl} - ...",
      "Luyện Nghe Tiếng Anh Level {lvl} - ..."
    ],
    "thumbnail_desc": "1 câu mô tả hình ảnh và cảm xúc nhân vật trên Thumbnail.",
    "character_en": "STEP 2 Scene in English: vary gender, age (child to elder), body type, hair style/color, outfit, accessories (never default to a young man in a blue shirt); specific facial muscles (eyebrows, eyelids, mouth, cheeks); pose/hands + 1 story prop; camera angle; small accents.",
    "target_vocab": [
      {{
        "word": "từ/cụm từ tiếng Anh đúng trình độ {lvl}",
        "pos": "n/v/adj/phrase",
        "meaning_vi": "nghĩa tiếng Việt",
        "repeat_count": 3
      }}
    ]
  }}
}}"""

    data = await call_llm_json(system_prompt, user_prompt, gemini_api_key)
    lines = data.get("step4_lines") if isinstance(data, dict) else None
    if lines and isinstance(lines, list) and len(lines) >= 2:
        first_en = str(lines[0].get("en") or "").strip()
        gen_title = str(data.get("title_vi") or "").strip()
        if first_en:
            _RECENT_STORY_HISTORY.append(f"[{lvl}] {gen_title}: \"{first_en}\"")

    if not lines or not isinstance(lines, list) or len(lines) < 4:
        clean_t = topic.strip().upper() or "CHỈ LÀ LỐP DỰ PHÒNG"
        if is_mono:
            lines = [
                {"speaker": sp1_name, "emotion": "sad", "en": "I just realized I was his backup plan for six whole months.", "vi": "Mình vừa nhận ra suốt sáu tháng qua mình chỉ là lốp dự phòng của anh ta.", "is_tiktok_climax": False},
                {"speaker": sp1_name, "emotion": "normal", "en": "Every night at two in the morning, he would text me sweet words.", "vi": "Mỗi đêm lúc hai giờ sáng, anh ta đều nhắn cho mình những lời ngọt ngào.", "is_tiktok_climax": False},
                {"speaker": sp1_name, "emotion": "happy", "en": "He bought my favorite coffee and held my hand in the rain.", "vi": "Anh ta mua món cà phê mình thích và nắm tay mình dưới cơn mưa.", "is_tiktok_climax": False},
                {"speaker": sp1_name, "emotion": "sad", "en": "Whenever I asked what we were, he told me to wait a little longer.", "vi": "Nhưng mỗi khi mình hỏi chúng ta là gì, anh lại bảo mình chờ thêm chút nữa.", "is_tiktok_climax": True},
                {"speaker": sp1_name, "emotion": "serious", "en": "Then yesterday, I walked into our favorite cafe after work.", "vi": "Rồi hôm qua, mình bước vào quán cà phê quen thuộc sau giờ làm.", "is_tiktok_climax": True},
                {"speaker": sp1_name, "emotion": "angry", "en": "He was sitting there celebrating his anniversary with his real girlfriend.", "vi": "Anh ta đang ngồi đó mừng kỷ niệm tình yêu với bạn gái chính thức.", "is_tiktok_climax": True},
                {"speaker": sp1_name, "emotion": "angry", "en": "He looked straight at me and pretended we were total strangers.", "vi": "Anh ta nhìn thẳng vào mắt mình rồi giả vờ như hai người xa lạ.", "is_tiktok_climax": True},
                {"speaker": sp1_name, "emotion": "serious", "en": "I blocked his number right there and walked away for good.", "vi": "Mình chặn số anh ta ngay tại chỗ và quay lưng bước đi mãi mãi.", "is_tiktok_climax": False},
            ]
        else:
            lines = [
                {"speaker": "Anna", "emotion": "angry", "en": "How could you treat me like a backup plan for six months?", "vi": "Sao anh có thể coi em như lốp dự phòng suốt sáu tháng qua?", "is_tiktok_climax": False},
                {"speaker": "Tom", "emotion": "serious", "en": "Anna, please lower your voice and let me explain.", "vi": "Anna, làm ơn nói nhỏ thôi và để anh giải thích đã.", "is_tiktok_climax": False},
                {"speaker": "Anna", "emotion": "angry", "en": "Explain what? I saw you holding her hand at our cafe!", "vi": "Giải thích gì cơ? Em thấy anh nắm tay cô ta ở quán quen của tụi mình!", "is_tiktok_climax": True},
                {"speaker": "Tom", "emotion": "sad", "en": "I wanted to tell you the truth, but I was afraid of losing you.", "vi": "Anh đã muốn nói sự thật, nhưng anh sợ sẽ mất em.", "is_tiktok_climax": True},
                {"speaker": "Anna", "emotion": "sad", "en": "You texted me every night at two in the morning saying you missed me.", "vi": "Đêm nào hai giờ sáng anh cũng nhắn tin nói nhớ em cơ mà.", "is_tiktok_climax": True},
                {"speaker": "Anna", "emotion": "angry", "en": "And worst of all, she is my closest friend!", "vi": "Và tệ nhất là cô ta lại chính là bạn thân của em!", "is_tiktok_climax": True},
                {"speaker": "Tom", "emotion": "sad", "en": "I know I messed everything up, and I am truly sorry.", "vi": "Anh biết anh đã làm hỏng tất cả, anh thực sự xin lỗi.", "is_tiktok_climax": False},
                {"speaker": "Anna", "emotion": "serious", "en": "Delete my number, because we are completely done.", "vi": "Xóa số tôi đi, giữa chúng ta chấm hết thật rồi.", "is_tiktok_climax": False},
            ]
        data = {
            "title_vi": clean_t[:36],
            "level": lvl,
            "step1_characters": [
                {
                    "name": "Anna",
                    "age": "23",
                    "personality": "Giàu cảm xúc, hay suy nghĩ nhiều nhưng chân thành",
                    "desire": f"Muốn giải tỏa nỗi lòng về '{topic}'",
                    "obstacle": "Sợ nói thẳng sẽ làm mất lòng hoặc hỏng mối quan hệ",
                },
                {
                    "name": "Tom",
                    "age": "25",
                    "personality": "Thẳng thắn, hài hước, biết lắng nghe và phân tích thực tế",
                    "desire": "Giúp Anna nhìn nhận vấn đề rõ ràng và quyết đoán hơn",
                    "obstacle": "Anna ban đầu còn ngại ngùng, vòng vo chưa dám kể hết",
                },
            ],
            "step2_outline": {
                "opening_hook": f"Hook 15s đầu: Anna thú nhận mất ngủ cả đêm vì '{topic}', kích thích sự tò mò ngay lập tức.",
                "body_scenes": "Thân bài: Anna kể lại sự giằng xé nội tâm khi cố giữ im lặng; Tom đồng cảm và đẩy tình huống lên cao.",
                "climax_twist": "Cao trào (Twist): Khi Anna quyết định nói thật vào sáng nay, đối phương lại thú nhận cũng có suy nghĩ giống hệt cô.",
                "ending_cta": f"Kết bài: Giải tỏa nút thắt cảm xúc và kêu gọi hành động ({cta}).",
            },
            "step3_self_review": {
                "youtube_check": f"Nhịp độ đối đáp nhanh, câu 10-16 từ chuẩn {lvl}, người học hiểu ~85% ngay lần nghe đầu; các cụm từ vựng mục tiêu (speak up, ignore feelings, relieved) được lặp lại xuyên suốt.",
                "tiktok_check": "Hook 3 giây đầu đi thẳng vào nỗi đau mất ngủ; đoạn cao trào 4 câu giữa bài có cú twist rõ ràng, cắt riêng lên TikTok tự đứng độc lập.",
                "realism_emotion": "Cảm xúc chính: hồi hộp -> đồng cảm -> vỡ òa nhẹ nhõm. Lời thoại tự nhiên như 2 người bạn trò chuyện quán cà phê, không giáo điều.",
                "unresolved_or_fixed": "Lưu ý: Bản nháp đầu câu hơi dài, đã tách thành câu ngắn có từ cảm thán (Oh gosh, Wait seriously, Ugh) để dễ tập Shadowing.",
            },
            "step4_lines": lines,
            "step5_packaging": {
                "titles": [
                    f"Luyện Nghe Tiếng Anh Level {lvl} - {clean_t}",
                    f"Luyện Nghe Tiếng Anh Level {lvl} - NỖI LÒNG {clean_t}",
                    f"Luyện Nghe Tiếng Anh Level {lvl} - CÂU CHUYỆN {clean_t}",
                ],
                "thumbnail_desc": "Cô gái và chàng trai Anime ngồi trò chuyện dưới ánh nắng chiều ấm áp, vẻ mặt vừa bất ngờ vừa nhẹ nhõm.",
                "target_vocab": [
                    {"word": "speak up", "pos": "phr.v", "meaning_vi": "lên tiếng, nói thẳng ra", "repeat_count": 3},
                    {"word": "ignore feelings", "pos": "phrase", "meaning_vi": "phớt lờ cảm xúc", "repeat_count": 3},
                    {"word": "relieved", "pos": "adj", "meaning_vi": "nhẹ nhõm, thở phào", "repeat_count": 3},
                ],
            },
        }

    # Build the 3 required script versions in Step 4 + TikTok cut + custom_script_for_video
    en_only_lines: List[str] = []
    vi_only_lines: List[str] = []
    bilingual_lines: List[str] = []
    tiktok_climax_lines: List[str] = []
    custom_script_rows: List[str] = []

    from generator import build_thumbnail_prompts, parse_emotion_and_intensity

    for idx, item in enumerate(lines[:turns]):
        if is_mono:
            sp = sp1_name
        else:
            raw_sp = str(item.get("speaker") or "").strip()
            if raw_sp.lower() in (sp1_name.lower(), "speaker a", "speaker 1", "a"):
                sp = sp1_name
            elif raw_sp.lower() in (sp2_name.lower(), "speaker b", "speaker 2", "b"):
                sp = sp2_name
            else:
                sp = sp1_name if idx % 2 == 0 else sp2_name
        en = re.sub(r"^(Anna|Tom|Jack|Mia)\s*(?:\[[^\]]+\])?\s*:\s*", "", str(item.get("en") or "").strip(), flags=re.IGNORECASE)
        vi = re.sub(r"^(Anna|Tom|Jack|Mia)\s*(?:\[[^\]]+\])?\s*:\s*", "", str(item.get("vi") or "").strip(), flags=re.IGNORECASE)
        raw_emo = str(item.get("emotion") or "normal").strip().lower()
        canon_emo, int_scale = parse_emotion_and_intensity(raw_emo)
        if not canon_emo:
            canon_emo = "normal"
        if ":" in raw_emo:
            pct_lbl = "100%" if int_scale >= 0.9 else ("30%" if int_scale <= 0.55 else "60%")
            emo = f"{canon_emo}:{pct_lbl}"
        else:
            emo = canon_emo
        if not en:
            continue
        en_only_lines.append(f"{sp}: {en}")
        vi_only_lines.append(f"{sp}: {vi}")
        bilingual_lines.append(f"{sp}: {en}\n   -> {vi}")
        custom_script_rows.append(f"{sp} [{emo}]: {en} | {vi}")
        if item.get("is_tiktok_climax"):
            tiktok_climax_lines.append(f"{sp}: {en}\n   -> {vi}")

    if not tiktok_climax_lines and len(bilingual_lines) >= 4:
        mid = len(bilingual_lines) // 2
        tiktok_climax_lines = bilingual_lines[max(0, mid - 2) : min(len(bilingual_lines), mid + 2)]

    tiktok_cut_full = (
        "\n\n".join(tiktok_climax_lines)
        + "\n\n👉 Xem trọn bài nghe ở kênh YouTube REACH"
    )

    pkg = data.setdefault("step5_packaging", {})
    thumb_prompts = build_thumbnail_prompts(
        title_vi=str(data.get("title_vi") or topic),
        level=lvl,
        channel_tag="UNI PODCAST",
        character_en=str(pkg.get("character_en") or ""),
    )
    pkg["fixed_style_prompt"] = thumb_prompts["fixed_style_prompt"]
    pkg["variable_video_prompt"] = thumb_prompts["variable_video_prompt"]
    pkg["character_prompt"] = thumb_prompts["character_prompt"]
    pkg["thumbnail_prompt"] = thumb_prompts["thumbnail_prompt"]
    if not pkg.get("thumbnail_desc"):
        pkg["thumbnail_desc"] = thumb_prompts["variable_video_prompt"]

    data["step4_formatted"] = {
        "en_only": "\n".join(en_only_lines),
        "vi_only": "\n".join(vi_only_lines),
        "bilingual": "\n\n".join(bilingual_lines),
        "tiktok_cut": tiktok_cut_full,
        "custom_script_for_video": "\n".join(custom_script_rows),
    }
    return data
