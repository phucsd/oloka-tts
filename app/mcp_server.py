"""
OlokaTTS Model Context Protocol (MCP) Server.
Enables AI models (ChatGPT, Claude Desktop, Cursor, Antigravity, Windsurf) 
to discover voices, synthesize 48kHz Vietnamese speech, and check GPU worker status.
"""

import os
import sys
import time
import json
import asyncio
import unicodedata
import requests
from pathlib import Path
from typing import Optional, List, Dict, Any
from mcp.server.fastmcp import FastMCP, Context
from app.database import SessionLocal
from app.config import settings
from app.services.mcp_auth_service import McpAuthService
from app.services.settings_service import SettingsService
from app.services.auth_service import AuthService

def extract_transport_session_id(ctx: Optional[Context] = None) -> Optional[str]:
    """Safely extracts transport-level session identifier from FastMCP Context if available."""
    if not ctx or not hasattr(ctx, "request_context") or not ctx.request_context:
        return None
    try:
        # 1. Direct client_id property on Context (if provided by MCP client)
        c_id = getattr(ctx, "client_id", None)
        if c_id and str(c_id).strip():
            return f"cid_{str(c_id).strip()}"

        req = getattr(ctx.request_context, "request", None)
        if req:
            if hasattr(req, "headers"):
                h_sess = req.headers.get("mcp-session-id")
                if h_sess and h_sess.strip():
                    return f"hdr_{h_sess.strip()}"
                auth_h = req.headers.get("authorization")
                if auth_h and auth_h.strip():
                    return f"auth_{auth_h.strip()[:32]}"
            if hasattr(req, "query_params") and req.query_params.get("session_id"):
                return f"sse_{req.query_params.get('session_id').strip()}"
    except Exception:
        pass
    return None

# Initialize FastMCP Server
mcp = FastMCP(
    "OlokaTTS",
    instructions=(
        "OlokaTTS is a high-fidelity 48kHz Vietnamese Neural Text-to-Speech engine powered by Kaggle Tesla T4 GPUs. "
        "Use this server to generate natural Vietnamese speech, explore 25 curated voice presets across Northern, "
        "Central, and Southern dialects, and control expressiveness using natural emotion tags."
    )
)

# Allow remote AI agents (ChatGPT 2026, Claude, Hugging Face proxy) to connect without DNS rebinding 421 errors
mcp.settings.stateless_http = True
if hasattr(mcp.settings, "transport_security") and mcp.settings.transport_security:
    mcp.settings.transport_security.enable_dns_rebinding_protection = False
    mcp.settings.transport_security.allowed_hosts = ["*"]
    mcp.settings.transport_security.allowed_origins = ["*"]

# Pre-initialize Streamable HTTP application instance
streamable_http_app = mcp.streamable_http_app()

