import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any, Dict, List

import httpx

from generator import DEFAULT_GEMINI_KEY


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
            "xu hướng giới trẻ Threads TikTok tâm sự đời sống",
            "Gen Z công sở tình yêu chuyện đời thường",
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
    active_key = (gemini_api_key or "").strip() or DEFAULT_GEMINI_KEY
    raw_json_str = ""
    async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
        if active_key:
            for model_id in (
                "gemini-flash-lite-latest",
                "gemini-3-flash-preview",
                "gemini-flash-latest",
            ):
                try:
                    gen_cfg: Dict[str, Any] = {"responseMimeType": "application/json", "temperature": 0.7}
                    if "gemini-3" in model_id:
                        gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}
                    payload = {
                        "contents": [{"parts": [{"text": system_prompt + "\n\n" + user_prompt}]}],
                        "generationConfig": gen_cfg,
                    }
                    gem_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent?key={active_key}"
                    resp = await client.post(gem_url, json=payload, timeout=12.0)
                    if resp.status_code == 200:
                        raw_json_str = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                        if raw_json_str and "{" in raw_json_str:
                            break
                except Exception:
                    continue


        if not raw_json_str:
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
                    timeout=55.0,
                )
                if resp.status_code == 200:
                    raw_json_str = resp.json()["choices"][0]["message"]["content"]
            except Exception:
                pass

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
        "Bạn là Trưởng nhóm Nghiên cứu Xu hướng & Biên kịch cho kênh YouTube luyện nghe tiếng Anh 'REACH Podcast'. "
        "Trả về DUY NHẤT JSON hợp lệ, không dùng markdown fence."
    )

    if has_explicit_query:
        user_prompt = f"""Người dùng đang sử dụng **Chế độ B** nhưng ĐÃ CHỦ ĐỘNG NHẬP CỤ THỂ vụ việc / chủ đề đang hot mà họ muốn làm video luyện nghe tiếng Anh trình độ **{level.upper()}**:
👉 Yêu cầu chủ đề của người dùng: **"{combined_hint}"**
👉 Mức độ nội dung: {tone_rule}

Dưới đây là KẾT QUẢ TÌM KIẾM WEB & BÁO CHÍ VIỆT NAM THỰC TẾ vừa thu thập được cho từ khóa của người dùng:
{signals_context}

QUY TẮC BẮT BUỘC TUYỆT ĐỐI KHI NGƯỜI DÙNG ĐÃ NHẬP VỤ VIỆC CỤ THỂ:
1. **CẢ 5 ỨNG VIÊN (#1 đến #5) PHẢI XOAY QUANH TRỰC TIẾP VỤ VIỆC "{combined_hint}"** và dữ liệu báo chí ở trên!
   - TUYỆT ĐỐI KHÔNG ĐƯỢC tự ý né tránh, không được xóa tên nhân vật/vụ việc người dùng đã nhập (ví dụ nếu người dùng nhập về vụ Trường Giang bị đào lại clip đùa giỡn/quấy rối đồng nghiệp nữ, ồn ào ngoại tình, phải xin lỗi và rút khỏi chương trình, thì cả 5 ứng viên đều phải khai thác trực tiếp các khía cạnh của đúng vụ việc Trường Giang đó!).
   - TUYỆT ĐỐI KHÔNG ĐƯỢC chèn các chủ đề khác không liên quan (như "công chúa phòng trọ", "mượn tiền", "sợ gọi điện thoại"...) vào bảng 5 ứng viên!
2. Hãy chia vụ việc **"{combined_hint}"** thành **5 góc khai thác kịch bản hội thoại 2 người (Anna & Tom)** thật cuốn hút để người dùng chọn góc họ thích nhất:
   - **Ứng viên #1 (Toàn cảnh vụ việc & Cú sụp đổ hình tượng):** Tóm tắt trực diện vụ việc đang gây bão mạng (nhân vật, các clip bị đào lại, lời xin lỗi, phản ứng dậy sóng trên Threads/TikTok).
   - **Ứng viên #2 (Góc nhìn Quấy rối nơi làm việc & Ranh giới với đồng nghiệp):** Phân tích hành vi đùa giỡn quá trớn, động chạm kém duyên với đồng nghiệp nữ dưới vỏ bọc "thân thiện, hài hước".
   - **Ứng viên #3 (Góc nhìn Ngoại tình & Nỗi đau của người vợ/gia đình bị vạ lây):** Bàn luận về bê bối tình ái sau lưng gia đình và tình cảnh người vợ bị réo tên trên mạng xã hội.
   - **Ứng viên #4 (Nghịch lý "Người chồng quốc dân / Hay nói đạo lý" vs Thực tế):** Vì sao công chúng phẫn nộ gấp đôi khi người nổi tiếng từng xây dựng hình tượng yêu vợ thương con lại vướng liên hoàn phốt?
   - **Ứng viên #5 (Bài học thực tế & Từ vựng phản kháng bằng tiếng Anh):** Cách nhận diện "red flags" và vạch rõ ranh giới (set boundaries, call out workplace harassment) khi gặp đồng nghiệp/đàn anh vượt quá giới hạn.
3. Ở cột `evidence`: Trích dẫn thẳng các tiêu đề báo chí / thông tin thực tế vừa tìm được ở trên (không bịa số view ảo).
4. Chọn ứng viên #1 (hoặc ứng viên bám sát nhất toàn cảnh vụ việc) làm `best_id`, giải thích lý do và hỏi người dùng duyệt.

Trả về JSON theo đúng cấu trúc:
{{
  "web_status_note": "Thông báo rõ đã tìm kiếm thực tế trên Web/Báo chí về vụ việc người dùng yêu cầu và trích xuất đúng thông tin vụ việc.",
  "candidates": [
    {{
      "id": 1,
      "topic": "Tên chủ đề bám sát trực diện vụ việc (có tên vụ việc/nhân vật hoặc từ khóa chính)",
      "description": "Mô tả chi tiết tình huống Anna & Tom bàn luận về đúng vụ việc này, cảm xúc, cú twist và thông điệp",
      "platform": "Threads VN / TikTok / Báo chí VN",
      "evidence": "Trích dẫn tiêu đề báo/thông tin thực tế từ kết quả tìm kiếm ở trên",
      "level_reason": "Lý do chủ đề và bộ từ vựng tiếng Anh xoay quanh vụ việc này hợp với trình độ {level.upper()}"
    }}
  ],
  "best_id": 1,
  "best_reason": "Lý do chọn ứng viên này làm đề xuất số 1 và câu hỏi xin ý kiến duyệt từ người dùng."
}}"""
    else:
        user_prompt = f"""Thực hiện quy trình **Chế độ B — AI tự tìm chủ đề đang hot** cho học viên tiếng Anh trình độ **{level.upper()}**.
Mức độ nội dung: {tone_rule}

Tín hiệu tin tức/đời sống Việt Nam thu thập được:
{signals_context}

QUY TẮC BẮT BUỘC:
1. Ưu tiên cao nhất cho các chủ đề, tình huống đời thường, câu nói, tâm sự cảm xúc đang được giới trẻ/người đi làm Việt Nam thảo luận nhiều trên **Threads**, **TikTok**, **YouTube (Shorts & video dài)**, **Facebook Reels** trong 7–14 ngày gần đây.
2. Tuân thủ đúng Mức độ nội dung ở trên ({tone_rule}). Mọi ứng viên phải dễ chuyển thành hội thoại giữa 2 người bạn (Anna & Tom).
3. Quy tắc trung thực số liệu: KHÔNG ĐƯỢC TỰ BỊA số lượt xem (views) hay link giả. Ở cột `evidence`, trích dẫn tín hiệu tin tức ở trên hoặc ghi rõ `"Chưa kiểm chứng số liệu trực tiếp — Đề xuất từ chủ đề nổi bật trên Threads/TikTok"`.
4. Chọn ra 1 ứng viên tốt nhất (`best_id` từ 1 đến 5), giải thích lý do chi tiết vì sao nên làm video này ngay, và dừng lại để hỏi người dùng duyệt.

Trả về JSON theo đúng cấu trúc:
{{
  "web_status_note": "Thông báo rõ ràng về trạng thái truy cập web & kiểm chứng số liệu (trung thực 100%, không bịa số liệu).",
  "candidates": [
    {{
      "id": 1,
      "topic": "Tên chủ đề ngắn gọn, bắt tai (4-8 từ)",
      "description": "Mô tả tình huống đời thường, cảm xúc nhân vật, cú twist nhỏ và thông điệp",
      "platform": "Threads VN / TikTok / YouTube Shorts / FB Reels",
      "evidence": "Ghi rõ trạng thái kiểm chứng (không bịa số view ảo)",
      "level_reason": "Lý do chủ đề và từ vựng này cực hợp với người học trình độ {level.upper()}"
    }}
  ],
  "best_id": 1,
  "best_reason": "Lý do chọn ứng viên này làm đề xuất số 1 và câu hỏi xin ý kiến duyệt từ người dùng."
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
                    "topic": "MẬP MỜ KHÔNG DANH PHẬN (SITUATIONSHIP)",
                    "description": "Anna tâm sự với Tom về một người ngày nào cũng nhắn tin quan tâm suốt 4 tháng nhưng khi hỏi 'chúng mình là gì' thì lại bảo 'cứ để tự nhiên'. Cảm xúc vừa hụt hẫng vừa tỉnh ngộ.",
                    "platform": "Threads VN & TikTok",
                    "evidence": "Chưa kiểm chứng số liệu trực tiếp — Chủ đề tâm sự tình cảm luôn thuộc nhóm tương tác cao nhất trên Threads VN",
                    "level_reason": f"Hội thoại đời thường giàu cảm xúc, câu ngắn gọn, lặp lại tự nhiên cụm từ vựng tình cảm/giao tiếp trình độ {level.upper()}.",
                },
                {
                    "id": 2,
                    "topic": "ÁP LỰC ĐỒNG TRANG LỨA TUỔI 25",
                    "description": "Cảm giác hoang mang khi lướt mạng thấy bạn bè cùng lớp đã mua xe, cưới hỏi, lên sếp trong khi mình vẫn đang loay hoay tìm định hướng.",
                    "platform": "Threads VN & YouTube Podcast",
                    "evidence": "Chưa kiểm chứng số liệu trực tiếp — Chủ đề đồng cảm sâu sắc với sinh viên mới ra trường & người đi làm",
                    "level_reason": f"Từ vựng chủ đề công việc, mục tiêu sống và động viên tinh thần vừa sức với mức {level.upper()}.",
                },
                {
                    "id": 3,
                    "topic": "BẠN THÂN MƯỢN TIỀN MÃI KHÔNG TRẢ",
                    "description": "Tình huống khó xử khi người bạn mượn tiền khất lần nhưng ngày nào cũng đăng story đi cà phê, du lịch sang chảnh.",
                    "platform": "Threads VN & Facebook Reels",
                    "evidence": "Chưa kiểm chứng số liệu trực tiếp — Tình huống tranh luận đời thường rất phổ biến trên Threads",
                    "level_reason": f"Nhiều mẫu câu giao tiếp khéo léo, đòi nợ tinh tế và bày tỏ thái độ ở trình độ {level.upper()}.",
                },
                {
                    "id": 4,
                    "topic": "ĐI LÀM VÌ ĐAM MÊ HAY VÌ TIỀN",
                    "description": "Cuộc tranh luận thực tế giữa việc chọn công việc lương cao nhưng kiệt sức (burnout) hay công việc nhẹ nhàng đủ sống để giữ sức khỏe tinh thần.",
                    "platform": "TikTok & YouTube Shorts",
                    "evidence": "Chưa kiểm chứng số liệu trực tiếp — Chủ đề công sở gần gũi với người học tiếng Anh đi làm",
                    "level_reason": f"Cung cấp bộ từ vựng công sở (work-life balance, burnout, salary) dễ áp dụng ngay cho level {level.upper()}.",
                },
                {
                    "id": 5,
                    "topic": "SỢ GỌI ĐIỆN THOẠI (TELEPHONE PHOBIA)",
                    "description": "Thói quen của người trẻ chỉ thích nhắn tin chứ cực kỳ hồi hộp, tim đập chân run mỗi khi chuông điện thoại reo hoặc phải gọi cho người lạ.",
                    "platform": "Threads VN & TikTok",
                    "evidence": "Chưa kiểm chứng số liệu trực tiếp — Hiện tượng tâm lý đời thường rất dễ gây đồng cảm và hài hước",
                    "level_reason": f"Ngữ cảnh hài hước, câu thoại ngắn, dễ bắt chước ngữ điệu cảm thán khi luyện Shadowing {level.upper()}.",
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


async def generate_5step_screenplay(
    topic: str,
    description: str = "",
    level: str = "B1",
    target_turns: int = 12,
    cta: str = "Đăng ký kênh REACH, ôn lại 5 từ vựng mục tiêu và trả lời câu hỏi ở phần bình luận",
    gemini_api_key: str = "",
    sensitivity_mode: str = "normal",
) -> Dict[str, Any]:
    """
    Thực hiện trọn vẹn PHẦN 3 — YÊU CẦU CỐ ĐỊNH CHO AI (Biên kịch 5 Bước):
      - Bước 1: Nhân vật (2 nhân vật Anna & Tom)
      - Bước 2: Khung kịch bản (Mở bài Hook 10-30s, Thân bài + Cao trào/Twist, Kết bài + CTA)
      - Bước 3: Tự chấm trước khi viết lời thoại (YouTube, TikTok, Thực tế/Cảm xúc + Chỗ đã tự khắc phục)
      - Bước 4: Lời thoại (Xuất đủ 3 bản: Chỉ Anh, Chỉ Việt, Song ngữ Anh-Việt + Bản cắt TikTok kết bằng 'xem trọn bài nghe ở kênh YouTube REACH')
      - Bước 5: Đóng gói (3 gợi ý tựa 'Luyện Nghe Tiếng Anh Level X - CHỦ ĐỀ', 1 câu mô tả Thumbnail, Danh sách từ vựng mục tiêu lặp >= 3 lần)
    """
    lvl = level.strip().upper() or "B1"
    raw_turns = int(target_turns or 14)
    turns = max(14, min(60, raw_turns if raw_turns >= 14 else 16))

    level_spec = {
        "A1": "Câu hội thoại nhập vai CỰC NGẮN (2-6 từ/câu), từ vựng A1 cơ bản, giống hệt phim/đời thực.",
        "A2": "Câu hội thoại nhập vai NGẮN GỌN (3-8 từ/câu), từ vựng đời sống A2, phản xạ trực tiếp.",
        "B1": "Câu hội thoại nhập vai NGẮN & KỊCH TÍNH (3-9 từ/câu), từ vựng giao tiếp B1 tự nhiên, dồn nén cảm xúc.",
        "B2": "Câu hội thoại nhập vai SẮC SẢO (4-10 từ/câu), cụm từ/idioms B2 ngắn gọn, đối đáp căng thẳng/sâu sắc.",
        "C1": "Câu hội thoại nhập vai TINH TẾ (4-11 từ/câu), cấu trúc nhấn mạnh C1 ngắn gọn mà đắt giá.",
        "C2": "Câu hội thoại nhập vai BẢN XỨ (4-11 từ/câu), ngôn từ C2 cô đọng, giàu hàm ý.",
    }.get(lvl, "Câu hội thoại nhập vai ngắn gọn (3-9 từ/câu), giàu cảm xúc.")

    tone_desc = {
        "polite": "LỊCH SỰ (Ngôn từ chuẩn mực, lịch thiệp, an toàn lớp học/công sở, tránh nhạy cảm)",
        "normal": "BÌNH THƯỜNG (Đời thường tự nhiên, gần gũi, chân thật, bám sát trực diện chủ đề người dùng chọn)",
        "sensitive_ok": "CHO PHÉP NHẠY CẢM (Cho phép khai thác trực diện các vụ drama, scandal, quấy rối, ngoại tình, góc khuất tâm lý và tranh cãi mạng xã hội)",
    }.get(sensitivity_mode, "BÌNH THƯỜNG (Đời thường tự nhiên)")

    live_signals = await fetch_live_vn_trend_signals(f"{topic} {description}")
    news_ctx = (
        "\n".join(
            f"- {s['title']}: {s.get('snippet', '')}" for s in live_signals[:5]
        )
        if live_signals
        else "Không có thêm trích đoạn báo chí."
    )

    system_prompt = (
        "Bạn là Biên kịch trưởng cho kênh luyện nghe tiếng Anh 'REACH Podcast'. "
        "Phong cách đặc trưng bắt buộc của kênh: HỘI THOẠI NHẬP VAI TRỰC TIẾP (2 nhân vật là người trong cuộc đang nói chuyện/tỏ tình/đối chất trực tiếp với nhau bằng ngôi I - You), "
        "CÂU CỰC NGẮN VÀ ĐẮT GIÁ (chỉ 2 đến 9 từ mỗi câu để giọng AI đọc chậm rãi, nhấn nhá biểu cảm như người thật). "
        "Chỉ trả về JSON hợp lệ (không có markdown fence)."
    )

    user_prompt = f"""Hãy viết hồ sơ kịch bản đầy đủ 5 bước cho tập luyện nghe tiếng Anh theo đúng phong cách hội thoại câu ngắn nhập vai của kênh REACH:
