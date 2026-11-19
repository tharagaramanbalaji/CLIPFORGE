# One image for every backend role (api, workers, migrations). CUDA runtime + cuDNN for
# faster-whisper/pyannote (Phase 2+); Ubuntu's FFmpeg build includes NVENC and libass.
FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    NVIDIA_DRIVER_CAPABILITIES=compute,video,utility

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3.12 python3.12-venv ffmpeg fonts-dejavu-core ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && python3.12 -m venv $VIRTUAL_ENV

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install -e ".[dev]"
COPY . .

RUN useradd --create-home --uid 10001 clipforge && mkdir -p /data && chown clipforge /data
USER clipforge

EXPOSE 8000
CMD ["uvicorn", "clipforge.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
