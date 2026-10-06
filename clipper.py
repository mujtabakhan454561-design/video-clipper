"""Core pipeline: download -> transcript -> AI highlights -> vertical clips.
Vizard-style features: moment search, silence removal, caption styles,
keyword highlighting, auto emoji titles.
"""
import json
import os
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field

# yt-dlp venv ke andar installed hai -> python -m yt_dlp use karo
YTDLP = [sys.executable, "-m", "yt_dlp"]


def ts_to_sec(ts: str) -> float:
    ts = ts.replace(",", ".")
    sec = 0.0
    for p in ts.split(":"):
        sec = sec * 60 + float(p)
    return sec


def sec_to_ass(sec: float) -> str:
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    cs = int((sec % 1) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


@dataclass
class Job:
    job_id: str
    workdir: str
    progress: int = 0
    status: str = "queued"  # queued|running|done|error
    message: str = ""
    clips: list = field(default_factory=list)
    title: str = ""


def run_cmd(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-2000:] or f"command failed: {cmd[0]}")
    return r.stdout


def _run_ytdlp(args, extra=None):
    """yt-dlp chalao; proxy SSL issue ho to --no-check-certificate se retry."""
    base = list(extra or [])
    r = subprocess.run(YTDLP + base + args, capture_output=True, text=True)
    if r.returncode != 0 and "CERTIFICATE_VERIFY_FAILED" in r.stderr:
        r = subprocess.run(YTDLP + base + ["--no-check-certificate"] + args,
                           capture_output=True, text=True)
    return r


def _downloaded_file(workdir):
    for f in os.listdir(workdir):
        if f.startswith("source.") and os.path.getsize(os.path.join(workdir, f)) > 100_000:
            return os.path.join(workdir, f)
    return None


# ---------------- download ----------------

# mukhtalif YouTube clients try karo (koi ek chal jata hai)
_CLIENT_OPTS = [
    [],
    ["--extractor-args", "youtube:player_client=web"],
    ["--extractor-args", "youtube:player_client=android"],
]


def download_video(url: str, workdir: str) -> tuple[str, float, str]:
    last_err = ""
    for opts in _CLIENT_OPTS:
        for f in os.listdir(workdir):
            if f.startswith("source."):
                os.remove(os.path.join(workdir, f))
        out = os.path.join(workdir, "source.%(ext)s")
        r = _run_ytdlp([
            "--no-playlist", "-f",
            "bv*[ext=mp4]+ba*[ext=m4a]/b[ext=mp4]/b",
            "--merge-output-format", "mp4",
            "-o", out, "--print", "%(duration)s\t%(title)s", url,
        ], extra=opts)
        if r.returncode != 0:
            last_err = r.stderr[-300:]
            continue
        line = (r.stdout.strip().splitlines() or ["0\tvideo"])[-1]
        dur_s, title = (line.split("\t") + ["video"])[:2]
        try:
            duration = float(dur_s)
        except ValueError:
            duration = 0
        path = _downloaded_file(workdir)
        if path:
            return path, duration, title.strip()
        last_err = "file download nahi hui"
    raise RuntimeError(
        "YouTube ne is server se download block kar diya. "
        "Video apne phone me download karke 'file upload' wala option use karo. "
        + last_err[-150:])


# ---------------- transcript ----------------

def parse_vtt(path: str):
    cues = []
    with open(path, encoding="utf-8", errors="ignore") as f:
        text = f.read()
    for b in re.split(r"\n\s*\n", text):
        m = re.search(r"(\d{2}:\d{2}:\d{2}\.\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}\.\d{3})", b)
        if not m:
            continue
        start, end = ts_to_sec(m.group(1)), ts_to_sec(m.group(2))
        words = re.sub(r"<[^>]+>", "", b[m.end():]).strip()
        words = re.sub(r"\s+", " ", words)
        if words and (not cues or cues[-1]["text"] != words):
            cues.append({"start": start, "end": end, "text": words})
    return cues


