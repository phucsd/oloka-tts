---
title: OlokaTTS Gateway
emoji: 🎙️
colorFrom: purple
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# 🎙️ OlokaTTS - Vietnamese Neural TTS Gateway & Studio (48kHz)

Hệ thống Text-to-Speech (TTS) tiếng Việt thế hệ mới với âm thanh chuẩn phòng thu **48 kHz**, hỗ trợ **Instant Voice Cloning**, **Emotion Tags**, và điều phối song song trên **GPU Kaggle Tesla T4 x 2** kết hợp **Local CPU ONNX Fallback**.

Dự án được kế thừa và nâng cấp toàn diện từ kinh nghiệm thực chiến của `Omnivoice Gateway`.

---

## ✨ Điểm Nổi Bật

1. **Âm Thanh Trung Thực 48 kHz:** Sử dụng mô hình **VieNeu-TTS v3 Turbo** (Phạm Nguyễn Ngọc Bảo) với codec `MOSS-Audio-Tokenizer-Nano` và g2p `sea-g2p`, chất lượng âm thanh 48kHz sắc nét vượt trội so với chuẩn 24kHz thông thường.
2. **25 Giọng Đọc Đa Dạng Tích Hợp Sẵn:** 10 giọng Editors' picks (Phạm Tuyên, Mai Anh, Hải Đăng, Minh Quân, Trúc Ly, v.v.) đủ 3 miền Bắc – Trung – Nam, không bắt buộc phải có file audio mẫu.
3. **Instant Voice Cloning (3–8s):** Tải lên hoặc ghi âm giọng nói 3–8 giây; hệ thống tự động khử nhiễu reference audio và nhân bản giọng đọc tức thì.
4. **Biểu Cảm & Cảm Xúc Linh Hoạt:** Hỗ trợ chèn trực tiếp các tag cảm xúc vào văn bản: `[cười]`, `[thở dài]`, `[hắng giọng]`.
5. **Khai Thác Tối Đa Kaggle Dual Tesla T4 (2x16GB VRAM):** Tự động khởi chạy 2 tiến trình worker độc lập trên `CUDA:0` và `CUDA:1`, tăng gấp đôi công suất xử lý.
6. **Khắc Phục Lỗi Kaggle `COMPLETE`:** Tự động phát hiện khi phiên Kaggle kết thúc sau 9h và kích hoạt `kaggle kernels push` để sinh phiên GPU mới mà không làm gián đoạn job.
7. **Hybrid Fallback (Local CPU ONNX):** Tích hợp động cơ ONNX Runtime chạy mượt trên CPU (RTF ≈ 0.5) cho phép nghe thử hoặc tạo câu ngắn tức thì mà không cần chờ GPU khởi động.
8. **Chuẩn OpenAI Drop-in:** Hỗ trợ endpoint `POST /v1/audio/speech` tương thích trực tiếp với OpenAI SDK, HyperFrames, LangChain và các AI Agent.
9. **Fullstack All-In-One:** Giao diện Web Studio hiện đại (FastAPI + Jinja2 + Tailwind + WaveSurfer.js) đóng gói toàn bộ trong một ứng dụng Python duy nhất.

---

## 📁 Cấu Trúc Dự Án

```text
Oloka TTS/
├── app/
│   ├── config.py                   # Pydantic Settings & Kaggle credentials
│   ├── database.py                 # SQLite WAL engine
│   ├── models.py                   # SQLAlchemy DB models (Jobs, Presets, Samples, Sessions)
│   ├── schemas.py                  # Pydantic schemas validation
│   ├── main.py                     # FastAPI app & Lifespan
│   ├── routers/
│   │   ├── web.py                  # Jinja2 Fullstack web routes (/, /voices, /conversation, /monitor)
│   │   ├── tts.py                  # REST API cho TTS jobs
│   │   ├── voices.py               # API 25 presets & Voice cloning samples
│   │   ├── internal_worker.py      # Worker pull/push/heartbeat APIs
│   │   ├── openai_speech.py        # Drop-in POST /v1/audio/speech
│   │   └── admin.py                # Admin status & Kaggle push trigger
│   ├── services/
│   │   ├── job_service.py          # Quản lý hàng đợi và trạng thái Job
│   │   ├── kaggle_orchestrator.py  # Giám sát Kaggle & Tự động push kernel
│   │   ├── kaggle_notebook_builder.py # Sinh mã worker và metadata cho Kaggle
│   │   └── local_engine.py         # Động cơ dự phòng Local ONNX CPU
│   └── templates/
│       ├── base.html               # Layout kính mờ (Glassmorphism), Tailwind, WaveSurfer
│       ├── index.html              # Studio chính (Editor, Emotion chips, Waveform)
│       ├── voices.html             # Thư viện 25 giọng & Voice Clone upload
│       ├── conversation.html       # Studio hội thoại đa nhân vật
│       └── monitor.html            # Bảng theo dõi Kaggle Dual T4 theo thời gian thực
├── kaggle_worker/
│   ├── worker.py                   # Script worker điều phối song song 2 GPU T4
│   └── kernel-metadata.json        # Cấu hình GPU NvidiaTeslaT4 trên Kaggle
├── storage/
│   ├── audio/                      # Chứa file WAV 48kHz thành phẩm
│   └── samples/                    # Chứa mẫu giọng clone
├── .env.example
├── .env
├── requirements.txt
└── run.py                          # Lệnh khởi chạy duy nhất
```

