FROM python:3.11-slim

# Install system dependencies (ffmpeg, libsndfile1 for audio processing)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    ffmpeg \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

# Hugging Face Spaces requires user with UID 1000
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user
ENV PATH=/home/user/.local/bin:$PATH

WORKDIR $HOME/app

# Copy requirements and install dependencies
COPY --chown=user requirements.txt $HOME/app/requirements.txt
RUN pip install --no-cache-dir --user -r requirements.txt

# Copy all project files
COPY --chown=user . $HOME/app

# Set environment variables for HF
ENV PYTHONUTF8=1
ENV PYTHONUNBUFFERED=1
ENV PORT=7860
ENV HOST=0.0.0.0

# Ensure storage directories exist with proper permissions
RUN mkdir -p $HOME/app/storage/audio $HOME/app/storage/samples

EXPOSE 7860

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]