# Default voice catalog with metadata
VOICE_CATALOG = [
    # 11 Editors' Picks
    {"id": "vp_haidang", "name": "Hải Đăng", "gender": "Nam", "region": "Nam", "description": "Trẻ trung, hiện đại, phong cách tự nhiên (Mặc định)", "is_editors_pick": True},
    {"id": "vp_adambua", "name": "Adam bựa", "gender": "Nam", "region": "Bắc", "description": "Hài hước, dí dỏm, độc đáo, tạo tiếng cười", "is_editors_pick": True},
    {"id": "vp_trucly", "name": "Trúc Ly", "gender": "Nữ", "region": "Bắc", "description": "Trong trẻo, lôi cuốn, phong cách tự nhiên", "is_editors_pick": True},
    {"id": "vp_anhkhoi", "name": "Anh Khôi", "gender": "Nam", "region": "Bắc", "description": "Trầm ấm, truyền cảm, phong cách kể chuyện", "is_editors_pick": True},
    {"id": "vp_maianh", "name": "Mai Anh", "gender": "Nữ", "region": "Bắc", "description": "Dịu dàng, chuẩn phát thanh, phong cách tin tức", "is_editors_pick": True},
    {"id": "vp_minhquan", "name": "Minh Quân Pro", "gender": "Nam", "region": "Bắc", "description": "Đĩnh đạc, rõ ràng, phong cách tự nhiên", "is_editors_pick": True},
    {"id": "vp_thuydung", "name": "Thùy Dung", "gender": "Nữ", "region": "Nam", "description": "Thanh thoát, chuyên nghiệp, phong cách tin tức", "is_editors_pick": True},
    {"id": "vp_thientamduc", "name": "Thiền Tâm Đức", "gender": "Nam", "region": "Bắc", "description": "Thong dong, an nhiên, phong cách kể chuyện Phật giáo / tản văn", "is_editors_pick": True},
    {"id": "vp_ngochuyen", "name": "Ngọc Huyền", "gender": "Nữ", "region": "Bắc", "description": "Tự nhiên, trong sáng, thân thiện", "is_editors_pick": True},
    {"id": "vp_quangson", "name": "Quang Sơn", "gender": "Nam", "region": "Trung", "description": "Đậm đà, chân thực, phong cách tự nhiên miền Trung", "is_editors_pick": True},
    {"id": "vp_ngoctran", "name": "Ngọc Trân", "gender": "Nữ", "region": "Trung", "description": "Sâu lắng, ấm áp, phong cách tự nhiên miền Trung", "is_editors_pick": True},
    # 14 Standard Voices
    {"id": "vp_minhduc", "name": "Minh Đức", "gender": "Nam", "region": "Bắc", "description": "Trang trọng, chuẩn mực, phong cách tin tức", "is_editors_pick": False},
    {"id": "vp_phamtuyen", "name": "Phạm Tuyên", "gender": "Nam", "region": "Bắc", "description": "Trầm ấm, phong thái tự nhiên", "is_editors_pick": False},
    {"id": "vp_thaison", "name": "Thái Sơn", "gender": "Nam", "region": "Nam", "description": "Cuốn hút, phong cách kể chuyện", "is_editors_pick": False},
    {"id": "vp_xuanvinh", "name": "Xuân Vĩnh", "gender": "Nam", "region": "Bắc", "description": "Mạnh mẽ, phong cách tự nhiên", "is_editors_pick": False},
    {"id": "vp_thanhbinh", "name": "Thanh Bình", "gender": "Nam", "region": "Bắc", "description": "Điềm tĩnh, phong cách kể chuyện", "is_editors_pick": False},
    {"id": "vp_ngoclinh", "name": "Ngọc Linh", "gender": "Nữ", "region": "Bắc", "description": "Nhẹ nhàng, phong cách kể chuyện", "is_editors_pick": False},
    {"id": "vp_doantrang", "name": "Đoan Trang", "gender": "Nữ", "region": "Bắc", "description": "Nữ tính, đằm thắm, phong cách tự nhiên", "is_editors_pick": False},
    {"id": "vp_thucdoan", "name": "Thục Đoan", "gender": "Nữ", "region": "Nam", "description": "Ngọt ngào, phong cách kể chuyện", "is_editors_pick": False},
    {"id": "vp_minhtriet", "name": "Minh Triết", "gender": "Nam", "region": "Nam", "description": "Sắc bén, phong cách tin tức", "is_editors_pick": False},
    {"id": "vp_myduyen", "name": "Mỹ Duyên", "gender": "Nữ", "region": "Nam", "description": "Êm ái, phong cách đọc truyện", "is_editors_pick": False},
    {"id": "vp_quynhanh", "name": "Quỳnh Anh", "gender": "Nữ", "region": "Bắc", "description": "Truyền cảm, phong cách đọc truyện", "is_editors_pick": False},
    {"id": "vp_ductri", "name": "Đức Trí", "gender": "Nam", "region": "Nam", "description": "Dày dặn, phong cách đọc truyện", "is_editors_pick": False},
    {"id": "vp_kimthanh", "name": "Kim Thanh", "gender": "Nữ", "region": "Nam", "description": "Ấm cúng, phong cách đọc truyện", "is_editors_pick": False},
    {"id": "vp_manhdung", "name": "Mạnh Dũng", "gender": "Nam", "region": "Bắc", "description": "Hào sảng, phong cách tự nhiên", "is_editors_pick": False}
]

def get_base_url() -> str:
    """Detects active Gateway URL (custom env > production domain)."""
    env_url = os.environ.get("OLOKATTS_GATEWAY_URL") or os.environ.get("GATEWAY_URL")
    if env_url:
        return env_url.rstrip("/")

    pub_url = getattr(settings, "PUBLIC_API_BASE_URL", None)
    if pub_url and "localhost" not in pub_url:
        return pub_url.rstrip("/")

    return "https://tts.oloka.net"

