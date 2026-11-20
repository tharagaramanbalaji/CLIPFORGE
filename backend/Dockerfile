# One image for every backend role (api, workers, migrations). CUDA runtime + cuDNN for
# faster-whisper/pyannote; Ubuntu's FFmpeg build includes NVENC and libass.
FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    NVIDIA_DRIVER_CAPABILITIES=compute,video,utility \
    HF_HOME=/models/hf \
    TORCH_HOME=/models/torch \
    NLTK_DATA=/models/nltk

# Plain-HTTP (port 80) access to archive.ubuntu.com is refused on this network, while HTTPS works
# (the NVIDIA repo fetches fine). Point apt at an HTTPS mirror; override with --build-arg if needed.
# espeak-ng is only used to synthesize test speech with known word timings (Phase 2 accuracy test).
ARG UBUNTU_MIRROR=https://mirrors.edge.kernel.org/ubuntu
RUN sed -i -E "s#https?://(archive|security)\.ubuntu\.com/ubuntu/?#${UBUNTU_MIRROR}/#g" \
        /etc/apt/sources.list.d/ubuntu.sources \
 && apt-get update \
 && apt-get install -y --no-install-recommends python3.12 python3.12-venv ffmpeg fonts-dejavu-core \
        ca-certificates espeak-ng \
 && rm -rf /var/lib/apt/lists/* \
 && python3.12 -m venv $VIRTUAL_ENV

WORKDIR /app
# 1) Heavy deps (torch/CUDA wheels, WhisperX, pyannote) in their own layer, keyed only on this small file,
#    so editing pyproject.toml or code never re-downloads them.
COPY requirements/heavy.txt requirements/heavy.txt
RUN pip install -r requirements/heavy.txt

# 2) Later system packages go here, after the heavy layer. libpython: torchcodec (pyannote audio I/O).
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpython3.12t64 \
 && rm -rf /var/lib/apt/lists/*

# 3) The project and its light deps, against a stub package (code is bind-mounted in dev anyway).
COPY pyproject.toml ./
RUN mkdir -p src/clipforge && echo '__version__ = "0"' > src/clipforge/__init__.py \
 && pip install -e ".[dev,asr,analysis]"
COPY . .

# /data (media) and /models (HF/torch caches) are named volumes; creating them here gives the volumes
# the right owner on first use.
RUN useradd --create-home --uid 10001 clipforge && mkdir -p /data /models && chown clipforge /data /models
USER clipforge

EXPOSE 8000
CMD ["uvicorn", "clipforge.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
