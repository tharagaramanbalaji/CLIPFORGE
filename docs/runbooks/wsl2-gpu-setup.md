# Runbook: WSL2 + Docker Desktop with GPU (one-time setup)

Needs admin rights and one reboot.

1. **Install WSL2** in an elevated PowerShell:
   ```powershell
   wsl --install -d Ubuntu-24.04
   ```
   Reboot, then open "Ubuntu" from the Start menu once to create your Linux user.

2. **Install Docker Desktop** from docker.com. In Settings:
   - General → "Use the WSL 2 based engine" (on)
   - Resources → WSL integration → enable for Ubuntu-24.04
   - Resources → Advanced: give it at least 8 GB RAM (16 GB recommended for Phase 2+)

3. **NVIDIA driver:** your Windows driver (610.88) already provides CUDA to WSL2. Do **not** install a Linux NVIDIA driver inside WSL.

4. **Verify GPU in Docker** (PowerShell or Ubuntu shell):
   ```bash
   docker run --rm --gpus all nvidia/cuda:12.6.3-base-ubuntu24.04 nvidia-smi
   ```
   The RTX 3060 should be listed.

5. **Start ClipForge:**
   ```bash
   cd clipforge
   cp .env.example .env
   docker compose up --build -d
   docker compose ps
   ```

6. **Phase 0 acceptance checks:**
   ```bash
   curl http://localhost:8000/api/v1/ready          # {"status":"ready",...}
   docker compose exec worker-gpu python -m clipforge.cli env      # gpus listed, ffmpeg ok, nvenc true
   docker compose exec worker-render python -m clipforge.cli env
   docker compose exec api pytest                   # unit tests inside Linux
   ```

## Notes
- Media is stored in the `clipforge-data` Docker volume (inside WSL2), not on `C:\`, for speed.
- If `nvidia-smi` works on Windows but not in a container, update Docker Desktop and run `wsl --update`.