def get_auth_headers() -> dict:
    """Returns authorization headers if API key is provided."""
    headers = {"User-Agent": "OlokaTTS-MCP-Client/1.0"}
    api_key = os.environ.get("OLOKATTS_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if api_key and api_key != "not-needed":
        headers["Authorization"] = f"Bearer {api_key}"
    return headers

def remove_diacritics(text: str) -> str:
    """Strip Vietnamese accent marks for tolerant matching."""
    nfkd = unicodedata.normalize('NFKD', text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).replace("đ", "d").replace("Đ", "D")

def resolve_voice_name(voice: str) -> str:
    """Resolves user/LLM input voice name to the exact preset name."""
    v_clean = voice.strip()
    if not v_clean:
        return "Hải Đăng"
    # 1. Exact match
    for item in VOICE_CATALOG:
        if item["name"].lower() == v_clean.lower():
            return item["name"]
    # 2. Accent-insensitive match (e.g. 'Hai Dang' -> 'Hải Đăng')
    v_no_accents = remove_diacritics(v_clean).lower()
    for item in VOICE_CATALOG:
        if remove_diacritics(item["name"]).lower() == v_no_accents:
            return item["name"]
    # 3. ID match (e.g. 'vp_haidang')
    for item in VOICE_CATALOG:
        if item["id"].lower() == v_clean.lower():
            return item["name"]
    return v_clean

# ==============================================================================
# MCP TOOLS
# ==============================================================================

@mcp.tool()
def list_voices(
    region: Optional[str] = None,
    gender: Optional[str] = None,
    editors_pick_only: bool = False
) -> str:
    """
    List available 48kHz Vietnamese neural voice presets with their dialect and characteristics.
    
    Parameters:
      region: Filter by dialect region: 'Bắc' (Northern), 'Trung' (Central), or 'Nam' (Southern).
      gender: Filter by gender: 'Nam' (Male) or 'Nữ' (Female).
      editors_pick_only: If True, only returns featured / highest quality voices.
    """
    filtered = VOICE_CATALOG
    if region:
        r_clean = region.strip().lower()
        filtered = [v for v in filtered if r_clean in v["region"].lower()]
    if gender:
        g_clean = gender.strip().lower()
        filtered = [v for v in filtered if g_clean in v["gender"].lower()]
    if editors_pick_only:
        filtered = [v for v in filtered if v["is_editors_pick"]]

    output = [f"### 🎙️ Danh Sách Giọng Đọc OlokaTTS ({len(filtered)} giọng khả dụng)\n"]
    for v in filtered:
        star = " ⭐ [Nổi Bật]" if v["is_editors_pick"] else ""
        output.append(
            f"- **{v['name']}**{star} | Miền {v['region']} • {v['gender']}\n"
            f"  *Đặc trưng:* {v['description']}\n"
            f"  *ID gọi hàm:* `{v['name']}`"
        )

    output.append("\n💡 *Mẹo: Sử dụng ID giọng (ví dụ: 'Hải Đăng', 'Mai Anh', 'Quang Sơn') cho tham số `voice` trong `generate_speech`.*")
    return "\n".join(output)


@mcp.tool()
def link_account(
    pair_code: Optional[str] = None,
    session_token: Optional[str] = None,
    ctx: Optional[Context] = None
) -> str:
    """
    Check authentication status or get a Magic Pairing Link to securely link your OlokaTTS account with your MCP client.
    
    Parameters:
      pair_code: Optional pairing code (e.g. 'OLK-8291') to check or verify.
      session_token: Optional persistent session token returned from previous link_account call.
    """
    db = SessionLocal()
    try:
        base_url = get_base_url()
        transport_sid = extract_transport_session_id(ctx)

        # 1. Check if already authenticated via session_token, transport_session_id, or pair_code
        user = McpAuthService.resolve_caller(
            db,
            pair_code=pair_code.strip() if pair_code else None,
            session_token=session_token.strip() if session_token else None,
            transport_session_id=transport_sid
        )
        if user:
            gpu_status_desc = McpAuthService.get_user_gpu_status_description(db, user)
            token_hint = f"- **Mã xác thực phiên (Session Token):** `{session_token.strip()}`\n" if session_token else ""
            return (
                f"✅ **TÀI KHOẢN ĐÃ ĐƯỢC XÁC THỰC THÀNH CÔNG!**\n\n"
                f"- **Tài khoản liên kết:** `{user.username}` ({user.email})\n"
                f"- **Vai trò:** `{user.role.upper()}`\n"
                f"{token_hint}"
                f"- **Hạ tầng GPU:** {gpu_status_desc}\n\n"
                f"Bạn có thể sử dụng tất cả các công cụ OlokaTTS bình thường!"
            )

        # 2. If explicit pair_code or session_token was provided but not yet approved
        if pair_code and pair_code.strip():
            sess = McpAuthService.get_pairing_session(db, pair_code.strip())
            if sess:
                if sess.status == "pending":
                    auth_url = f"{base_url}/mcp/pair?code={sess.code}"
                    return (
                        f"⏳ **Mã phiên `{sess.code}` đang chờ cấp quyền trên trình duyệt.**\n\n"
                        f"👉 Vui lòng nhấp vào liên kết sau để đăng nhập và bấm 'Xác Nhận & Cấp Quyền':\n"
                        f"[{auth_url}]({auth_url})\n\n"
                        f"- **Mã xác thực phiên (Session Token):** `{sess.session_token}`\n\n"
                        f"Sau khi xác nhận trên trình duyệt, hãy bảo tôi kiểm tra lại nhé!"
                    )
                elif sess.status in ("expired", "revoked"):
                    return (
                        f"❌ **Mã phiên `{pair_code.strip()}` đã {sess.status}.**\n\n"
                        f"Vui lòng gọi lại `link_account()` không kèm mã cũ để tạo phiên ghép đôi mới."
                    )

        if session_token and session_token.strip():
            sess = McpAuthService.get_session_by_token(db, session_token.strip())
            if sess:
                if sess.status == "pending":
                    auth_url = f"{base_url}/mcp/pair?code={sess.code}"
                    return (
                        f"⏳ **Phiên của bạn đang chờ cấp quyền trên trình duyệt.**\n\n"
                        f"👉 Vui lòng nhấp vào liên kết sau để đăng nhập và bấm 'Xác Nhận & Cấp Quyền':\n"
                        f"[{auth_url}]({auth_url})\n\n"
                        f"- **Mã ghép đôi:** `{sess.code}`\n\n"
                        f"Sau khi xác nhận trên trình duyệt, hãy bảo tôi kiểm tra lại nhé!"
                    )
                elif sess.status in ("expired", "revoked"):
                    return (
                        f"❌ **Phiên xác thực đã {sess.status}.**\n\n"
                        f"Vui lòng gọi lại `link_account()` để tạo phiên ghép đôi mới."
                    )

        # 3. Check if there is an existing pending session strictly for THIS transport
        if transport_sid:
            pending_sess = McpAuthService.get_pending_session_for_transport(db, transport_sid)
            if pending_sess:
                auth_url = f"{base_url}/mcp/pair?code={pending_sess.code}"
                return (
                    f"🔗 **LIÊN KẾT TÀI KHOẢN OLOKATTS VỚI MCP CLIENT:**\n\n"
                    f"👉 **Vui lòng nhấp vào liên kết sau để đăng nhập và cấp quyền:**\n"
                    f"[{auth_url}]({auth_url})\n\n"
                    f"- **Mã phiên của bạn:** `{pending_sess.code}`\n"
                    f"- **Mã xác thực phiên (Session Token):** `{pending_sess.session_token}`\n\n"
                    f"*(Sau khi bạn đăng nhập trên trình duyệt và bấm 'Xác Nhận & Cấp Quyền', hãy nhắn lại cho tôi biết nhé!)*"
                )

        # 4. Generate new session strictly for this caller/transport
        new_sess = McpAuthService.create_pairing_session(
            db,
            client_name="MCP Client",
            transport_session_id=transport_sid
        )
        auth_url = f"{base_url}/mcp/pair?code={new_sess.code}"
        return (
            f"🔗 **LIÊN KẾT TÀI KHOẢN OLOKATTS VỚI PHIÊN MCP:**\n\n"
            f"👉 **Vui lòng nhấp vào liên kết sau để đăng nhập và cấp quyền:**\n"
            f"[{auth_url}]({auth_url})\n\n"
            f"- **Mã phiên của bạn:** `{new_sess.code}`\n"
            f"- **Mã xác thực phiên (Session Token):** `{new_sess.session_token}`\n\n"
            f"*(Sau khi đăng nhập trên trình duyệt và bấm 'Xác Nhận', hãy nhắn lại cho tôi biết nhé!)*"
        )
    finally:
        db.close()


@mcp.tool()
async def generate_speech(
    prompt: str,
    voice: str = "Hải Đăng",
    speed: float = 1.0,
    temperature: float = 0.7,
    pair_code: Optional[str] = None,
    session_token: Optional[str] = None,
    api_key: Optional[str] = None,
    save_to_file: bool = True,
    output_path: Optional[str] = None,
    ctx: Optional[Context] = None
) -> str:
    """
    Synthesize high-fidelity 48kHz Vietnamese speech from text using OlokaTTS Neural Workers.
    
    Parameters:
      prompt: Vietnamese text to read. You can embed natural emotion tags to control voice acting:
              - [cười] (chuckle / laugh)
              - [thở dài] (sigh)
              - [thì thầm] (whisper)
              - [ngập ngừng] (hesitation)
              - [0.5s] or [1.0s] (pause)
      voice: Name of voice preset (e.g., 'Hải Đăng', 'Mai Anh', 'Anh Khôi', 'Trúc Ly', 'Quang Sơn', 'Ngọc Trân', 'Adam bựa').
      speed: Speaking speed multiplier (0.5 to 2.0, default 1.0).
      temperature: Neural voice expressiveness / prosody variation (0.1 to 1.5, default 0.7).
      pair_code: Optional pairing code (e.g. 'OLK-xxxxxx') if explicit binding is used.
      session_token: Optional persistent session token from approved pairing session.
      api_key: Optional API key (oloka_live_...) for account attribution.
      save_to_file: If True, downloads and saves the generated .wav audio locally.
      output_path: Optional local destination file path.
    """
    base_url = get_base_url()
    voice = resolve_voice_name(voice)
    db = SessionLocal()
    auth_user_id = None
    auth_username = None
    auth_role = None
    auth_token = None

    try:
        transport_sid = extract_transport_session_id(ctx)
        authenticated_user = McpAuthService.resolve_caller(
            db,
            pair_code=pair_code.strip() if pair_code else None,
            session_token=session_token.strip() if session_token else None,
            transport_session_id=transport_sid,
            api_key=api_key.strip() if api_key else None
        )
        require_auth = SettingsService.get_bool(db, "mcp_require_auth", default=True)

        if authenticated_user:
            auth_user_id = authenticated_user.id
            auth_username = authenticated_user.username
            auth_role = (authenticated_user.role or "user").upper()
            auth_token = AuthService.create_token(
                auth_user_id,
                auth_username,
                authenticated_user.role or "user",
                expires_in_days=1
            )

        if require_auth and not authenticated_user:
            # Check if caller passed a pair code
            if pair_code and pair_code.strip():
                sess = McpAuthService.get_pairing_session(db, pair_code.strip())
                if sess and sess.status == "pending":
                    auth_url = f"{base_url}/mcp/pair?code={sess.code}"
                    return (
                        f"🔒 **YÊU CẦU CẤP QUYỀN TRÊN TRÌNH DUYỆT (MÃ PHIÊN: `{sess.code}`):**\n\n"
                        f"Phiên kết nối `{sess.code}` của bạn đang chờ bạn xác nhận trên trình duyệt web.\n\n"
                        f"👉 **Vui lòng nhấp vào liên kết sau để đăng nhập & cấp quyền:**\n"
                        f"[{auth_url}]({auth_url})\n\n"
                        f"- **Mã xác thực phiên (Session Token):** `{sess.session_token}`\n\n"
                        f"*(Lưu ý cho AI Assistant: Sau khi người dùng xác nhận trên trình duyệt, bạn BẮT BUỘC phải gọi lại hàm `generate_speech` với tham số `pair_code=\"{sess.code}\"` hoặc `session_token=\"{sess.session_token}\"`).* "
                    )
                elif sess and sess.status in ("expired", "revoked"):
                    return (
                        f"❌ **Mã ghép đôi `{pair_code.strip()}` đã {sess.status}.**\n\n"
                        f"Vui lòng gọi lại hàm `generate_speech` mà không truyền mã cũ để nhận phiên ghép đôi mới."
                    )

            if session_token and session_token.strip():
                sess = McpAuthService.get_session_by_token(db, session_token.strip())
                if sess and sess.status == "pending":
                    auth_url = f"{base_url}/mcp/pair?code={sess.code}"
                    return (
                        f"🔒 **YÊU CẦU CẤP QUYỀN TRÊN TRÌNH DUYỆT:**\n\n"
                        f"Phiên kết nối của bạn đang chờ bạn xác nhận trên trình duyệt web.\n\n"
                        f"👉 **Vui lòng nhấp vào liên kết sau để đăng nhập & cấp quyền:**\n"
                        f"[{auth_url}]({auth_url})\n\n"
                        f"- **Mã phiên:** `{sess.code}`\n\n"
                        f"*(Sau khi người dùng xác nhận trên trình duyệt, hãy gọi lại hàm `generate_speech` với `session_token=\"{sess.session_token}\"`).* "
                    )
                elif sess and sess.status in ("expired", "revoked"):
                    return (
                        f"❌ **Phiên xác thực đã {sess.status}.**\n\n"
                        f"Vui lòng tạo phiên ghép đôi mới qua `link_account()`."
                    )

            # Generate or get pending session strictly for this caller/transport
            sess_to_use = None
            if transport_sid:
                sess_to_use = McpAuthService.get_pending_session_for_transport(db, transport_sid)
            if not sess_to_use:
                sess_to_use = McpAuthService.create_pairing_session(db, client_name="MCP Client", transport_session_id=transport_sid)

            auth_url = f"{base_url}/mcp/pair?code={sess_to_use.code}"
            return (
                f"🔒 **YÊU CẦU XÁC THỰC TÀI KHOẢN OLOKATTS:**\n\n"
                f"Để sử dụng công cụ tạo giọng nói Neural 48kHz và bảo vệ tài nguyên GPU, "
                f"bạn cần liên kết tài khoản OlokaTTS với phiên trò chuyện này:\n\n"
                f"👉 **Vui lòng nhấp vào liên kết sau để đăng nhập & cấp quyền:**\n"
                f"[{auth_url}]({auth_url})\n\n"
                f"- **Mã ghép đôi:** `{sess_to_use.code}`\n"
                f"- **Mã xác thực phiên (Session Token):** `{sess_to_use.session_token}`\n\n"
                f"*(Lưu ý cho AI Assistant: Sau khi người dùng xác nhận trên trình duyệt, bạn BẮT BUỘC phải gọi lại hàm `generate_speech` với tham số `pair_code=\"{sess_to_use.code}\"` hoặc `session_token=\"{sess_to_use.session_token}\"`).* "
            )


        # ----------------------------------------------------------------------
        # Direct In-Process Execution (Zero HTTP Loopback Deadlocks on Uvicorn)
        # ----------------------------------------------------------------------
        try:
            from app.services.job_service import JobService
            from app.models import TTSJob

            job = JobService.create_job(
                db=db,
                prompt=prompt.strip(),
                voice_type="preset",
                voice_id=voice.strip(),
                speed=float(speed),
                temperature=float(temperature),
                user_id=auth_user_id
            )

            if job.status == "failed":
                return f"❌ Lỗi khởi tạo yêu cầu giọng nói: {job.error_message}"

            job_id = job.id
            job_status = job.status
            job_audio_path = job.audio_path
            job_duration = job.duration

            # Release initial db write session to eliminate SQLite locks on /api/worker/jobs/pull
            db.close()

            # Poll for completion directly - wait until worker synthesizes speech (up to 110s for cold boot)
            start_time = time.time()
            max_wait = 110
            final_job_status = job_status
            final_audio_path = job_audio_path
            final_duration = job_duration
            final_error = None

            while time.time() - start_time < max_wait:
                await asyncio.sleep(1)
                # Fresh, lightweight read that closes immediately
                check_db = SessionLocal()
                final_exec_acc_id = None
                final_worker_id = None
                try:
                    row = check_db.query(
                        TTSJob.status, TTSJob.error_message, TTSJob.audio_path, TTSJob.duration,
                        TTSJob.execution_account_id, TTSJob.worker_id
                    ).filter(TTSJob.id == job_id).first()
                    if row:
                        final_job_status = row[0]
                        final_error = row[1]
                        final_audio_path = row[2]
                        final_duration = row[3]
                        final_exec_acc_id = row[4]
                        final_worker_id = row[5]
                        if final_job_status in ("completed", "failed"):
                            break
                finally:
                    check_db.close()

            if final_job_status == "failed":
                return f"❌ Lỗi xử lý từ GPU worker: {final_error or 'Không xác định'}"

            if final_job_status != "completed":
                return (
                    f"⏱️ **Tác vụ `#{job_id}` chưa hoàn tất sau {max_wait} giây.** "
                    f"Trạng thái hiện tại: `{final_job_status}`. "
                    f"Kaggle GPU Worker có thể đang khởi động lại hoặc gặp trục trặc mạng. Vui lòng thử lại sau giây lát."
                )

            # Audio generation completed successfully
            audio_file_path = final_audio_path
            audio_bytes = b""
            if audio_file_path and os.path.exists(audio_file_path):
                with open(audio_file_path, "rb") as f:
                    audio_bytes = f.read()

            saved_file_str = ""
            if save_to_file or output_path:
                out_dir = Path("./output_audio").resolve()
                out_dir.mkdir(parents=True, exist_ok=True)
                if output_path:
                    safe_filename = Path(output_path).name
                    safe_filename = "".join(c for c in safe_filename if c.isalnum() or c in ('-', '_', '.')).strip(" .")
                    if not safe_filename.lower().endswith(".wav"):
                        safe_filename += ".wav"
                    target_path = (out_dir / safe_filename).resolve()
                else:
                    ts = int(time.time())
                    clean_voice = "".join(c for c in voice if c.isalnum() or c in (' ', '_')).strip().replace(' ', '_')
                    target_path = (out_dir / f"oloka_{clean_voice}_{ts}.wav").resolve()
                if audio_bytes:
                    with open(target_path, "wb") as f:
                        f.write(audio_bytes)
                    saved_file_str = f"- **Đường dẫn tệp cục bộ:** `{str(target_path)}`\n"

            est_duration = final_duration or max(0.5, round(len(audio_bytes) / 96000.0, 1))
            user_str = f"- **Tài khoản xác thực:** `{auth_username}` ({auth_role})\n" if auth_username else ""
            
            gpu_res_str = ""
            if final_exec_acc_id:
                read_db = SessionLocal()
                try:
                    from app.models import KaggleExecutionAccount
                    k_acc = read_db.query(KaggleExecutionAccount).filter(KaggleExecutionAccount.id == final_exec_acc_id).first()
                    if k_acc:
                        if auth_role == "ADMIN":
                            gpu_res_str = f"- **Tài nguyên GPU:** Master Admin Kaggle Dual T4 (@{k_acc.kaggle_username})\n"
                        else:
                            gpu_res_str = f"- **Tài nguyên GPU:** Kaggle Cá Nhân BYOK (@{k_acc.kaggle_username})\n"
                finally:
                    read_db.close()
            elif final_worker_id == "local_cpu":
                gpu_res_str = "- **Tài nguyên:** Local CPU ONNX\n"

            public_audio_url = f"{base_url}/v1/tts/jobs/{job_id}/audio"

            return (
                f"✅ **ĐÃ TẠO GIỌNG NÓI THÀNH CÔNG!**\n\n"
                f"- **Giọng đọc:** {voice}\n"
                f"- **Định dạng:** 48kHz WAV PCM (Studio Quality)\n"
                f"- **Thời lượng:** ~{est_duration:.1f} giây\n"
                f"- **Dung lượng tệp:** {len(audio_bytes) / 1024:.1f} KB\n"
                f"- **Nghe trực tiếp:** [{public_audio_url}]({public_audio_url})\n"
                f"{user_str}"
                f"{gpu_res_str}"
                f"{saved_file_str}"
                f"- **Máy chủ xử lý:** `{base_url}`\n\n"
                f"💬 *Nội dung đã đọc:* \"{prompt[:120]}{'...' if len(prompt) > 120 else ''}\""
            )

        except Exception as in_proc_err:
            print(f"⚠️ [MCP] In-process execution exception: {in_proc_err}, falling back to HTTP...")

        # Fallback to HTTP call if in-process is unavailable (e.g. CLI stdio on another machine)
        headers = get_auth_headers()
        headers["Content-Type"] = "application/json"
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        elif api_key and api_key.strip():
            headers["Authorization"] = f"Bearer {api_key.strip()}"

        url = f"{base_url}/v1/audio/speech"
        payload = {
            "model": "olokatts-v3-turbo",
            "input": prompt.strip(),
            "voice": voice.strip(),
            "speed": float(speed)
        }

        response = requests.post(url, json=payload, headers=headers, timeout=120)
        if response.status_code != 200:
            return (
                f"❌ Lỗi tạo giọng nói từ OlokaTTS Gateway (Mã {response.status_code}):\n"
                f"{response.text[:300]}\n"
                f"Endpoint: {url}"
            )

        audio_bytes = response.content
        saved_file_str = ""
        if save_to_file or output_path:
            out_dir = Path("./output_audio").resolve()
            out_dir.mkdir(parents=True, exist_ok=True)
            if output_path:
                safe_filename = Path(output_path).name
                safe_filename = "".join(c for c in safe_filename if c.isalnum() or c in ('-', '_', '.')).strip(" .")
                if not safe_filename.lower().endswith(".wav"):
                    safe_filename += ".wav"
                target_path = (out_dir / safe_filename).resolve()
            else:
                ts = int(time.time())
                clean_voice = "".join(c for c in voice if c.isalnum() or c in (' ', '_')).strip().replace(' ', '_')
                target_path = (out_dir / f"oloka_{clean_voice}_{ts}.wav").resolve()
            with open(target_path, "wb") as f:
                f.write(audio_bytes)
            saved_file_str = f"- **Đường dẫn tệp cục bộ:** `{str(target_path)}`\n"


        est_duration = max(0.5, round(len(audio_bytes) / 96000.0, 1))
        user_str = f"- **Tài khoản xác thực:** `{auth_username}` ({auth_role})\n" if auth_username else ""

        return (
            f"✅ **Đã tạo giọng nói thành công!**\n\n"
            f"- **Giọng đọc:** {voice}\n"
            f"- **Định dạng:** 48kHz WAV PCM (Studio Quality)\n"
            f"- **Thời lượng ước tính:** ~{est_duration} giây\n"
            f"- **Dung lượng tệp:** {len(audio_bytes) / 1024:.1f} KB\n"
            f"{user_str}"
            f"{saved_file_str}"
            f"- **Máy chủ xử lý:** `{base_url}`\n\n"
            f"💬 *Nội dung đã đọc:* \"{prompt[:120]}{'...' if len(prompt) > 120 else ''}\""
        )

    except requests.exceptions.Timeout:
        return "⏱️ Yêu cầu tạo giọng nói bị quá thời gian chờ (Timeout 120s). Kaggle GPU Worker có thể đang khởi động lại hoặc bận xử lý hàng đợi."
    except Exception as e:
        return f"❌ Lỗi kết nối tới OlokaTTS Gateway ({base_url}): {str(e)}"
    finally:
        try:
            db.close()
        except Exception:
            pass


@mcp.tool()
def get_system_status() -> str:
    """
    Check the operational status of OlokaTTS Gateway, Kaggle Tesla T4 GPU Workers, and queue health.
    """
    base_url = get_base_url()

    # 1. Direct in-process database lookup (0ms latency, zero HTTP loopback deadlocks on Uvicorn)
    try:
        db = SessionLocal()
        try:
            from datetime import datetime, timedelta
            from app.models import WorkerSession, TTSJob
            from app.services.local_engine import LocalEngine
            from app.services.job_service import JobService

            # Clean up stale jobs while checking status
            JobService.cleanup_stale_jobs(db, max_age_minutes=5)

            cutoff = datetime.utcnow() - timedelta(seconds=90)
            live_workers = db.query(WorkerSession).filter(
                WorkerSession.status.in_(["starting", "ready", "busy"]),
                WorkerSession.last_heartbeat_at >= cutoff
            ).count()
            pending_jobs = db.query(TTSJob).filter(
                TTSJob.status.in_(["queued", "booting_kaggle", "processing"])
            ).count()
            local_avail = LocalEngine.is_available()
            status_indicator = "🟢 SẴN SÀNG" if (live_workers > 0 or local_avail) else "🟡 ĐANG CHỜ WORKER"

            return (
                f"### ⚡ Trạng Thái Hệ Thống OlokaTTS ({status_indicator})\n\n"
                f"- **Máy chủ Gateway:** `{base_url}`\n"
                f"- **GPU Worker online:** {live_workers} GPU (Tesla T4 16GB)\n"
                f"- **Động cơ Local CPU ONNX:** {'Khả dụng' if local_avail else 'Chưa kích hoạt'}\n"
                f"- **Job đang chờ trong hàng đợi:** {pending_jobs} jobs\n"
            )
        finally:
            db.close()
    except Exception:
        pass

    # 2. Fallback to HTTP call if running outside the Gateway process (e.g. standalone CLI)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }
    try:
        resp = requests.get(f"{base_url}/api/status", headers=headers, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            live_workers = data.get("live_worker_count", 0)
            pending_jobs = data.get("pending_jobs", 0)
            local_avail = data.get("local_engine", False)
            status_indicator = "🟢 SẴN SÀNG" if (live_workers > 0 or local_avail) else "🟡 ĐANG CHỜ WORKER"

            return (
                f"### ⚡ Trạng Thái Hệ Thống OlokaTTS ({status_indicator})\n\n"
                f"- **Máy chủ Gateway:** `{base_url}`\n"
                f"- **GPU Worker online:** {live_workers} GPU (Tesla T4 16GB)\n"
                f"- **Động cơ Local CPU ONNX:** {'Khả dụng' if local_avail else 'Chưa kích hoạt'}\n"
                f"- **Job đang chờ trong hàng đợi:** {pending_jobs} jobs\n"
            )
        return f"⚠️ Gateway phản hồi mã HTTP {resp.status_code}. Máy chủ: `{base_url}`"
    except Exception as e:
        return f"❌ Không thể kết nối tới Gateway tại `{base_url}`: {str(e)}"


@mcp.tool()
def estimate_speech_duration(text: str, speed: float = 1.0) -> str:
    """
    Estimates the speech duration, word count, and character count of a Vietnamese text snippet.
    
    Parameters:
      text: The text to evaluate.
      speed: The playback speed multiplier (default 1.0).
    """
    clean_text = text.strip()
    chars = len(clean_text)
    words = len(clean_text.split())
    # Vietnamese average reading rate is ~3.2 words per second at 1.0x speed
    eff_speed = max(0.2, float(speed))
    seconds = max(1, round(words / (3.2 * eff_speed)))

    mins = seconds // 60
    rem_secs = seconds % 60
    time_str = f"{mins} phút {rem_secs} giây" if mins > 0 else f"{seconds} giây"

    return (
        f"📊 **Ước Tính Âm Thanh:**\n"
        f"- Ký tự: **{chars:,}** ký tự\n"
        f"- Số từ: **{words:,}** từ\n"
        f"- Tốc độ: **{speed:.2f}x**\n"
        f"- Thời lượng dự kiến: **~{time_str}** (~{seconds}s)"
    )

# ==============================================================================
# MCP RESOURCES & PROMPTS
# ==============================================================================

@mcp.resource("olokatts://voices")
def get_voices_resource() -> str:
    """Returns raw JSON catalog of all 25 Vietnamese voice presets."""
    return json.dumps(VOICE_CATALOG, ensure_ascii=False, indent=2)

@mcp.resource("olokatts://system-status")
def get_status_resource() -> str:
    """Returns current system health status."""
    return get_system_status()

@mcp.prompt()
def vietnamese_storyteller(story_topic: str) -> str:
    """Generate a Vietnamese dramatic story formatted with natural OlokaTTS emotion tags."""
    return (
        f"Hãy viết một câu chuyện ngắn đầy cảm xúc về chủ đề: '{story_topic}'.\n\n"
        "Yêu cầu:\n"
        "1. Sử dụng tiếng Việt biểu cảm, văn phong tự nhiên.\n"
        "2. Đặt các thẻ cảm xúc của OlokaTTS vào đúng ngữ cảnh để diễn tả giọng nói sống động:\n"
        "   - [cười] khi nhân vật vui vẻ\n"
        "   - [thở dài] khi buồn bã hoặc bất lực\n"
        "   - [thì thầm] khi bí mật hoặc hồi hộp\n"
        "   - [ngập ngừng] khi do dự\n"
        "   - [0.5s] hoặc [1.0s] để tạo khoảng lặng kịch tính.\n"
        "3. Đề xuất giọng đọc phù hợp (ví dụ: 'Anh Khôi' hoặc 'Thái Sơn' cho giọng nam trầm ấm kể chuyện)."
    )

@mcp.prompt()
def vietnamese_news_anchor(news_topic: str) -> str:
    """Format a news bulletin ready for formal Vietnamese radio or television TTS broadcast."""
    return (
        f"Hãy soạn thảo bản tin thời sự phát thanh chuẩn mực về: '{news_topic}'.\n\n"
        "Yêu cầu:\n"
        "1. Văn phong báo chí trang trọng, cô đọng, khách quan.\n"
        "2. Sử dụng dấu câu chuẩn xác, ngắt câu rõ ràng bằng [0.3s] giữa các luận điểm.\n"
        "3. Đề xuất sử dụng giọng đọc 'Mai Anh' (Nữ phát thanh chuẩn Bắc) hoặc 'Minh Đức' (Nam tin tức trang trọng)."
    )

if __name__ == "__main__":
    # When run directly from CLI (e.g. by Claude Desktop or Cursor), runs stdio transport
    mcp.run()