---

## 🚀 Hướng Dẫn Cài Đặt & Khởi Chạy

### 1. Cài đặt môi trường
Khuyến nghị sử dụng Python 3.10 – 3.12:

```bash
cd "E:\Phuc's Data\Github\Oloka TTS"
python -m venv .venv

# Windows PowerShell:
.venv\Scripts\Activate.ps1

# Cài đặt thư viện:
pip install -r requirements.txt
```

### 2. Cấu hình file `.env`
File `.env` đã được cấu hình sẵn thông tin Kaggle API. Nếu cần thay đổi:

```ini
PUBLIC_API_BASE_URL=http://localhost:8000
WORKER_TOKEN=vieneu_secure_worker_token_2026

KAGGLE_USERNAME=phcnguynhukendykerry
KAGGLE_KEY=KGAT_b36826720acb8581235c6e78f725fae7
KAGGLE_KERNEL_REF=phcnguynhukendykerry/vieneu-worker
KAGGLE_ACCELERATOR=nvidiaTeslaT4
```

### 3. Khởi động Web Studio
Chỉ cần chạy 1 lệnh duy nhất:

```bash
python run.py
```

Truy cập trình duyệt tại: **http://localhost:8000**
- 🎙️ **Studio chính:** `http://localhost:8000/`
- 👥 **Thư viện giọng & Clone:** `http://localhost:8000/voices`
- 💬 **Hội thoại đa nhân vật:** `http://localhost:8000/conversation`
- 📊 **Giám sát Kaggle Worker:** `http://localhost:8000/monitor`
- 📖 **API Docs (Swagger):** `http://localhost:8000/docs`

---

## ⚡ Vận Hành Worker Kaggle (Tesla T4 x 2)

Hệ thống hỗ trợ 2 cách vận hành GPU Worker:

### Cách 1: Tự Động Hoàn Toàn (Khuyên Dùng)
Khi bạn tạo job trên Web Studio hoặc qua API:
1. Nếu chưa có worker nào online, Gateway sẽ tự động gọi Kaggle API để build và push kernel `phcnguynhukendykerry/vieneu-worker` lên Kaggle.
2. Bạn cũng có thể bấm nút **"Khởi Động / Re-push Worker Kaggle"** ngay trên trang Giám Sát (`/monitor`).
3. Kaggle sẽ cấp phiên GPU Dual T4, tự động cài đặt `vieneu[cuda]` và chạy song song 2 worker trên GPU 0 và GPU 1.

### Cách 2: Chạy Thủ Công Trên Kaggle Notebook
1. Mở Kaggle và tạo một Notebook mới với Accelerator: **GPU T4 x 2** và bật **Internet: On**.
2. Sao chép toàn bộ nội dung file `kaggle_worker/worker.py` dán vào Notebook.
3. Cập nhật biến `GATEWAY_URL` thành Public URL của Gateway (nếu deploy lên VPS / Cloud) và chạy cell.

---

## 🔌 Tích Hợp OpenAI Speech API (`POST /v1/audio/speech`)

Ví dụ gọi API bằng Python (chuẩn OpenAI SDK):

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="not-needed"
)

response = client.audio.speech.create(
    model="vieneu-v3-turbo",
    voice="Phạm Tuyên",
    input="Xin chào! [cười] Đây là âm thanh chất lượng cao 48kHz tạo từ VieNeu Gateway."
)

response.stream_to_file("output.wav")
```
