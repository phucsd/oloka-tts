import json
from pathlib import Path

worker_py_path = Path("kaggle_worker/worker.py")
worker_code = worker_py_path.read_text(encoding="utf-8")

nb_content = {
    "cells": [
        {
            "cell_type": "markdown",
            "metadata": {},
            "source": [
                "# 🎙️ VieNeu-TTS Dual Tesla T4 Worker Daemon\n",
                "Tự động kết nối với VieNeu Gateway tại: `https://tts.oloka.net`"
            ]
        },
        {
            "cell_type": "code",
            "execution_count": None,
            "metadata": {},
            "outputs": [],
            "source": ["%%writefile worker.py\n"] + [line + "\n" for line in worker_code.splitlines()]
        },
        {
            "cell_type": "code",
            "execution_count": None,
            "metadata": {},
            "outputs": [],
            "source": [
                "# 2. Khởi chạy worker bằng python unbuffered\n",
                "!python -u worker.py\n"
            ]
        }
    ],
    "metadata": {
        "language_info": {
            "name": "python",
            "version": "3.10"
        },
        "kernelspec": {
            "display_name": "Python 3",
            "language": "python",
            "name": "python3"
        }
    },
    "nbformat": 4,
    "nbformat_minor": 2
}

out_nb = Path("kaggle_worker_remote/vieneu-tts-dual-t4-worker.ipynb")
with open(out_nb, "w", encoding="utf-8") as f:
    json.dump(nb_content, f, indent=2)

print("✅ Đã tạo xong notebook vieneu-tts-dual-t4-worker.ipynb chuẩn!")
