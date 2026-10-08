# 🚀 Kế Hoạch Triển Khai: Hệ Thống Quản Lý API Key & Trang Tài Liệu API Documentation

Tài liệu này vạch ra lộ trình kỹ thuật chi tiết để nâng cấp OlokaTTS từ nền tảng Studio cục bộ thành một **Nền tảng Voice-AI Cloud mở** sẵn sàng cho lập trình viên và doanh nghiệp tích hợp.

---

## 🎯 Mục Tiêu Cốt Lõi

1. **Hệ thống Quản lý API Key Chuyên Nghiệp:**
   - Hỗ trợ tạo **nhiều API Key** theo dự án (Mobile app, Web, Bot, Automation).
   - Đặt tên, phân quyền (Scopes), ngày hết hạn, xem lần dùng cuối, thu hồi (Revoke) và cấp lại tức thì.
   - Định dạng chuẩn công nghiệp: `oloka_live_xxxxxxxxxxxxxxxxxxxxxxxx`.
   - Xác thực bắt buộc và ghi nhận số lượt gọi (Usage Analytics) vào từng key.
   - Admin Portal có bảng giám sát và quản lý tập trung toàn bộ API Key hệ thống.

2. **Trang Tài Liệu API Documentation Chuyên Sâu (`/docs/api`):**
   - Thiết kế theo tiêu chuẩn ElevenLabs / Stripe Developer Docs (Dark ink trên canvas thanh lịch, mã code đa ngôn ngữ 1-chạm sao chép).
   - Đặc tả 100% các endpoint: Text-to-Speech Streaming, Async Job Queue, Voice Cloning API, Voice Catalog, và System Health.
   - Code mẫu đầy đủ cho: **Python**, **JavaScript / TypeScript (Node.js)**, **cURL**, **PHP**, **Go**.
   - Bảng mã lỗi HTTP và giải pháp xử lý chi tiết.

---

## 🏗️ Kiến Trúc Hệ Thống & Cơ Sở Dữ Liệu

### 1. Model Mới: `ApiKey` (`app/models.py`)
Thay thế trường `api_key` đơn lẻ trong bảng `User` bằng một bảng quan hệ riêng:

```python
class ApiKey(Base):
    __tablename__ = "api_keys"

    id = Column(String(64), primary_key=True)          # key_uuid
    user_id = Column(String(64), ForeignKey("users.id"), index=True, nullable=False)
    name = Column(String(128), nullable=False)         # Tên gợi nhớ: "Chatbot Zalo", "App Mobile"
    key_prefix = Column(String(16), nullable=False)    # "oloka_live_abc..." (hiển thị công khai)
    key_hash = Column(String(256), nullable=False, unique=True, index=True) # SHA-256 hash của secret
    scopes = Column(String(256), default="tts:generate,voices:read") # Phân quyền
    is_active = Column(Boolean, default=True)          # Đang kích hoạt hay bị thu hồi (Revoked)
    last_used_at = Column(DateTime, nullable=True)     # Thời điểm gọi API gần nhất
    total_requests = Column(Integer, default=0)        # Tổng lượt request đã xử lý
    total_characters = Column(Integer, default=0)      # Tổng số ký tự đã tổng hợp
    expires_at = Column(DateTime, nullable=True)       # Ngày hết hạn (tùy chọn)
    created_at = Column(DateTime, default=datetime.utcnow)
```

### 2. Chuẩn Mã Khóa (API Key Secret Format)
- Định dạng: `oloka_live_<32_hex_characters>` (Ví dụ: `oloka_live_9f8a3c4b1e2d0a8b7c6d5e4f3a2b1c0d`).
- **Nguyên tắc bảo mật Stripe/OpenAI:** Mã khóa đầy đủ **chỉ hiển thị một lần duy nhất** ngay sau khi tạo kèm nút Copy. Database chỉ lưu mã băm **SHA-256** và `key_prefix` (8 ký tự đầu + `...` + 4 ký tự cuối) để nhận diện.

---

## 📋 Lộ Trình Triển Khai (4 Giai Đoạn)

### Giai Đoạn 1: Cơ Sở Dữ Liệu & Service Xác Thực API Key (Backend)
- [ ] **Tạo bảng `api_keys`** và tự động migrate dữ liệu: chuyển đổi các `user.api_key` cũ thành key đầu tiên mang tên *"Default Key"*.
- [ ] **Xây dựng `ApiKeyService`**:
  - `generate_key(user_id, name, scopes, expires_days)`: Tạo key mới, băm lưu DB, trả về secret 1 lần.
  - `validate_key(secret_token)`: Băm token, tra cứu DB, kiểm tra `is_active`, hạn dùng, tăng bộ đếm `total_requests` và cập nhật `last_used_at`.
  - `revoke_key(user_id, key_id)`: Thu hồi ngay lập tức.
  - `list_user_keys(user_id)`: Liệt kê danh sách kèm metadata sử dụng.