def fetch_transcript(url: str, workdir: str):
    """Pehle manual, phir auto captions (koi bhi language -> auto detect)."""
    for auto in (False, True):
        r = _run_ytdlp([
            "--no-playlist", "--skip-download",
            "--write-sub" if not auto else "--write-auto-sub",
            "--sub-langs", "all", "--sub-format", "vtt",
            "-o", os.path.join(workdir, "subs"), url,
        ])
        vtts = [f for f in os.listdir(workdir)
                if f.startswith("subs") and f.endswith(".vtt")]
        # English ko tarjeeh do, warna jo mile
        vtts.sort(key=lambda f: (0 if ".en" in f else 1, f))
        for f in vtts:
            cues = parse_vtt(os.path.join(workdir, f))
            if cues:
                return cues
    return []


def transcribe_upload(video_path: str):
    """Local file -> faster-whisper (language auto-detect)."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RuntimeError(
            "Is file ke liye 'faster-whisper' chahiye. "
            "YouTube link use karo (captions auto mil jayenge)."
        )
    model = WhisperModel("tiny", device="cpu", compute_type="int8")
    segments, info = model.transcribe(video_path)
    print("detected language:", info.language)
    return [
        {"start": s.start, "end": s.end, "text": s.text.strip()}
        for s in segments if s.text.strip()
    ]


# ---------------- AI ----------------

def _gemini_json(prompt: str, api_key: str):
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json"},
    }).encode()
    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key=" + api_key,
        data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.load(resp)
        raw = data["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"AI request failed (API key check karo): {e}")


def _clean_items(items, n_clips, min_dur, max_dur):
    out = []
    for it in items[:n_clips]:
        try:
            s, e = float(it["start"]), float(it["end"])
            if e - s < min_dur - 2 or e - s > max_dur + 10 or s < 0:
                continue
            out.append({
                "start": s, "end": e,
                "title": str(it.get("title", "Clip"))[:60],
                "reason": str(it.get("reason", ""))[:120],
                "keywords": [str(k)[:20] for k in it.get("keywords", [])][:6],
                "emoji": str(it.get("emoji", ""))[:4],
            })
        except (KeyError, ValueError, TypeError):
            continue
    if not out:
        raise RuntimeError("AI ne koi valid clip nahi diya, dobara try karo.")
    return out


def ai_highlights(cues, api_key, n_clips, min_dur, max_dur):
    lines = []
    for c in cues:
        m, s = int(c["start"] // 60), int(c["start"] % 60)
        lines.append(f"[{m:02d}:{s:02d}] {c['text']}")
    prompt = f"""You are a viral short-video editor. Timestamped transcript of a long video below.

Pick the {n_clips} BEST moments that work as standalone vertical short clips (funny, shocking, emotional, or highly informative). Each clip {min_dur} to {max_dur} seconds.

Transcript:
{chr(10).join(lines)[:30000]}

Reply ONLY with a JSON array, no other text. Each item:
{{"start": <seconds>, "end": <seconds>, "title": "<catchy title>", "reason": "<one line>", "keywords": ["<3-6 important words spoken in the clip>"], "emoji": "<one matching emoji>"}}"""
    return _clean_items(_gemini_json(prompt, api_key), n_clips, min_dur, max_dur)


def find_moments(cues, query, api_key, n_clips, min_dur, max_dur):
    """'Find specific moment' — query se matching lamhe dhoondo."""
    if api_key.strip():
        lines = []
        for c in cues:
            m, s = int(c["start"] // 60), int(c["start"] % 60)
            lines.append(f"[{m:02d}:{s:02d}] {c['text']}")
        prompt = f"""Timestamped transcript below. Find the {n_clips} best moments about: "{query}".
Each clip {min_dur} to {max_dur} seconds, standalone samajh aaye.

Transcript:
{chr(10).join(lines)[:30000]}

