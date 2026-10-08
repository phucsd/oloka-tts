"""
Script tiện ích đặt lại mật khẩu tài khoản Admin cho OlokaTTS Gateway.
Cách dùng:
    python reset_admin.py [mật_khẩu_mới]

Ví dụ:
    python reset_admin.py
    (Mặc định sẽ đặt lại về: Admin@123456)

    python reset_admin.py MatKhauMoi123!
"""

import sys
import os

# Đảm bảo UTF-8 trên Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from app.database import SessionLocal, Base, engine
from app.models import User
from app.services.auth_service import AuthService
from app.config import settings

def reset_admin(new_password: str = None):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    
    import secrets
    password = new_password or settings.ADMIN_DEFAULT_PASSWORD or secrets.token_urlsafe(16)

    try:
        admin = db.query(User).filter((User.username == "admin") | (User.role == "admin")).first()
        
        if not admin:
            admin = User(
                username="admin",
                email="admin@olokatts.com",
                hashed_password=AuthService.hash_password(password),
                role="admin",
                is_active=True,
                kaggle_username=settings.KAGGLE_USERNAME,
                kaggle_key=settings.KAGGLE_KEY
            )
            db.add(admin)
            db.commit()
            db.refresh(admin)
            print("==================================================")
            print(" ĐÃ TẠO TÀI KHOẢN QUẢN TRỊ ADMIN MỚI THÀNH CÔNG!")
            print(f" Username : {admin.username}")
            print(f" Email    : {admin.email}")
            print(f" Mật khẩu : {password}")
            print(f" Vai trò  : {admin.role}")
            print("==================================================")
        else:
            admin.username = "admin"
            admin.role = "admin"
            admin.is_active = True
            admin.hashed_password = AuthService.hash_password(password)
            db.commit()
            print("==================================================")
            print(" ĐÃ RESET MẬT KHẨU TÀI KHOẢN ADMIN THÀNH CÔNG!")
            print(f" Username : {admin.username}")
            print(f" Email    : {admin.email}")
            print(f" Mật khẩu : {password}")
            print(f" Trạng thái: Hoạt động (is_active=True)")
            print("==================================================")
            
        AuthService.log_audit(
            db,
            action="admin_password_reset",
            message=f"Đã reset mật khẩu tài khoản admin thành công qua CLI script",
            user_id=admin.id
        )
    finally:
        db.close()

if __name__ == "__main__":
    new_pwd = sys.argv[1] if len(sys.argv) > 1 else None
    reset_admin(new_pwd)