- [ ] **Nâng cấp Middleware / Dependency Xác Thực:**
  - Cập nhật `get_current_user_optional` và `get_api_key_auth` để gắn `request.state.api_key`.
  - Bảo vệ endpoint `/v1/audio/speech` và `/v1/tts/jobs`: Yêu cầu API Key hợp lệ và khấu trừ ký tự vào tài khoản người tạo.

---

### Giai Đoạn 2: Giao Diện Quản Lý API Key Cho Người Dùng (`/settings/api-keys`)
- [ ] **Tab Quản Lý API Keys trong trang Cài Đặt:**
  - Nút **"Tạo API Key Mới"** mở Modal nhập Tên gợi nhớ và chọn Quyền (Chỉ đọc giọng, Tạo giọng nói, Toàn quyền).
  - Modal thông báo **"Lưu khóa bí mật của bạn"** với nút 1-chạm sao chép, cảnh báo khóa sẽ không hiển thị lại lần thứ hai.
  - Bảng danh sách Keys:
    * Tên key & Tiền tố (`oloka_live_9f8a...1c0d`)
    * Trạng thái (Đang hoạt động / Đã thu hồi)
    * Ngày tạo & Lần dùng cuối
    * Số request đã phục vụ
    * Nút hành động: **Thu Hồi (Revoke)** và **Xóa (Delete)**.

---

### Giai Đoạn 3: Quản Trị Viên Giám Sát API Key (`/admin/api-keys`)
- [ ] **Bảng điều khiển API Keys cho Admin:**
  - Thống kê toàn hệ thống: Tổng số Key đang hoạt động, Tổng lượt gọi API trong 24h/7 ngày.
  - Bảng tra cứu danh sách toàn bộ Key thuộc mọi người dùng: Lọc theo User, trạng thái, thời gian.
  - Quyền hạn Admin: Khóa khẩn cấp (Emergency Revoke) bất kỳ API key nào có hành vi lạm dụng hoặc spam GPU.
  - Ghi nhật ký Audit Log mỗi khi có thao tác tạo, thu hồi hoặc khóa key.

---

### Giai Đoạn 4: Xây Dựng Trang Tài Liệu API Documentation (`/docs/api`)
- [ ] **Giao diện Developer Portal cao cấp:**
  - Thanh Menu bên trái (Sidebar Navigation) phân mục rõ ràng.
  - Vùng nội dung ở giữa giải thích chi tiết, kèm bảng thông số tham số query/body.
  - Cột mã nguồn bên phải (Sticky Code Block) hỗ trợ chuyển đổi linh hoạt: **cURL**, **Python (requests)**, **Python (openai SDK)**, **Node.js (fetch)**, **PHP**, **Go**.
- [ ] **Nội dung chi tiết từng Endpoint:**
  1. **Authentication:** Cách truyền `Authorization: Bearer oloka_live_...`.
  2. **POST `/v1/audio/speech` (OpenAI Drop-in):** Tạo audio nhanh, tương thích trực tiếp các thư viện AI có sẵn.
  3. **POST `/v1/tts/jobs` (Async Queue):** Dành cho văn bản dài, podcast, sách nói, có điều phối GPU Dual Tesla T4.
  4. **GET `/v1/tts/jobs/{id}`:** Polling tiến độ, nhận link WAV 48kHz khi hoàn tất.
  5. **GET `/v1/voices`:** Lấy danh sách 25 giọng đọc (Bắc, Trung, Nam) kèm sample audio.
  6. **POST `/v1/voices/clone`:** Gửi file âm thanh mẫu (3–8s) để nhân bản giọng đọc qua API.
  7. **Emotion Tags Guide:** Hướng dẫn lồng ghép các thẻ cảm xúc `[cười]`, `[thở dài]`, `[thì thầm]`, `[0.5s]`.
  8. **Error Handling & Rate Limits:** Bảng mã lỗi HTTP và JSON trả về chuẩn mực.

---

## ⏱️ Thời Gian Triển Khai Dự Kiến

| Giai Đoạn | Nội Dung Thực Hiện | Trạng Thái |
| :--- | :--- | :--- |
| **P1** | Database `api_keys` model, băm SHA-256 & Service xác thực | ⏳ Sẵn sàng thực hiện |
| **P2** | Giao diện User Quản lý API Key (Tạo, Xem 1 lần, Revoke) | ⏳ Sẵn sàng thực hiện |
| **P3** | Trang Quản trị Admin giám sát API Key toàn hệ thống | ⏳ Sẵn sàng thực hiện |
| **P4** | Trang Tài liệu API Documentation (`/docs/api`) đa ngôn ngữ | ⏳ Sẵn sàng thực hiện |
| **P5** | Kiểm thử luồng gọi API thực tế & Sync lên Hugging Face Space | ⏳ Sẵn sàng thực hiện |
