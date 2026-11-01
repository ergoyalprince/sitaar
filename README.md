# SITAAR: Every Sound Has a Story

AI stem separation (Demucs), 5-band equalizer, waveforms and spectrum, tempo/key analysis,
a live voice tutor and PDF reports. One server, one command.

## Run (local)
Requirements: Python 3.10+, FFmpeg on PATH (`ffmpeg -version`), internet on first run (downloads the Demucs model, about 80 MB, and the browser libraries from CDNs).

    cd backend
    python -m venv .venv
    source .venv/bin/activate        # Windows: .venv\Scripts\activate
    pip install -r requirements.txt
    uvicorn main:app --port 8000

Open http://localhost:8000 (use localhost or https so the microphone works).

## Run (Docker, with PostgreSQL)
    docker compose up --build
Open http://localhost:8000

## How to use
1. Separation Studio: choose a song, pick 4 or 6 stems, press Separate. A 4-minute song takes roughly 1 to 3 minutes on a GPU and 5 to 15 minutes on a CPU.
2. Mix with volume, Mute (M) and Solo (S). Presets: Full mix, Karaoke, Acapella, Drums only. Download any stem or the current mix as WAV.
3. Audio Laboratory: open any audio file for waveform, spectrum, tempo estimate and the equalizer (the equalizer also processes the stem mixer).
4. Voice Tutor: pick a target note, sing into the mic and follow the live advice.
5. Report: creates a PDF of everything above.

## Configuration (environment variables)
DATABASE_URL (default SQLite), SITAAR_DATA, SITAAR_MAX_MB (100), SITAAR_TTL_HOURS (24, auto-delete),
SITAAR_DEVICE (cuda or cpu), SITAAR_WORKERS (1).

## API
POST /api/jobs (file, stems=4|6) · GET /api/jobs/{id} · GET /api/jobs/{id}/stems/{name} · DELETE /api/jobs/{id} · GET /api/health

## Structure
backend/main.py (API, job queue, Demucs, analysis) · backend/static/index.html (interface using Tone.js, WaveSurfer.js, Teoria, pdf-lib, Zod)

Use only audio you own or have permission to process.
# sitaar