- Tiêu đề / Ý tưởng: "{topic}"
- Mô tả mong muốn (tình huống, cảm xúc, thông điệp): "{description or topic}"
- Thông tin báo chí / mạng xã hội thực tế liên quan:
{news_ctx}
- Mức độ nội dung: {tone_desc}
- Level: {lvl} ({level_spec})
- Số lượt thoại (turns): {turns} lượt đối đáp qua lại giữa **Anna** (Nữ) và **Tom** (Nam)
- CTA mong muốn ở cuối bài: "{cta}"

YÊU CẦU CỐ ĐỊNH BẮT BUỘC (5 BƯỚC):
- **QUY TẮC VÀNG VỀ LỜI THOẠI (GIỐNG HỆT VIDEO MẪU REACH):**
  1. **Nhập vai người trong cuộc trực tiếp (`I` và `You`):** Anna và Tom KHÔNG đóng vai 2 MC ngồi kể chuyện người khác, mà **nhập vai trực tiếp thành 2 nhân vật chính trong tình huống "{topic}"** đang đối diện nhau (ví dụ: người yêu chất vấn nhau, đôi bạn thân tỏ tình/cãi nhau, vợ chồng đối chất...).
  2. **Câu cực ngắn, dồn nén cảm xúc (2–9 từ/lượt):** Mỗi lượt thoại chỉ dài **2 đến 9 từ** để khi đọc chậm có độ ngân và biểu cảm chân thực.
     *Ví dụ mẫu chuẩn:*
     - `"Why didn't you tell me you were married?"` (`Sao em không nói với anh là em đã kết hôn?`)
     - `"You never asked me."` (`Anh có bao giờ hỏi em đâu.`)
     - `"What do you mean?"` (`Ý em là sao?`)
     - `"I asked if you have a boyfriend!"` (`Anh đã hỏi em có bạn trai chưa mà!`)
     - `"Yes, and I said no."` (`Đúng, và em trả lời là chưa.`)
     - `"But you have a husband!"` (`Nhưng em lại có chồng rồi!`)
     - `"Are you serious right now?"` (`Em đang nói thật đấy à?`)
     *Hoặc mẫu tỏ tình bạn thân:*
     - `"Because I like you."` (`Vì tao thích mày.`)
     - `"What?"` (`Cái gì cơ?`)
     - `"I like you, I like you a lot."` (`Tao thích mày, tao rất thích mày.`)
     - `"Wait, this is sudden."` (`Khoan đã, chuyện này đột ngột quá.`)
     - `"But we are best friends."` (`Nhưng tụi mình là bạn thân mà.`)
  3. **Dịch tiếng Việt (`vi`) sát cảm xúc & xưng hô đời thực:** Dùng xưng hô đúng quan hệ (`Anh - Em` hoặc `Tao - Mày` / `Tớ - Cậu`).
