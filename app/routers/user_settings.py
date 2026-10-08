import os
import requests
from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from pathlib import Path
from typing import Optional
from app.database import get_db
from app.models import User, generate_id
from app.services.auth_service import AuthService, get_current_user

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["basename"] = os.path.basename

from app.services.api_key_service import ApiKeyService
from app.services.db_sync_service import DbSyncService

router = APIRouter(tags=["User Settings & BYOK"])

@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, welcome: int = 0, msg: str = None, error: str = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    api_keys = ApiKeyService.list_user_api_keys(db, user.id)
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "page_title": "Cài Đặt Tài Khoản & Kaggle Key - OlokaTTS",
            "user": user,
            "api_keys": api_keys,
            "welcome": bool(welcome),
            "msg": msg,
            "error": error
        }
    )

@router.post("/settings/kaggle")
def update_kaggle_credentials(
    kaggle_username: str = Form(...),
    kaggle_key: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    user.kaggle_username = kaggle_username.strip()
    user.kaggle_key = kaggle_key.strip()
    db.commit()
    DbSyncService.backup_database(immediate=True)
    AuthService.log_audit(db, action="update_kaggle_key", message=f"Đã cập nhật Kaggle API Key", user_id=user.id)
    return RedirectResponse(url="/settings?msg=kaggle_saved", status_code=302)

@router.post("/api/user/kaggle/test")
def test_kaggle_credentials(payload: dict = None, user: User = Depends(get_current_user)):
    k_user = (payload or {}).get("kaggle_username") or user.kaggle_username
    k_key = (payload or {}).get("kaggle_key") or user.kaggle_key

    if not k_user or not k_key:
        raise HTTPException(status_code=400, detail="Vui lòng nhập đầy đủ Kaggle Username và API Key!")

    try:
        # Test authentication against Kaggle API directly
        # Kaggle HTTP Basic Auth with username and api_key
        resp = requests.get(
            "https://www.kaggle.com/api/v1/datasets/list?pageSize=1",
            auth=(k_user.strip(), k_key.strip()),
            timeout=10
        )
        if resp.status_code == 200:
            return {
                "status": "success",
                "message": f"Kết nối Kaggle thành công! Tài khoản: {k_user} (Hợp lệ)"
            }
        elif resp.status_code == 401:
            return {
                "status": "error",
                "message": "Kaggle báo lỗi 401 Unauthorized: Username hoặc API Key không hợp lệ. Vui lòng kiểm tra lại file kaggle.json!"
            }
        else:
            return {
                "status": "error",
                "message": f"Kaggle phản hồi mã HTTP {resp.status_code}: {resp.text[:150]}"
            }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Lỗi kết nối tới máy chủ Kaggle: {str(e)}"
        }

@router.post("/settings/password")
def change_password(
    old_password: str = Form(...),
    new_password: str = Form(...),
    confirm_new_password: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    if not AuthService.verify_password(old_password, user.hashed_password):
        return RedirectResponse(url="/settings?error=wrong_old_password", status_code=302)

    if len(new_password) < 6:
        return RedirectResponse(url="/settings?error=password_too_short", status_code=302)

    if new_password != confirm_new_password:
        return RedirectResponse(url="/settings?error=password_mismatch", status_code=302)

    user.hashed_password = AuthService.hash_password(new_password)
    db.commit()
    DbSyncService.backup_database(immediate=True)
    AuthService.log_audit(db, action="change_password", message="Đã đổi mật khẩu", user_id=user.id)
    return RedirectResponse(url="/settings?msg=password_changed", status_code=302)

@router.post("/api/user/api-keys/create")
async def create_api_key_endpoint(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    # Support both JSON payload and Form data
    content_type = request.headers.get("content-type", "")
    name = "API Key"
    scopes = "tts:generate,voices:read"
    expires_days = None

    if "application/json" in content_type:
        try:
            data = await request.json()
            name = data.get("name") or name
            scopes = data.get("scopes") or scopes
            expires_days = data.get("expires_days")
        except Exception:
            pass
    else:
        form = await request.form()
        name = form.get("name") or name
        scopes = form.get("scopes") or scopes

    api_key_obj, raw_secret = ApiKeyService.create_api_key(
        db=db,
        user_id=user.id,
        name=name,
        scopes=scopes,
        expires_days=int(expires_days) if expires_days else None
    )
    DbSyncService.backup_database(immediate=True)

    if "application/json" in request.headers.get("accept", "") or "application/json" in content_type:
        return {
            "status": "success",
            "key": {
                "id": api_key_obj.id,
                "name": api_key_obj.name,
                "prefix": api_key_obj.key_prefix,
                "scopes": api_key_obj.scopes,
                "created_at": api_key_obj.created_at.strftime("%Y-%m-%d %H:%M")
            },
            "secret": raw_secret
        }

    return RedirectResponse(url="/settings?msg=key_created", status_code=303)

@router.post("/api/user/api-keys/{key_id}/revoke")
def revoke_api_key_endpoint(
    key_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    success = ApiKeyService.revoke_api_key(db, user.id, key_id)
    if not success:
        raise HTTPException(status_code=404, detail="Không tìm thấy API Key hoặc bạn không có quyền thu hồi.")
    DbSyncService.backup_database(immediate=True)
    return {"status": "success", "message": "Đã thu hồi API Key thành công."}

@router.post("/api/user/api-keys/{key_id}/delete")
def delete_api_key_endpoint(
    key_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    success = ApiKeyService.delete_api_key(db, user.id, key_id)
    if not success:
        raise HTTPException(status_code=404, detail="Không tìm thấy API Key hoặc bạn không có quyền xóa.")
    DbSyncService.backup_database(immediate=True)
    return {"status": "success", "message": "Đã xóa API Key thành công."}

@router.post("/settings/regenerate-api-key")
def regenerate_api_key(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    api_key_obj, raw_secret = ApiKeyService.create_api_key(db, user.id, name="Regenerated Key")
    return RedirectResponse(url="/settings?msg=api_key_regenerated", status_code=303)
