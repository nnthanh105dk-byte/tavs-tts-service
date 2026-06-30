"""
TAVS TTS Microservice
======================
Microservice nhỏ, nhiệm vụ duy nhất: nhận danh sách câu lời thoại tiếng
Việt + tên giọng đọc, dùng edge-tts (thư viện cộng đồng, miễn phí, dựa
trên dịch vụ "Đọc to văn bản" của Microsoft Edge) để sinh audio, đồng
thời tự tính timestamp cho từng câu để xuất file phụ đề SRT.

CONTRACT với WordPress plugin TAVS (xem includes/class-tavs-tts-engine.php):
  Request:  POST /tts
            { "lines": ["Câu 1", "Câu 2", ...], "voice": "vi-VN-HoaiMyNeural" }
  Response: { "audio_base64": "<base64 của file MP3>", "srt": "<nội dung file .srt>" }

Vì sao gộp toàn bộ "lines" thành 1 file audio duy nhất thay vì sinh
từng câu riêng rồi ghép: edge-tts cho khoảng nghỉ tự nhiên hơn khi đọc
liền mạch, và WordPress chỉ cần lưu đúng 1 file MP3 + 1 file SRT cho
mỗi video, đơn giản hoá toàn bộ luồng dữ liệu phía sau (Render Studio).
"""

import asyncio
import base64
import io
import logging

import edge_tts
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tavs-tts")

app = FastAPI(title="TAVS TTS Microservice")

# CORS: WordPress (PHP server-side) gọi vào đây qua wp_remote_post(), không
# phải từ trình duyệt — về lý thuyết không cần CORS. Nhưng vẫn bật để
# phòng trường hợp sau này gọi trực tiếp từ JS phía client, và để bạn dễ
# test bằng công cụ như Postman/trình duyệt mà không bị chặn.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def health_check():
    """
    Endpoint kiểm tra microservice còn sống — dùng để bạn tự test sau khi
    deploy (mở URL này trên trình duyệt, thấy JSON nghĩa là deploy thành công),
    KHÔNG dùng để giữ service không ngủ (Render free tier vẫn sleep sau 15
    phút không hoạt động — đây là giới hạn chấp nhận được vì TAVS chỉ gọi
    TTS khi bạn chủ động bấm nút, không cần phản hồi tức thì).
    """
    return {"status": "ok", "service": "tavs-tts-microservice"}


@app.post("/tts")
async def generate_tts(request: Request):
    """
    Sinh audio + SRT cho toàn bộ danh sách câu.

    Nhận JSON thô (không dùng pydantic model) và tự validate bằng tay —
    quyết định kỹ thuật này nhằm tránh phụ thuộc pydantic v2 (vốn cần
    biên dịch Rust qua maturin, dễ lỗi trên môi trường build có quyền
    ghi hạn chế như Render free tier). Đơn giản hơn nhưng đủ an toàn cho
    1 microservice nội bộ chỉ phục vụ chính plugin TAVS.
    """
    body = await request.json()

    lines = body.get("lines")
    voice = body.get("voice", "vi-VN-HoaiMyNeural")

    if not isinstance(lines, list) or len(lines) == 0:
        raise HTTPException(status_code=400, detail="Trường 'lines' phải là danh sách câu, không được rỗng.")
    if not all(isinstance(line, str) and line.strip() for line in lines):
        raise HTTPException(status_code=400, detail="Mỗi phần tử trong 'lines' phải là chuỗi ký tự không rỗng.")
    if not isinstance(voice, str) or not voice.strip():
        voice = "vi-VN-HoaiMyNeural"

    try:
        audio_bytes, srt_content = await synthesize_with_timestamps(lines, voice)
    except Exception as exc:  # noqa: BLE001 — microservice nhỏ, log đủ chi tiết là đủ
        logger.exception("Lỗi khi sinh TTS")
        raise HTTPException(status_code=502, detail=f"Lỗi edge-tts: {exc}") from exc

    return {
        "audio_base64": base64.b64encode(audio_bytes).decode("utf-8"),
        "srt": srt_content,
    }


async def synthesize_with_timestamps(lines: list[str], voice: str) -> tuple[bytes, str]:
    """
    Gọi edge-tts cho TỪNG câu riêng biệt (không gộp thành 1 đoạn văn bản
    dài) để lấy được thời lượng chính xác của mỗi câu — đây là cách duy
    nhất để tính đúng timestamp SRT khớp với từng câu, vì edge-tts không
    trả về timestamp theo câu nếu gộp chung. Sau đó nối các đoạn audio
    lại bằng cách ghép bytes MP3 tuần tự (hoạt động ổn với MP3 vì đây là
    định dạng cho phép nối frame liên tiếp).
    """
    audio_segments: list[bytes] = []
    srt_entries: list[str] = []
    current_time_ms = 0

    for index, line in enumerate(lines, start=1):
        segment_bytes, duration_ms = await synthesize_single_line(line, voice)
        audio_segments.append(segment_bytes)

        start_ms = current_time_ms
        end_ms = current_time_ms + duration_ms
        srt_entries.append(
            f"{index}\n"
            f"{format_srt_timestamp(start_ms)} --> {format_srt_timestamp(end_ms)}\n"
            f"{line}\n"
        )
        current_time_ms = end_ms

    full_audio = b"".join(audio_segments)
    full_srt = "\n".join(srt_entries)
    return full_audio, full_srt


async def synthesize_single_line(text: str, voice: str) -> tuple[bytes, int]:
    """
    Sinh audio cho 1 câu, trả về (bytes MP3, thời lượng ước tính mili-giây).

    edge-tts hỗ trợ stream kèm "WordBoundary" event cho biết thời điểm
    từng từ — ta dùng event cuối cùng để biết tổng thời lượng audio chính
    xác, thay vì ước lượng thô theo độ dài text (vốn không chính xác với
    tiếng Việt do tốc độ đọc khác nhau giữa câu ngắn/dài).
    """
    communicate = edge_tts.Communicate(text, voice)
    audio_buffer = io.BytesIO()
    last_offset_100ns = 0
    last_duration_100ns = 0

    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_buffer.write(chunk["data"])
        elif chunk["type"] == "WordBoundary":
            # offset + duration tính bằng đơn vị 100-nanosecond (chuẩn edge-tts)
            last_offset_100ns = chunk["offset"]
            last_duration_100ns = chunk["duration"]

    # Thời điểm kết thúc của từ cuối cùng ≈ thời lượng audio.
    # Cộng thêm 300ms đệm để tránh cắt cụt âm cuối câu khi ghép SRT.
    end_100ns = last_offset_100ns + last_duration_100ns
    duration_ms = int(end_100ns / 10_000) + 300

    return audio_buffer.getvalue(), duration_ms


def format_srt_timestamp(ms: int) -> str:
    """Định dạng mili-giây thành timestamp chuẩn SRT: HH:MM:SS,mmm"""
    hours, remainder = divmod(ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"
