import os
from sqlalchemy.orm import Session
from app.models import SystemSetting

DEFAULT_SETTINGS = {
    "max_chars_per_job": ("3000", "Độ dài ký tự tối đa cho mỗi yêu cầu TTS"),
    "max_concurrency": ("3", "Số lượng job TTS được xử lý song song tối đa"),
    "worker_idle_timeout_minutes": ("15", "Thời gian tự động ngắt kết nối Worker khi nhàn rỗi (phút)"),
    "allow_public_registration": ("true", "Cho phép người dùng tự do đăng ký tài khoản mới"),
    "allow_guest_demo": ("true", "Cho phép khách chưa đăng nhập dùng thử Studio"),
    "default_voice": ("Hải Đăng", "Giọng đọc mặc định khi mở Studio"),
    "default_speed": ("1.0", "Tốc độ đọc mặc định"),
    "default_temp": ("0.7", "Độ biểu cảm mặc định (Temperature)"),
    "fallback_to_local_cpu": ("true", "Cho phép tự động dùng động cơ CPU ONNX khi worker bận"),
    "master_kaggle_username": ("", "Kaggle Username mặc định của hệ thống (dùng chung)"),
    "master_kaggle_key": ("", "Kaggle API Key mặc định của hệ thống (dùng chung)"),
    "google_client_id": ("", "Google OAuth Client ID cho tính năng Đăng nhập & Đăng ký Google"),
    "google_client_secret": ("", "Google OAuth Client Secret"),
    "admin_google_email": ("phucsd@gmail.com", "Email Google mặc định có quyền Quản trị viên (Admin)"),
    "google_redirect_uri": ("", "Tùy chỉnh Redirect URI nếu chạy qua Custom Domain/Proxy"),
    "mcp_require_auth": ("true", "Yêu cầu người dùng kết nối MCP phải xác thực tài khoản qua Magic Link hoặc API Key")
}

class SettingsService:
    @staticmethod
    def get_setting(db: Session, key: str, default: str = None) -> str:
        s = db.query(SystemSetting).filter(SystemSetting.key == key).first()
        if s and s.value is not None:
            return s.value
        if default is not None:
            return default
        if key in DEFAULT_SETTINGS:
            return DEFAULT_SETTINGS[key][0]
        return ""

    @staticmethod
    def get_int(db: Session, key: str, default: int = 0) -> int:
        val = SettingsService.get_setting(db, key, str(default))
        try:
            return int(val)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def get_bool(db: Session, key: str, default: bool = False) -> bool:
        val = SettingsService.get_setting(db, key, "true" if default else "false").lower()
        return val in ("true", "1", "yes", "on")

    @staticmethod
    def get_all_settings(db: Session) -> dict:
        result = {}
        for k, (def_val, desc) in DEFAULT_SETTINGS.items():
            result[k] = def_val
        
        all_db = db.query(SystemSetting).all()
        for item in all_db:
            result[item.key] = item.value
        return result

    @staticmethod
    def set_setting(db: Session, key: str, value: str, description: str = None):
        item = db.query(SystemSetting).filter(SystemSetting.key == key).first()
        if not item:
            item = SystemSetting(
                key=key, 
                value=value, 
                description=description or DEFAULT_SETTINGS.get(key, ("", ""))[1]
            )
            db.add(item)
        else:
            item.value = value
            if description:
                item.description = description
        db.commit()

    @staticmethod
    def update_bulk(db: Session, settings_dict: dict):
        for k, v in settings_dict.items():
            SettingsService.set_setting(db, k, str(v))
