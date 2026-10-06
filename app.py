"""AI Video Clipper — long video se viral short clips. Run: uvicorn app:app --host 0.0.0.0 --port 7860"""
import os
import shutil
import threading
import time
import uuid

from fastapi import FastAPI, Form, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from clipper import Job, run_job

BASE = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(BASE, "jobs")
os.makedirs(JOBS_DIR, exist_ok=True)

app = FastAPI(title="AI Video Clipper")
jobs: dict[str, Job] = {}

app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")


@app.get("/")
def home():
    return FileResponse(os.path.join(BASE, "static", "index.html"))


@app.post("/api/jobs")
def create_job(
    url: str = Form(default=""),
    api_key: str = Form(default=""),
    n_clips: str = Form(default="3"),
    min_dur: str = Form(default="20"),
    max_dur: str = Form(default="60"),
    moment: str = Form(default=""),
    remove_silence: str = Form(default="off"),
    style: str = Form(default="default"),
    hl_keywords: str = Form(default="on"),
    auto_emoji: str = Form(default="on"),
    file: UploadFile = File(default=None),
):
    job_id = uuid.uuid4().hex[:10]
    wd = os.path.join(JOBS_DIR, job_id)
    os.makedirs(wd, exist_ok=True)
    job = Job(job_id=job_id, workdir=wd)
    jobs[job_id] = job

    try:
        n = max(1, min(8, int(n_clips)))
        mn = max(10, min(120, int(min_dur)))
        mx = max(mn, min(180, int(max_dur)))
    except ValueError:
        return JSONResponse({"error": "Numbers sahi likho."}, status_code=400)

    upload_path = None
    if file is not None and file.filename:
        upload_path = os.path.join(wd, "upload_" + os.path.basename(file.filename))
        with open(upload_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
    elif not url.strip():
        return JSONResponse({"error": "YouTube link ya video file do."}, status_code=400)

    t = threading.Thread(
        target=run_job, daemon=True,
        kwargs=dict(job=job, source_url=url.strip() or None,
                    upload_path=upload_path, api_key=api_key,
                    n_clips=n, min_dur=mn, max_dur=mx,
                    moment=moment,
                    remove_silence=(remove_silence == "on"),
                    style=style if style in ("default", "modern") else "default",
                    hl_keywords=(hl_keywords == "on"),
                    auto_emoji=(auto_emoji == "on")),
    )
    t.start()
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "Job nahi mila."}, status_code=404)
    return {
        "status": job.status, "progress": job.progress,
        "message": job.message, "clips": job.clips, "title": job.title,
    }


@app.get("/outputs/{job_id}/clips/{fname}")
def download_clip(job_id: str, fname: str):
    path = os.path.join(JOBS_DIR, job_id, "clips", os.path.basename(fname))
    if not os.path.isfile(path):
        return JSONResponse({"error": "File nahi mili."}, status_code=404)
    return FileResponse(path, media_type="video/mp4", filename=fname)


# purani jobs ki safai (7 din)
def _cleaner():
    while True:
        now = time.time()
        for jid in os.listdir(JOBS_DIR):
            p = os.path.join(JOBS_DIR, jid)
            if os.path.isdir(p) and now - os.path.getmtime(p) > 7 * 86400:
                shutil.rmtree(p, ignore_errors=True)
                jobs.pop(jid, None)
        time.sleep(3600)


threading.Thread(target=_cleaner, daemon=True).start()