Reply ONLY with a JSON array: {{"start": <seconds>, "end": <seconds>, "title": "<catchy>", "reason": "<one line>", "keywords": ["<words>"], "emoji": "<emoji>"}}"""
        return _clean_items(_gemini_json(prompt, api_key.strip()), n_clips, min_dur, max_dur)
    # AI key nahi -> keyword search
    words = [w.lower() for w in re.findall(r"\w+", query) if len(w) > 2]
    if not words:
        raise RuntimeError("Moment search ke liye koi lafz likho.")
    hits = [c for c in cues if any(w in c["text"].lower() for w in words)]
    if not hits:
        raise RuntimeError(f"'{query}' transcript me nahi mila. Koi aur lafz try karo.")
    out, i = [], 0
    while i < len(hits) and len(out) < n_clips:
        s = max(0, hits[i]["start"] - 3)
        e = s + min(max_dur, max(min_dur, 40))
        out.append({"start": s, "end": e, "title": query[:55],
                    "reason": "Keyword match", "keywords": words[:6], "emoji": "🎯"})
        i += 1
        while i < len(hits) and hits[i]["start"] < e:
            i += 1
    return out


def fallback_highlights(duration, n_clips, min_dur, max_dur):
    if duration <= 0:
        duration = n_clips * 60
    clip_len = min(max_dur, max(min_dur, 45))
    step = max(duration / n_clips, clip_len + 5)
    out = []
    for i in range(n_clips):
        s = i * step + 5
        if s + clip_len > duration:
            break
        out.append({"start": s, "end": s + clip_len, "title": f"Clip {i+1}",
                    "reason": "Auto (AI key nahi di)", "keywords": [], "emoji": "✂️"})
    return out


# ---------------- subtitles (ASS, Vizard styles) ----------------

# name: (font, size, primary BGR color, outline, marginV)
STYLES = {
    "default": ("Arial", 62, "&H00FFFFFF", 3, 150),   # white
    "modern": ("Arial", 72, "&H0000FFFF", 4, 170),     # yellow bold (screenshot jaisa)
}
HL_COLORS = {"default": "&H0000FFFF", "modern": "&H00FFFFFF"}


def _ass_esc(s: str) -> str:
    return s.replace("{", "(").replace("}", ")").replace("\n", " ")


def write_ass(cues, path, style="default", keywords=None, hl_on=False):
    font, size, color, outline, margin_v = STYLES.get(style, STYLES["default"])
    hl_color = HL_COLORS.get(style, HL_COLORS["default"])
    kws = [k for k in (keywords or []) if len(k) > 2]

    def colorize(text):
        t = _ass_esc(text)
        if hl_on and kws:
            for k in kws:
                t = re.sub(
                    f"(?i)({re.escape(_ass_esc(k))})",
                    r"{\\c" + hl_color + r"\\b1}\1{\\c" + color + r"\\b0}",
                    t)
        return t

    with open(path, "w", encoding="utf-8") as f:
        f.write("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
                "ScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
                "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
                "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
                "MarginL, MarginR, MarginV, Encoding\n"
                f"Style: Clip,{font},{size},{color},&H000019FF,&H99000000,&H00000000,"
                f"-1,0,0,0,100,100,0,0,1,{outline},0,2,40,40,{margin_v},1\n\n[Events]\n"
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
        for c in cues:
            if c["end"] - c["start"] < 0.15:
                continue
            f.write(f"Dialogue: 0,{sec_to_ass(c['start'])},{sec_to_ass(c['end'])},"
                    f"Clip,,0,0,0,,{colorize(c['text'])}\n")


# ---------------- silence removal ----------------

def detect_silences(video, start, end):
    """(sil_start, sil_end) list, absolute seconds me."""
    cmd = ["ffmpeg", "-hide_banner", "-ss", str(start), "-to", str(end),
           "-i", video, "-af", "silencedetect=noise=-35dB:d=0.4",
           "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    sil, cur = [], None
    for line in r.stderr.splitlines():
        m = re.search(r"silence_start: ([\d.]+)", line)
        if m:
            cur = float(m.group(1))
        m = re.search(r"silence_end: ([\d.]+)", line)
        if m and cur is not None:
            sil.append((start + cur, start + float(m.group(1))))
            cur = None
    return sil


def kept_segments(start, end, silences):
    kept, cur = [], start
    for s, e in sorted(silences):
        if s > cur + 0.1:
            kept.append((cur, min(s, end)))
        cur = max(cur, e)
    if cur < end - 0.1:
        kept.append((cur, end))
    return [(a, b) for a, b in kept if b - a >= 0.8]


def retime_cues(cues, kept):
    """Captions ko silence-removed timeline par dobara fit karo."""
    out, offset = [], 0.0
    for a, b in kept:
        for c in cues:
            if c["end"] <= a or c["start"] >= b:
                continue
            ns = max(c["start"], a) - a + offset
            ne = min(c["end"], b) - a + offset
            if ne - ns > 0.15:
                out.append({"start": ns, "end": ne, "text": c["text"]})
        offset += b - a
    return out, offset


# ---------------- smart crop: face tracking ----------------

def detect_face_cx(video: str, start: float, end: float):
    """Clip ke andar chehron ki average horizontal position (0..1).
    Nahi mile to None -> center crop fallback."""
    try:
        import cv2
    except ImportError:
        return None
    try:
        import tempfile
        d = tempfile.mkdtemp(prefix="faces_")
        dur = max(1, int(end - start))
        # har 2 second par ek frame
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-i", video,
             "-vf", f"fps={max(1, dur // 15)}", "-frames:v", "16",
             os.path.join(d, "f%02d.jpg")],
            capture_output=True, timeout=120)
        clf = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        cxs = []
        for fn in sorted(os.listdir(d)):
            img = cv2.imread(os.path.join(d, fn))
            if img is None:
                continue
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            faces = clf.detectMultiScale(gray, 1.2, 4, minSize=(60, 60))
            h, w = gray.shape
            for (x, y, fw, fh) in faces:
                if fh > h * 0.12:  # chhote false-positive ignore
                    cxs.append((x + fw / 2) / w)
        import shutil
        shutil.rmtree(d, ignore_errors=True)
        if not cxs:
            return None
        cxs.sort()
        return cxs[len(cxs) // 2]  # median
    except Exception:
        return None


# ---------------- clip cutting ----------------

def _burnable_title(t: str) -> str:
    """Emoji hatao (server font me render nahi hote) — UI me emoji rehta hai."""
    t = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def cut_clip(video, ass_path, start, end, out_path, title="", emoji="",
             kept=None, face_cx=None):
    ass_esc = ass_path.replace(":", "\\:").replace("'", "")
    # smart crop: chehre par focus, warna center
    if face_cx is None:
        crop_x = "(in_w-ih*9/16)/2"
    else:
        crop_x = f"clip(in_w*{face_cx:.3f}-ih*9/32,0,in_w-ih*9/16)"
    vf = [f"crop=ih*9/16:ih:x={crop_x}", "scale=1080:1920",
          f"subtitles='{ass_esc}'"]
    af = ["loudnorm=I=-16:TP=-1.5:LRA=11"]  # ek jaisi awaz har clip me
    if kept:
        expr = "+".join(f"between(t\\,{a}\\,{b})" for a, b in kept)
        vf.append(f"select='{expr}',setpts=N/FRAME_RATE/TB")
        af.insert(0, f"aselect='{expr}',asetpts=N/SR/TB")
    if title:
        t = _burnable_title((emoji + " " + title) if emoji else title)
        if t:
            t = t.replace(":", "\\:").replace("'", "").replace(",", "\\,")
            vf.append(
                "drawtext=font='DejaVu Sans':"
                f"text='{t}':fontsize=52:fontcolor=white:borderw=2:bordercolor=black:"
                "x=(w-text_w)/2:y=100")
    cmd = ["ffmpeg", "-y", "-ss", str(start), "-to", str(end), "-i", video,
           "-vf", ",".join(vf)]
    if af:
        cmd += ["-af", ",".join(af)]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out_path]
    run_cmd(cmd)


# ---------------- orchestration ----------------

def run_job(job: Job, source_url=None, upload_path=None, api_key="",
            n_clips=3, min_dur=20, max_dur=60, moment="",
            remove_silence=False, style="default", hl_keywords=True,
            auto_emoji=True):
    try:
        job.status = "running"
        wd = job.workdir
        os.makedirs(wd, exist_ok=True)

        job.message = "Video download ho rahi hai..."
        job.progress = 5
        if source_url:
            video, duration, title = download_video(source_url, wd)
            job.title = title
            job.message = "Captions nikal rahe hain (language auto-detect)..."
            job.progress = 25
            cues = fetch_transcript(source_url, wd)
        else:
            video = upload_path
            job.title = os.path.basename(upload_path)
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", video],
                capture_output=True, text=True)
            try:
                duration = float(r.stdout.strip())
            except ValueError:
                duration = 0
            job.message = "Audio transcribe ho raha hai (language auto-detect)..."
            job.progress = 25
            cues = transcribe_upload(video)

        if not cues:
            raise RuntimeError("Captions/transcript nahi mile. Koi aur video try karo.")

        job.message = "AI best moments dhoond raha hai..."
        job.progress = 50
        if moment.strip():
            highlights = find_moments(cues, moment.strip(), api_key,
                                      n_clips, min_dur, max_dur)
        elif api_key.strip():
            highlights = ai_highlights(cues, api_key.strip(),
                                       n_clips, min_dur, max_dur)
        else:
            highlights = fallback_highlights(duration, n_clips, min_dur, max_dur)

        outdir = os.path.join(wd, "clips")
        os.makedirs(outdir, exist_ok=True)
        for i, h in enumerate(highlights):
            job.message = f"Clip {i+1}/{len(highlights)} ban raha hai..."
            job.progress = 55 + int(40 * (i + 1) / len(highlights))
            s, e = h["start"], h["end"]

            seg = [c for c in cues if c["end"] > s and c["start"] < e]
            kept = None
            if remove_silence:
                sil = detect_silences(video, s, e)
                ks = kept_segments(s, e, sil)
                if ks and sum(b - a for a, b in ks) >= 3:
                    # -ss ke baad timestamps 0-based hote hain -> relative karo
                    kept = [(a - s, b - s) for a, b in ks]
            if kept:
                rcues, _ = retime_cues(cues, kept)
            else:
                rcues = [{"start": max(c["start"], s) - s,
                          "end": min(c["end"], e) - s,
                          "text": c["text"]} for c in seg
                         if min(c["end"], e) - max(c["start"], s) > 0.15]

            ass = os.path.join(wd, f"clip{i}.ass")
            write_ass(rcues, ass, style=style,
                      keywords=h.get("keywords"), hl_on=hl_keywords)
            out = os.path.join(outdir, f"clip{i+1}.mp4")
            emoji = h.get("emoji", "") if auto_emoji else ""
            job.message = f"Clip {i+1}/{len(highlights)}: face tracking..."
            face_cx = detect_face_cx(video, s, e)
            cut_clip(video, ass, s, e, out,
                     title=h["title"], emoji=emoji, kept=kept,
                     face_cx=face_cx)
            job.clips.append({
                "file": f"{job.job_id}/clips/clip{i+1}.mp4",
                "title": h["title"], "reason": h.get("reason", ""),
                "start": round(s, 1), "end": round(e, 1),
            })

        job.progress = 100
        job.status = "done"
        job.message = f"{len(job.clips)} clips taiyar!"
    except Exception as e:
        job.status = "error"
        job.message = str(e)[:300]