- **Bước 1 — Nhân vật (gọn):** Đúng 2 nhân vật (Anna và Tom). Mỗi người nêu rõ: tên, tuổi, tính cách chính, điều họ muốn, điều cản họ.
- **Bước 2 — Khung kịch bản:** Mở bài (Hook đi thẳng vào câu chất vấn/thú nhận gây sốc) • Thân bài (đối đáp dồn dập đẩy kịch tính) • Cao trào (lật tình huống) • Kết bài.
- **Bước 3 — Tự chấm trước khi viết lời thoại:** Kiểm tra độ ngắn của câu (2-9 từ/câu), từ vựng mục tiêu lặp lại tự nhiên, Hook 3 giây đầu.
- **Bước 4 — Lời thoại ({turns} lượt thoại ngắn 2–9 từ):**
  + Mỗi lượt gồm `speaker` ("Anna" hoặc "Tom"), `en` (100% tiếng Anh, chỉ 2–9 từ, TUYỆT ĐỐI KHÔNG chèn từ tiếng Việt vào `en`, không viết "Anna:" trong `en`), `vi` (câu dịch tiếng Việt ngắn gọn, đúng xưng hô `Anh - Em` hoặc `Tao - Mày`), `is_tiktok_climax` (true cho 6-8 câu cao trào).
