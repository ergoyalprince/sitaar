"""SITAAR backend: FastAPI + Demucs source separation + SQLAlchemy job store."""
import os, re, sys, json, uuid, shutil, subprocess, threading, importlib.util, datetime as dt
from pathlib import Path
from typing import List, Optional
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, String, Integer, Text, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker

BASE = Path(__file__).parent
DATA = Path(os.getenv("SITAAR_DATA", BASE / "data")); DATA.mkdir(parents=True, exist_ok=True)
DB_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA / 'sitaar.db'}")
MAX_MB = int(os.getenv("SITAAR_MAX_MB", "100"))
TTL_H = int(os.getenv("SITAAR_TTL_HOURS", "24"))
DEVICE = os.getenv("SITAAR_DEVICE", "")  # "cuda" / "cpu" / blank = auto
ALLOWED = {".mp3", ".wav", ".flac", ".m4a", ".ogg"}
MODELS = {
    "4": ("htdemucs", ["vocals", "drums", "bass", "other"]),
    "6": ("htdemucs_6s", ["vocals", "drums", "bass", "guitar", "piano", "other"]),
}

engine = create_engine(DB_URL, connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {}, pool_pre_ping=True)
Session = sessionmaker(bind=engine)
Base = declarative_base()


class Job(Base):
    __tablename__ = "jobs"
    id = Column(String(32), primary_key=True)
    filename = Column(String(255))
    model = Column(String(4))
    status = Column(String(16), default="queued")  # queued | running | done | error
    progress = Column(Integer, default=0)
    error = Column(Text, default="")
    analysis = Column(Text, default="{}")
    created = Column(DateTime, default=dt.datetime.utcnow)


Base.metadata.create_all(engine)
app = FastAPI(title="SITAAR API")
gate = threading.Semaphore(int(os.getenv("SITAAR_WORKERS", "1")))  # heavy jobs run one at a time by default


class JobOut(BaseModel):
    id: str
    filename: str
    model: str
    status: str
    progress: int
    error: str
    stems: List[str]
    analysis: dict


def _out(j: Job) -> JobOut:
    stems = MODELS[j.model][1] if j.status == "done" else []
    return JobOut(id=j.id, filename=j.filename, model=j.model, status=j.status, progress=j.progress,
                  error=j.error or "", stems=stems, analysis=json.loads(j.analysis or "{}"))


def _set(job_id: str, **kw):
    with Session() as s:
        j = s.get(Job, job_id)
        for k, v in kw.items():
            setattr(j, k, v)
        s.commit()


def analyse(path: Path) -> dict:
    """Tempo and key estimate (optional: needs librosa)."""
    try:
        import librosa, numpy as np
        y, sr = librosa.load(str(path), sr=22050, mono=True, duration=240)
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr).mean(axis=1)
        maj = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
        mnr = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
        names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
        best = (-2.0, "C", "major")
        for i in range(12):
            for prof, mode in ((maj, "major"), (mnr, "minor")):
                c = float(np.corrcoef(np.roll(prof, i), chroma)[0, 1])
                if c > best[0]:
                    best = (c, names[i], mode)
        return {"tempo": int(round(float(np.atleast_1d(tempo)[0]))), "key": best[1], "mode": best[2], "confidence": round(best[0], 2)}
    except Exception as e:  # analysis must never fail a job
        return {"error": str(e)[:160]}


def run_job(job_id: str, src: Path, key: str):
    with gate:
        _set(job_id, status="running", progress=1)
        model, stems = MODELS[key]
        out = DATA / job_id / "out"
        cmd = [sys.executable, "-m", "demucs", "-n", model, "-o", str(out)]
        if DEVICE:
            cmd += ["-d", DEVICE]
        cmd.append(str(src))
        tail, last = "", 1
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            while True:
                chunk = p.stderr.read1(256)
                if not chunk:
                    break
                tail = (tail + chunk.decode("utf-8", "ignore"))[-600:]
                m = re.findall(r"(\d{1,3})%", tail)
                if m:
                    pr = min(int(m[-1]), 98)
                    if pr >= last + 2:
                        last = pr
                        _set(job_id, progress=pr)
            p.wait()
            if p.returncode != 0:
                raise RuntimeError(tail.strip()[-300:] or "Demucs failed")
            src_dir = out / model / src.stem
            dest = DATA / job_id / "stems"; dest.mkdir(exist_ok=True)
            for s_ in stems:
                f = src_dir / f"{s_}.wav"
                if not f.exists():
                    raise RuntimeError(f"Missing stem {s_}")
                shutil.move(str(f), dest / f"{s_}.wav")
            shutil.rmtree(out, ignore_errors=True)
            _set(job_id, analysis=json.dumps(analyse(src)), status="done", progress=100)
        except Exception as e:
            _set(job_id, status="error", error=str(e)[:400])


@app.get("/api/health")
def health():
    return {"ok": True, "demucs": importlib.util.find_spec("demucs") is not None,
            "librosa": importlib.util.find_spec("librosa") is not None, "max_mb": MAX_MB}


@app.post("/api/jobs", response_model=JobOut)
async def create_job(bg: BackgroundTasks, file: UploadFile = File(...), stems: str = Form("4")):
    if importlib.util.find_spec("demucs") is None:
        raise HTTPException(503, "Demucs is not installed in the Python environment running this server. Stop the server, activate your venv and run: pip install demucs")
    if stems not in MODELS:
        raise HTTPException(400, "stems must be 4 or 6")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED:
        raise HTTPException(400, f"Unsupported format. Use: {', '.join(sorted(ALLOWED))}")
    job_id = uuid.uuid4().hex
    folder = DATA / job_id; folder.mkdir()
    src = folder / f"input{ext}"
    size = 0
    with open(src, "wb") as f:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > MAX_MB * 1024 * 1024:
                f.close(); shutil.rmtree(folder, ignore_errors=True)
                raise HTTPException(413, f"File larger than {MAX_MB} MB")
            f.write(chunk)
    with Session() as s:
        j = Job(id=job_id, filename=(file.filename or "audio")[:200], model=stems)
        s.add(j); s.commit(); s.refresh(j)
        out = _out(j)
    bg.add_task(run_job, job_id, src, stems)
    return out


@app.get("/api/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str):
    with Session() as s:
        j = s.get(Job, job_id)
        if not j:
            raise HTTPException(404, "Job not found")
        return _out(j)


@app.get("/api/jobs/{job_id}/stems/{name}")
def get_stem(job_id: str, name: str):
    if not re.fullmatch(r"[a-z]+", name) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise HTTPException(400, "Bad name")
    f = DATA / job_id / "stems" / f"{name}.wav"
    if not f.exists():
        raise HTTPException(404, "Stem not found")
    return FileResponse(f, media_type="audio/wav", filename=f"{name}.wav")


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise HTTPException(400, "Bad id")
    shutil.rmtree(DATA / job_id, ignore_errors=True)
    with Session() as s:
        j = s.get(Job, job_id)
        if j:
            s.delete(j); s.commit()
    return {"deleted": True}


def _cleanup():
    cutoff = dt.datetime.utcnow() - dt.timedelta(hours=TTL_H)
    with Session() as s:
        for j in s.query(Job).filter(Job.created < cutoff).all():
            shutil.rmtree(DATA / j.id, ignore_errors=True)
            s.delete(j)
        s.commit()


@app.on_event("startup")
def startup():
    _cleanup()


app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")