- **Bước 5 — Đóng gói:** 3 gợi ý tựa `"Luyện Nghe Tiếng Anh Level {lvl} - CHỦ ĐỀ"`, 1 câu mô tả Thumbnail, 5 từ vựng mục tiêu.

Trả về JSON theo đúng schema sau:
{{
  "title_vi": "TÊN CHỦ ĐỀ NGẮN GỌN IN HOA (3-6 TỪ)",
  "level": "{lvl}",
  "step1_characters": [
    {{
      "name": "Anna",
      "age": "24",
      "personality": "Tính cách chính",
      "desire": "Điều Anna muốn trong tập này",
      "obstacle": "Điều cản trở Anna"
    }},
    {{
      "name": "Tom",
      "age": "25",
      "personality": "Tính cách chính",
      "desire": "Điều Tom muốn trong tập này",
      "obstacle": "Điều cản trở Tom"
    }}
  ],
  "step2_outline": {{
    "opening_hook": "Mở bài (Hook câu đầu gây sốc)...",
    "body_scenes": "Thân bài & tình tiết đối đáp dẫn tới cao trào...",
    "climax_twist": "Cao trào (lật tình huống hoặc cảm xúc)...",
    "ending_cta": "Kết bài..."
  }},
  "step3_self_review": {{
    "youtube_check": "Đánh giá câu ngắn 2-9 từ, nhịp chậm rõ và cách lặp từ vựng mục tiêu...",
    "tiktok_check": "Đánh giá Hook 3 giây đầu và đoạn cao trào...",
    "realism_emotion": "Cảm xúc chủ đạo & độ chân thực...",
    "unresolved_or_fixed": "Chỉ rõ chỗ câu dài đã được cắt gọn thành câu 2-9 từ như thế nào..."
  }},
  "step4_lines": [
    {{
      "speaker": "Tom",
      "en": "Short dramatic English line (2-9 words).",
      "vi": "Câu dịch tiếng Việt ngắn gọn, tự nhiên.",
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
        "word": "từ/cụm từ tiếng Anh",
        "pos": "n/v/adj/phrase",
        "meaning_vi": "nghĩa tiếng Việt",
        "repeat_count": 3
      }}
    ]
  }}
}}"""

    data = await call_llm_json(system_prompt, user_prompt, gemini_api_key)
    lines = data.get("step4_lines") if isinstance(data, dict) else None

    if not lines or not isinstance(lines, list) or len(lines) < 4:
        clean_t = topic.strip().upper() or "CHUYỆN ĐỜI THƯỜNG"
        lines = [
            {"speaker": "Anna", "en": "How could you do this to me?", "vi": "Sao anh có thể làm vậy với em?", "is_tiktok_climax": False},
            {"speaker": "Tom", "en": "Anna, please wait.", "vi": "Anna, nghe anh nói đã.", "is_tiktok_climax": False},
            {"speaker": "Anna", "en": "Don't touch me!", "vi": "Đừng chạm vào tôi!", "is_tiktok_climax": False},
            {"speaker": "Tom", "en": "Let me explain everything.", "vi": "Để anh giải thích tất cả.", "is_tiktok_climax": False},
            {"speaker": "Anna", "en": "Explain what?", "vi": "Giải thích cái gì cơ?", "is_tiktok_climax": True},
            {"speaker": "Tom", "en": "It was just a terrible mistake.", "vi": "Đó chỉ là một sai lầm tồi tệ thôi.", "is_tiktok_climax": True},
            {"speaker": "Anna", "en": "A mistake?!", "vi": "Một sai lầm á?!", "is_tiktok_climax": True},
            {"speaker": "Anna", "en": "She is my best friend!", "vi": "Cô ấy là bạn thân nhất của tôi!", "is_tiktok_climax": True},
            {"speaker": "Tom", "en": "I know. I am so sorry.", "vi": "Anh biết. Anh thực sự xin lỗi.", "is_tiktok_climax": True},
            {"speaker": "Anna", "en": "And now she is pregnant!", "vi": "Và giờ nó đang có bầu đấy!", "is_tiktok_climax": True},
            {"speaker": "Tom", "en": "I never meant to hurt you.", "vi": "Anh chưa bao giờ muốn làm em tổn thương.", "is_tiktok_climax": False},
            {"speaker": "Anna", "en": "We are completely over.", "vi": "Giữa chúng ta chấm hết rồi.", "is_tiktok_climax": False},
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

    for idx, item in enumerate(lines):
        sp = str(item.get("speaker") or ("Anna" if idx % 2 == 0 else "Tom")).strip()
        en = re.sub(r"^(Anna|Tom)\s*:\s*", "", str(item.get("en") or "").strip(), flags=re.IGNORECASE)
        vi = re.sub(r"^(Anna|Tom)\s*:\s*", "", str(item.get("vi") or "").strip(), flags=re.IGNORECASE)
        if not en:
            continue
        en_only_lines.append(f"{sp}: {en}")
        vi_only_lines.append(f"{sp}: {vi}")
        bilingual_lines.append(f"{sp}: {en}\n   -> {vi}")
        custom_script_rows.append(f"{sp}: {en} | {vi}")
        if item.get("is_tiktok_climax"):
            tiktok_climax_lines.append(f"{sp}: {en}\n   -> {vi}")

    if not tiktok_climax_lines and len(bilingual_lines) >= 4:
        mid = len(bilingual_lines) // 2
        tiktok_climax_lines = bilingual_lines[max(0, mid - 2) : min(len(bilingual_lines), mid + 2)]

    tiktok_cut_full = (
        "\n\n".join(tiktok_climax_lines)
        + "\n\n👉 Xem trọn bài nghe ở kênh YouTube REACH"
    )

    from generator import build_thumbnail_prompts

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
