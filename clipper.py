"""Core pipeline: download -> transcript -> AI highlights -> vertical clips.
Vizard-style features: moment search, silence removal, caption styles,
keyword highlighting, auto emoji titles, AI B-roll (Pexels).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import uuid
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
def _can_impersonate():
    try:
        import importlib.util
        return importlib.util.find_spec("curl_cffi") is not None
    except Exception:
        return False


def _client_opts():
    opts = []
    if _can_impersonate():
        # asal Chrome jaisa TLS fingerprint — bot-check se bachne ka naya tareeqa
        opts.append(["--impersonate", "chrome"])
        opts.append(["--impersonate", "chrome", "--extractor-args",
                     "youtube:player_client=web"])
    opts += [
        [],
        ["--extractor-args", "youtube:player_client=web"],
        ["--extractor-args", "youtube:player_client=android"],
        ["--extractor-args", "youtube:player_client=ios"],
        ["--extractor-args", "youtube:player_client=web_embedded"],
        ["--extractor-args", "youtube:player_client=tv"],
        ["--extractor-args", "youtube:player_client=mediaconnect"],
    ]
    return opts


_CLIENT_OPTS = _client_opts()


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
    # yt-dlp fail -> Cobalt / Piped / Invidious se try karo (alternate raste)
    if "youtu" in url or "youtube.com" in url or "y2u.be" in url:
        cob = _cobalt_download(url, workdir)
        if cob:
            return cob
        pip = _piped_download(url, workdir)
        if pip:
            return pip
        inv = _invidious_download(url, workdir)
        if inv:
            return inv
    raise RuntimeError(
        "YouTube ne is server se download block kar diya. "
        "Video apne phone me download karke 'file upload' wala option use karo. "
        + last_err[-150:])


_PIPED_APIS = [
    "https://api.piped.private.coffee",
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.reallyaweso.me",
    "https://pipedapi.adminforge.de",
    "https://pipedapi.leptons.xyz",
    "https://pipedapi.r4fo.com",
]


def _piped_download(url, workdir):
    """Piped public API se YouTube video lao (YouTube IP block se bachat)."""
    m = re.search(r"(?:v=|youtu\.be/|shorts/|live/)([\w-]{11})", url)
    if not m:
        return None
    vid = m.group(1)
    for api in _PIPED_APIS:
        try:
            req = urllib.request.Request(
                f"{api}/streams/{vid}",
                headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.load(r)
            streams = [s for s in (data.get("videoStreams") or [])
                       if s.get("url") and not s.get("videoOnly")
                       and "mp4" in (s.get("mimeType") or "")]

            def _q(s):
                q = re.sub(r"\D", "", s.get("quality") or "") or "0"
                return int(q)

            streams.sort(key=_q, reverse=True)
            if not streams:
                continue
            dur = float(data.get("duration") or 0)
            title = data.get("title") or "video"
            _download(streams[0]["url"], os.path.join(workdir, "source.mp4"))
            path = _downloaded_file(workdir)
            if path:
                return path, dur, title
        except Exception:
            continue
    return None


_INVIDIOUS = [
    "https://inv.tux.pizza",
    "https://invidious.nerdvpn.de",
    "https://iv.melmac.space",
    "https://inv.us.projectsegfau.lt",
    "https://vid.puffyan.us",
    "https://invidious.private.coffee",
    "https://yt.artemislena.eu",
    "https://iv.duti.dev",
]


def _invidious_download(url, workdir):
    """Invidious public API se YouTube video lao (koi key nahi chahiye)."""
    m = re.search(r"(?:v=|youtu\.be/|shorts/|live/)([\w-]{11})", url)
    if not m:
        return None
    vid = m.group(1)
    for inst in _INVIDIOUS:
        try:
            req = urllib.request.Request(
                f"{inst}/api/v1/videos/{vid}",
                headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.load(r)
            streams = data.get("formatStreams") or []

            def _score(s):
                q = re.sub(r"\D", "", s.get("qualityLabel") or "") or "0"
                ok = "mp4" in (s.get("container") or "")
                return int(q) if ok else -1

            streams.sort(key=_score, reverse=True)
            if not streams or _score(streams[0]) < 0:
                continue
            dur = float(data.get("lengthSeconds") or 0)
            title = data.get("title") or "video"
            _download(streams[0]["url"], os.path.join(workdir, "source.mp4"))
            path = _downloaded_file(workdir)
            if path:
                return path, dur, title
        except Exception:
            continue
    return None


_COBALT_APIS = [
    "https://cobalt-api.meowing.de/api/json",
    "https://api.cobalt.tools/api/json",
    "https://cobalt-api.kwiatekmiki.com/api/json",
]


def _cobalt_download(url, workdir):
    """Cobalt API se YouTube video lao (koi key nahi chahiye)."""
    body = json.dumps({"url": url, "videoQuality": "720",
                       "filenameStyle": "basic"}).encode()
    for api in _COBALT_APIS:
        try:
            req = urllib.request.Request(
                api, data=body,
                headers={"Content-Type": "application/json",
                         "Accept": "application/json",
                         "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
            if data.get("status") not in ("tunnel", "redirect"):
                continue
            durl = data.get("url")
            if not durl:
                continue
            _download(durl, os.path.join(workdir, "source.mp4"))
            path = _downloaded_file(workdir)
            if path:
                r2 = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries",
                     "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", path],
                    capture_output=True, text=True)
                try:
                    dur = float(r2.stdout.strip())
                except ValueError:
                    dur = 0
                return path, dur, "video"
        except Exception:
            continue
    return None


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
        # PyAV 15+ me 'metadata_errors' option khatam ho gaya;
        # faster-whisper abhi bhi wohi purana call karta hai -> shim laga do
        import av
        _orig_open = av.open

        def _safe_open(*a, **k):
            k.pop("metadata_errors", None)
            return _orig_open(*a, **k)

        av.open = _safe_open
        from faster_whisper import WhisperModel
    except ImportError:
        raise RuntimeError(
            "Is file ke liye 'faster-whisper' chahiye. "
            "YouTube link use karo (captions auto mil jayenge)."
        )
    model = WhisperModel("tiny", device="cpu", compute_type="int8")
    segments, info = model.transcribe(video_path, word_timestamps=True)
    print("detected language:", info.language)
    out = []
    for s in segments:
        if not s.text.strip():
            continue
        words = [{"start": w.start, "end": w.end, "text": w.word.strip()}
                 for w in (s.words or []) if w.word.strip()]
        out.append({"start": s.start, "end": s.end, "text": s.text.strip(),
                    "words": words})
    return out


# ---------------- AI ----------------

_GEMINI_MODELS = ["gemini-3.8-flash", "gemini-2.5-flash",
                 "gemini-2.0-flash", "gemini-1.5-flash"]

def _model_rank(n):
    """Naya version pehle (3.8 > 2.5), flash family pehle."""
    import re
    m = re.search(r"(\d+)\.(\d+)", n)
    ver = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    fam = 0 if "flash" in n else (1 if "pro" in n else 2)
    return (fam, -ver[0], -ver[1], n)

def _gemini_models(api_key):
    """Google se available models ki list lao (retire hone par auto adjust)."""
    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models?key=" + api_key,
        headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    models = []
    for m in data.get("models", []):
        name = m.get("name", "").replace("models/", "")
        if "generateContent" in m.get("supportedGenerationMethods", []):
            models.append(name)
    models.sort(key=_model_rank)
    return models


def _parse_ai_json(text):
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return json.loads(t.strip())


def _gemini_generate(model, prompt, api_key, ver="v1beta"):
    """Ek model+version par generateContent try karo."""
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json"},
    }).encode()
    url = (f"https://generativelanguage.googleapis.com/{ver}/models/"
           f"{model}:generateContent?key=" + api_key)
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            detail = ""
        raise RuntimeError(f"HTTP {e.code}: {detail}")
    cands = data.get("candidates") or []
    if not cands:
        raise RuntimeError("AI ne jawab nahi diya")
    parts = (cands[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise RuntimeError("AI ne khaali jawab diya")
    return text


def _gemini_json(prompt: str, api_key: str):
    try:
        models = _gemini_models(api_key) or _GEMINI_MODELS
    except Exception as e:
        raise RuntimeError(f"AI request failed (API key check karo): {e}")
    last_err = "koi model nahi mila"
    for model in models:
        for ver in ("v1beta", "v1"):
            try:
                return _parse_ai_json(_gemini_generate(model, prompt, api_key, ver))
            except Exception as e:
                last_err = f"{ver}/{model}: {e}"[:140]
                continue  # agla model/version try karo
    raise RuntimeError(f"AI request failed (API key check karo): {last_err}")


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


def ai_hooks(highlights, clip_segs, api_key):
    """Har clip ke liye AI se catchy hook line banao (3-7 lafz)."""
    if not api_key.strip():
        return [h["title"] for h in highlights]
    blocks = []
    for i, (h, seg) in enumerate(zip(highlights, clip_segs)):
        txt = " ".join(c["text"] for c in seg)[:600]
        blocks.append(f"Clip {i+1} [{h['title']}]: {txt}")
    prompt = (
        "You write viral TikTok hooks. For each clip below write ONE short "
        "punchy hook line (3-7 words, no emoji, no quotes) that stops scrolling.\n\n"
        + "\n".join(blocks) +
        "\n\nReply ONLY with a JSON array of strings, one per clip, same order.")
    try:
        hooks = _gemini_json(prompt, api_key.strip())
        if isinstance(hooks, list) and len(hooks) == len(highlights):
            return [str(x).strip()[:60] for x in hooks]
    except Exception:
        pass
    return [h["title"] for h in highlights]


def fallback_highlights(duration, n_clips, min_dur, max_dur):
    if duration <= 0:
        duration = n_clips * 60
    # setting ka asar: har clip ki length min..max ke andar, thodi varied
    lens = [min_dur, (min_dur + max_dur) // 2, max_dur]
    out, pos = [], 5
    for i in range(n_clips):
        clip_len = lens[i % len(lens)]
        if pos + clip_len > duration:
            break
        out.append({"start": pos, "end": pos + clip_len, "title": f"Clip {i+1}",
                    "reason": "Auto (AI key nahi di)", "keywords": [], "emoji": "✂️"})
        pos += clip_len + 5
    return out


# ---------------- subtitles (ASS, Vizard styles) ----------------

RATIOS = {"9:16": (1080, 1920), "4:5": (1080, 1350),
          "1:1": (1080, 1080), "16:9": (1280, 720)}

CAPTION_COLORS = {
    "White": "&H00FFFFFF", "Yellow": "&H0000FFFF", "Cyan": "&H00FFFF00",
    "Lime": "&H0000FF00", "Red": "&H000000FF", "Orange": "&H000080FF",
    "Pink": "&H00FF80FF",
}

# name: (font, size, primary BGR color, outline, marginV, borderStyle, shadow, backColor)
STYLES = {
    "default": ("Arial", 62, "&H00FFFFFF", 3, 150, 1, 0, "&H99000000"),  # white
    "modern":  ("Arial", 72, "&H0000FFFF", 4, 170, 1, 0, "&H99000000"),  # yellow bold
    "neon":    ("Arial", 68, "&H00FFFF00", 3, 160, 1, 0, "&H99000000"),  # cyan neon
    "beast":   ("Arial", 84, "&H000000FF", 5, 170, 1, 0, "&H99000000"),  # red bold huge
    "gold":    ("Arial", 70, "&H0000D7FF", 4, 160, 1, 0, "&H99000000"),  # gold
    "minimal": ("Arial", 44, "&H00FFFFFF", 2, 110, 1, 0, "&H99000000"),  # small white
    "hormozi": ("Arial", 88, "&H00FFFFFF", 6, 180, 1, 0, "&H99000000"),  # hormozi bold
    "boxed":   ("Arial", 64, "&H00FFFFFF", 2, 150, 3, 0, "&HCC000000"),  # black box
    "invert":  ("Arial", 68, "&H00000000", 4, 160, 1, 0, "&H99FFFFFF"),  # black text
    "soft":    ("Arial", 60, "&H00FFFFFF", 1, 150, 1, 3, "&H99000000"),  # soft shadow
}
HL_COLORS = {"default": "&H0000FFFF", "modern": "&H00FFFFFF",
             "neon": "&H00FFFFFF", "beast": "&H0000FFFF",
             "gold": "&H00FFFFFF", "minimal": "&H0000FFFF",
             "hormozi": "&H0000FFFF", "boxed": "&H0000FFFF",
             "invert": "&H0000FFFF", "soft": "&H00FFFF00"}


def _ass_esc(s: str) -> str:
    return s.replace("{", "(").replace("}", ")").replace("\n", " ")


def write_ass(cues, path, style="default", keywords=None, hl_on=False,
              color_override=None, ratio="9:16", anchor="bottom", raw_tags=False):
    W, H = RATIOS.get(ratio, RATIOS["9:16"])
    font, size, color, outline, margin_v, bs, sh, back = STYLES.get(
        style, STYLES["default"])
    base = color_override or color
    # keyword highlight: base se contrast rakho
    hl_color = HL_COLORS.get(style, HL_COLORS["default"])
    if color_override:
        hl_color = "&H00FFFFFF" if base == "&H0000FFFF" else "&H0000FFFF"
    margin_v = int(margin_v * H / 1920)
    kws = [k for k in (keywords or []) if len(k) > 2]

    def colorize(text):
        t = text if raw_tags else _ass_esc(text)
        if hl_on and kws:
            for k in kws:
                t = re.sub(
                    f"(?i)({re.escape(_ass_esc(k))})",
                    r"{\\c" + hl_color + r"\\b1}\1{\\c" + base + r"\\b0}",
                    t)
        return t

    with open(path, "w", encoding="utf-8") as f:
        f.write("[Script Info]\nScriptType: v4.00+\n"
                f"PlayResX: {W}\nPlayResY: {H}\n"
                "ScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
                "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
                "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
                "MarginL, MarginR, MarginV, Encoding\n"
                f"Style: Clip,{font},{size},{base},&H000019FF,{back},&H00000000,"
                f"-1,0,0,0,100,100,0,0,{bs},{outline},{sh},2,40,40,{margin_v},1\n\n[Events]\n"
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
        for c in cues:
            if c["end"] - c["start"] < 0.15:
                continue
            mv = margin_v
            if anchor == "middle":
                mv = H // 2 - 160  # classic: beech wali bar (center y=1120)
            elif anchor == "duo":
                mv = H // 2  # duo: bar center y=960
            elif anchor == "reversebar":
                mv = H // 2 + 160  # reverse: bar center y=800
            elif anchor == "topbar":
                mv = H - 100  # bartop: upar wali bar (center y=100)
            f.write(f"Dialogue: 0,{sec_to_ass(c['start'])},{sec_to_ass(c['end'])},"
                    f"Clip,,0,0,{mv},,{colorize(c['text'])}\n")


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


def _even_words(start, end, text):
    """Word timings nahi hain to evenly distribute karo (fallback)."""
    ws = text.split()
    if not ws:
        return []
    dur = max(0.1, end - start)
    per = dur / len(ws)
    return [{"start": start + i * per, "end": start + (i + 1) * per, "text": w}
            for i, w in enumerate(ws)]


def build_karaoke_cues(words, max_chars=26, active_tag="{\\c&H0000FFFF&}"):
    """Lafz-ba-lafz highlight wale cues (karaoke style animation).
    Har lafz apne time par highlight hota hai, poori line visible rehti hai."""
    lines, cur = [], []
    for w in words:
        t = " ".join(x["text"] for x in cur + [w])
        if cur and len(t) > max_chars:
            lines.append(cur)
            cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(cur)
    cues = []
    for line in lines:
        texts = [w["text"] for w in line]
        for k, w in enumerate(line):
            wend = line[k + 1]["start"] if k + 1 < len(line) else line[-1]["end"]
            if wend - w["start"] < 0.05:
                continue
            parts = [(f"{active_tag}{t}{{\\r}}" if m == k else t)
                     for m, t in enumerate(texts)]
            cues.append({"start": w["start"], "end": wend,
                         "text": " ".join(parts)})
    return cues


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

def _sample_faces(video: str, start: float, end: float):
    """Clip ke frames me faces: [(cx, cy, fh)] fractions me. cy=face center, fh=face height."""
    try:
        import cv2
    except ImportError:
        return []
    try:
        import tempfile
        d = tempfile.mkdtemp(prefix="faces_")
        dur = max(1, int(end - start))
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-i", video,
             "-vf", f"fps={max(1, dur // 15)}", "-frames:v", "16",
             os.path.join(d, "f%02d.jpg")],
            capture_output=True, timeout=120)
        clf = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        out = []
        for fn in sorted(os.listdir(d)):
            img = cv2.imread(os.path.join(d, fn))
            if img is None:
                continue
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            faces = clf.detectMultiScale(gray, 1.2, 4, minSize=(60, 60))
            h, w = gray.shape
            for (x, y, fw, fh) in faces:
                if fh > h * 0.08:  # bahut chhote false-positive ignore
                    out.append(((x + fw / 2) / w, (y + fh / 2) / h, fh / h))
        import shutil
        shutil.rmtree(d, ignore_errors=True)
        return out
    except Exception:
        return []


def _sample_face_cxs(video: str, start: float, end: float):
    """Clip ke frames me sab chehron ki cx list (0..1)."""
    return [f[0] for f in _sample_faces(video, start, end)]


def detect_face_cx(video: str, start: float, end: float):
    """Clip ke andar chehron ki average horizontal position (0..1).
    Nahi mile to None -> center crop fallback."""
    cxs = _sample_face_cxs(video, start, end)
    if not cxs:
        return None
    cxs.sort()
    return cxs[len(cxs) // 2]  # median


def detect_face_box(video: str, start: float, end: float):
    """Main face ka box (cx, cy, fh) fractions me, ya None."""
    faces = _sample_faces(video, start, end)
    if not faces:
        return None
    faces.sort(key=lambda f: f[0])
    return faces[len(faces) // 2]


def detect_two_face_boxes(video: str, start: float, end: float):
    """Double frame ke liye (main_box, second_box|None). Box = (cx, cy, fh).
    main = bare cluster ka chehra (speaker), second = chhote cluster ka
    chehra (listener) — dono hamesha ALAG bande, kabhi same nahi."""
    faces = _sample_faces(video, start, end)
    if not faces:
        return (None, None)
    faces.sort(key=lambda f: f[0])
    main = faces[len(faces) // 2]
    if len(faces) < 3:
        return (main, None)
    cxs = [f[0] for f in faces]
    best_i, best_gap = 0, 0.0
    for i in range(1, len(cxs)):
        g = cxs[i] - cxs[i - 1]
        if g > best_gap:
            best_gap, best_i = g, i
    if best_gap < 0.15:
        return (main, None)  # 1 speaker
    g1, g2 = faces[:best_i], faces[best_i:]
    if len(g1) < 2 or len(g2) < 2:
        return (main, None)
    # bara group = speaker (upar tight close-up), chhota group = listener (neeche)
    # dono ALAG cluster se — upar-neeche kabhi same banda nahi aayega
    large = g1 if len(g1) >= len(g2) else g2
    small = g2 if large is g1 else g1
    return (large[len(large) // 2], small[len(small) // 2])


def detect_two_faces(video: str, start: float, end: float):
    """Double frame ke liye (main_cx, second_cx|None). 1 face mile to bhi kaam karega."""
    b1, b2 = detect_two_face_boxes(video, start, end)
    return ((b1[0] if b1 else None), (b2[0] if b2 else None))


# double frame styles: naam -> (label)
DF_STYLES = {
    "classic": "Classic (close-up upar, wide neeche)",
    "duo": "Duo (2 speakers upar-neeche)",
    "reverse": "Reverse (wide upar, close-up neeche)",
    "pip": "PIP (chhoti window corner me)",
    "bartop": "Bar Top (captions sab se upar)",
    "clean": "Clean (bina bar ke)",
}


# ---------------- clip cutting ----------------

def _burnable_title(t: str) -> str:
    """Emoji hatao (server font me render nahi hote) — UI me emoji rehta hai."""
    t = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF]", "", t)
    return re.sub(r"\s+", " ", t).strip()


# ---------------- AI B-roll (Pexels: related photos/videos) ----------------

_STOPWORDS = set(
    "the a an and or of to in on is are was were be been for with as at by "
    "from that this it its they them he she his her you your we our i my me "
    "not no do does did will would can could should have has had all any very "
    "just so but if about into out up down over under then than too what when "
    "there here".split())


def make_upload_title(h, seg_cues=None):
    """Upload ke liye ready title: emoji + catchy title + hashtags."""
    title = (h.get("title") or "Clip").strip()
    # emoji yaqini banao (AI title me hota hai, auto mode me alag se)
    if not title or ord(title[0]) < 256:
        title = f"{h.get('emoji') or '🎬'} {title}"
    words = []
    for k in (h.get("keywords") or []):
        words += re.findall(r"[a-zA-Z]{4,}", k.lower())
    if not words and seg_cues:
        freq = {}
        for c in seg_cues:
            for w in re.findall(r"[a-zA-Z]{4,}", c["text"].lower()):
                if w not in _STOPWORDS:
                    freq[w] = freq.get(w, 0) + 1
        words = [w for w, _ in sorted(freq.items(), key=lambda x: -x[1])]
    seen, tags = set(), []
    for w in words:
        if w not in seen and w not in _STOPWORDS:
            seen.add(w)
            tags.append("#" + w)
        if len(tags) == 5:
            break
    return (title + " " + " ".join(tags)).strip()


def draw_df_diagram(style, path):
    """Double frame layout ka simple diagram (PIL se, koi video nahi chahiye)."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return False
    W, H = 540, 960
    img = Image.new("RGB", (W, H), (8, 12, 24))
    dr = ImageDraw.Draw(img)
    FACE = (214, 172, 140)
    BAR = (13, 27, 61)
    def face(cx, cy, r):
        dr.ellipse([cx - r, cy - r, cx + r, cy + r], fill=FACE)
    def bar(y0, h=110, label="CAPTIONS"):
        dr.rectangle([0, y0, W, y0 + h], fill=BAR)
        dr.rectangle([90, y0 + 38, 450, y0 + 55], fill=(255, 255, 255))
        dr.rectangle([170, y0 + 63, 370, y0 + 80], fill=(255, 235, 0))
        dr.text((16, y0 + 12), label, fill=(160, 180, 220))
    def panel(y0, y1, kind, label, fx=None):
        dr.rectangle([0, y0, W, y1], fill=(96, 64, 52) if kind == "cu" else (36, 48, 66))
        dr.text((16, y0 + 12), label, fill=(255, 255, 255))
        if kind == "cu":
            face(W // 2 if fx is None else int(W * fx), (y0 + y1) // 2 - 20, 85)
            dr.rectangle([W // 2 - 90, (y0 + y1) // 2 + 65, W // 2 + 90, (y0 + y1) // 2 + 130],
                         fill=(60, 60, 64))
        else:
            dr.rectangle([60, y0 + 90, W - 60, y1 - 60], fill=(52, 68, 90))
            face(W // 2, (y0 + y1) // 2 - 30, 45)
    if style == "classic":
        panel(0, 500, "cu", "CLOSE-UP")
        bar(500)
        panel(610, 960, "wide", "WIDE SHOT")
    elif style == "duo":
        panel(0, 425, "cu", "SPEAKER 1", fx=0.38)
        bar(425)
        panel(535, 960, "cu", "SPEAKER 2", fx=0.62)
    elif style == "reverse":
        panel(0, 350, "wide", "WIDE SHOT")
        bar(350)
        panel(460, 960, "cu", "CLOSE-UP")
    elif style == "pip":
        dr.rectangle([0, 0, W, H], fill=(36, 48, 66))
        dr.rectangle([60, 120, W - 60, 700], fill=(52, 68, 90))
        face(W // 2, 350, 60)
        dr.text((16, 16), "FULL VIDEO", fill=(255, 255, 255))
        dr.rectangle([W - 200, 60, W - 40, 220], fill=(96, 64, 52),
                     outline=(255, 255, 255), width=3)
        face(W - 120, 140, 45)
        dr.text((16, 760), "CAPTIONS (bottom)", fill=(200, 210, 230))
        dr.rectangle([90, 810, 450, 830], fill=(255, 255, 255))
        dr.rectangle([170, 840, 370, 860], fill=(255, 235, 0))
    elif style == "bartop":
        bar(0)
        panel(110, 610, "cu", "CLOSE-UP")
        panel(610, 960, "wide", "WIDE SHOT")
    else:  # clean
        panel(0, 480, "cu", "CLOSE-UP")
        panel(480, 960, "wide", "WIDE SHOT")
        dr.text((16, 800), "CAPTIONS (bottom)", fill=(200, 210, 230))
        dr.rectangle([90, 850, 450, 870], fill=(255, 255, 255))
        dr.rectangle([170, 880, 370, 900], fill=(255, 235, 0))
    img.save(path)
    return True


def ensure_previews():
    """Caption style previews + double frame diagram app me hi banao (upload nahi chahiye)."""
    import tempfile
    d = os.path.join(tempfile.gettempdir(), "vc_previews")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        return d
    try:
        bg = os.path.join(d, "_bg.png")
        if not os.path.isfile(bg):
            run_cmd(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                     "-i", "color=c=0x1a1a2e:s=1080x1920:r=30:d=1",
                     "-frames:v", "1", bg])
        need = [s for s in STYLES if not os.path.isfile(os.path.join(d, f"{s}.png"))]
        if need:
            cues = [{"start": 0, "end": 5, "text": "ye moment sab se best hai"}]
            for style in need:
                ass = os.path.join(d, f"_{style}.ass")
                write_ass(cues, ass, style=style, keywords=["best"], hl_on=True,
                          ratio="9:16", anchor="bottom")
                esc = ass.replace(":", "\\:").replace("'", "")
                run_cmd(["ffmpeg", "-y", "-v", "error", "-i", bg, "-vf",
                         f"subtitles='{esc}'", "-frames:v", "1",
                         os.path.join(d, f"{style}.png")])
                if os.path.isfile(ass):
                    os.remove(ass)
        for _st in ("classic", "duo", "reverse", "pip", "bartop", "clean"):
            _dp = os.path.join(d, f"df_{_st}.png")
            if not os.path.isfile(_dp):
                draw_df_diagram(_st, _dp)
        _kp = os.path.join(d, "karaoke.png")
        if not os.path.isfile(_kp):
            _kass = os.path.join(d, "_karaoke.ass")
            _kcues = build_karaoke_cues(
                [{"start": i * 0.4, "end": i * 0.4 + 0.38, "text": w}
                 for i, w in enumerate("ye moment sab se best hai".split())],
                max_chars=26)
            write_ass(_kcues, _kass, style="hormozi", hl_on=False,
                      ratio="9:16", anchor="bottom", raw_tags=True)
            _kesc = _kass.replace(":", "\\:").replace("'", "")
            try:
                run_cmd(["ffmpeg", "-y", "-v", "error", "-i", bg, "-vf",
                         f"subtitles='{_kesc}'", "-frames:v", "1", _kp])
            except Exception:
                pass
            if os.path.isfile(_kass):
                os.remove(_kass)
    except Exception:
        pass
    return d


def _broll_queries(seg_cues, keywords):
    """Clip ke liye 2 Pexels search queries: AI keywords, warna frequent words."""
    qs = [k.strip() for k in (keywords or []) if len(k.strip()) > 2][:2]
    if len(qs) < 2:
        freq = {}
        for c in seg_cues:
            for w in re.findall(r"[a-zA-Z]{4,}", c["text"].lower()):
                if w not in _STOPWORDS:
                    freq[w] = freq.get(w, 0) + 1
        for w, _ in sorted(freq.items(), key=lambda x: -x[1]):
            if w not in qs:
                qs.append(w)
            if len(qs) == 2:
                break
    return qs


def _download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def _pexels_json(url, key):
    req = urllib.request.Request(url, headers={"Authorization": key})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def pexels_fetch(query, key, workdir, dur, ratio="9:16"):
    """Pexels se related video (warna photo->zoompan) lao: 1080x1920, dur sec.
    Fail ho to None."""
    q = urllib.parse.quote(query)
    uid = uuid.uuid4().hex[:6]
    W, H = RATIOS.get(ratio, RATIOS["9:16"])
    orient = "portrait" if W <= H else "landscape"
    last_err = ""
    # 1) stock video
    try:
        data = _pexels_json(
            f"https://api.pexels.com/videos/search?query={q}&per_page=3"
            "&orientation=" + orient + "&size=small", key)
        for v in data.get("videos", []):
            files = sorted((f for f in v.get("video_files", []) if f.get("link")),
                           key=lambda f: f.get("width", 9999))
            if not files:
                continue
            src = os.path.join(workdir, f"broll_src_{uid}.mp4")
            _download(files[0]["link"], src)
            out = os.path.join(workdir, f"broll_{uid}.mp4")
            run_cmd(["ffmpeg", "-y", "-i", src, "-vf",
                     f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                     f"crop={W}:{H},fps=30",
                     "-t", str(dur), "-an", "-c:v", "libx264",
                     "-preset", "veryfast", "-crf", "23", out])
            return out
    except Exception as e:
        last_err = f"video: {e}"[:100]
    # 2) photo fallback -> slow zoom video
    try:
        data = _pexels_json(
            f"https://api.pexels.com/v1/search?query={q}&per_page=3"
            f"&orientation={orient}", key)
        for p in data.get("photos", []):
            srcinfo = p.get("src") or {}
            link = srcinfo.get("large") or srcinfo.get("medium")
            if not link:
                continue
            src = os.path.join(workdir, f"broll_img_{uid}.jpg")
            _download(link, src)
            out = os.path.join(workdir, f"broll_{uid}.mp4")
            run_cmd(["ffmpeg", "-y", "-loop", "1", "-i", src, "-vf",
                     f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                     f"crop={W}:{H},zoompan=z='min(zoom+0.0012,1.25)':d=1:"
                     "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                     f"s={W}x{H}:fps=30",
                     "-t", str(dur), "-an", "-c:v", "libx264",
                     "-preset", "veryfast", "-crf", "23", out])
            return out
    except Exception as e:
        last_err = f"photo: {e}"[:100]
    raise RuntimeError(f"Pexels fail ({last_err or 'no result'})")


def pixabay_fetch(query, key, workdir, dur, ratio="9:16"):
    """Pixabay (free key foran milta hai) se related video/photo lao."""
    q = urllib.parse.quote(query)
    uid = uuid.uuid4().hex[:6]
    W, H = RATIOS.get(ratio, RATIOS["9:16"])
    orient = "vertical" if W <= H else "horizontal"
    last_err = ""
    # 1) stock video
    try:
        req = urllib.request.Request(
            f"https://pixabay.com/api/videos/?key={key}&q={q}&per_page=3"
            f"&orientation={orient}"
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
        for h in data.get("hits", []):
            vids = (h.get("videos") or {})
            link = (vids.get("medium") or {}).get("url") or \
                   (vids.get("small") or {}).get("url")
            if not link:
                continue
            src = os.path.join(workdir, f"broll_src_{uid}.mp4")
            _download(link, src)
            out = os.path.join(workdir, f"broll_{uid}.mp4")
            run_cmd(["ffmpeg", "-y", "-i", src, "-vf",
                     f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                     f"crop={W}:{H},fps=30",
                     "-t", str(dur), "-an", "-c:v", "libx264",
                     "-preset", "veryfast", "-crf", "23", out])
            return out
    except Exception as e:
        last_err = f"video: {e}"[:100]
    # 2) photo fallback -> slow zoom video
    try:
        req = urllib.request.Request(
            f"https://pixabay.com/api/?key={key}&q={q}&per_page=3"
            f"&orientation={orient}&image_type=photo")
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
        for h in data.get("hits", []):
            link = h.get("largeImageURL") or h.get("webformatURL")
            if not link:
                continue
            src = os.path.join(workdir, f"broll_img_{uid}.jpg")
            _download(link, src)
            out = os.path.join(workdir, f"broll_{uid}.mp4")
            run_cmd(["ffmpeg", "-y", "-loop", "1", "-i", src, "-vf",
                     f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                     f"crop={W}:{H},zoompan=z='min(zoom+0.0012,1.25)':d=1:"
                     "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                     f"s={W}x{H}:fps=30",
                     "-t", str(dur), "-an", "-c:v", "libx264",
                     "-preset", "veryfast", "-crf", "23", out])
            return out
    except Exception as e:
        last_err = f"photo: {e}"[:100]
    raise RuntimeError(f"Pixabay fail ({last_err or 'no result'})")


def wikimedia_fetch(query, workdir, dur, ratio="9:16"):
    """Wikimedia Commons se free photos/videos — koi key nahi chahiye."""
    W, H = RATIOS.get(ratio, RATIOS["9:16"])
    uid = uuid.uuid4().hex[:6]
    last_err = ""
    # 1) photo -> slow zoom video
    try:
        q = urllib.parse.quote(f"filetype:bitmap {query}")
        url = ("https://commons.wikimedia.org/w/api.php?action=query&format=json"
               f"&generator=search&gsrsearch={q}&gsrnamespace=6&gsrlimit=8"
               "&prop=imageinfo&iiprop=url|size&iiurlwidth=1280")
        req = urllib.request.Request(url, headers={"User-Agent": "VideoClipper/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
        pages = list((data.get("query") or {}).get("pages", {}).values())
        for pg in pages:
            info = (pg.get("imageinfo") or [{}])[0]
            link = info.get("thumburl") or info.get("url") or ""
            if not link.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                continue
            src = os.path.join(workdir, f"broll_img_{uid}.jpg")
            _download(link, src)
            out = os.path.join(workdir, f"broll_{uid}.mp4")
            run_cmd(["ffmpeg", "-y", "-loop", "1", "-i", src, "-vf",
                     f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                     f"crop={W}:{H},zoompan=z='min(zoom+0.0012,1.25)':d=1:"
                     "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                     f"s={W}x{H}:fps=30",
                     "-t", str(dur), "-an", "-c:v", "libx264",
                     "-preset", "veryfast", "-crf", "23", out])
            return out
        last_err = "koi photo nahi mili"
    except Exception as e:
        last_err = f"photo: {e}"[:100]
    # 2) video
    try:
        q = urllib.parse.quote(f"filetype:video {query}")
        url = ("https://commons.wikimedia.org/w/api.php?action=query&format=json"
               f"&generator=search&gsrsearch={q}&gsrnamespace=6&gsrlimit=8"
               "&prop=imageinfo&iiprop=url|size")
        req = urllib.request.Request(url, headers={"User-Agent": "VideoClipper/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
        pages = list((data.get("query") or {}).get("pages", {}).values())
        for pg in pages:
            info = (pg.get("imageinfo") or [{}])[0]
            link = info.get("url") or ""
            if not link:
                continue
            src = os.path.join(workdir, f"broll_src_{uid}.mp4")
            _download(link, src)
            out = os.path.join(workdir, f"broll_{uid}.mp4")
            run_cmd(["ffmpeg", "-y", "-i", src, "-vf",
                     f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                     f"crop={W}:{H},fps=30",
                     "-t", str(dur), "-an", "-c:v", "libx264",
                     "-preset", "veryfast", "-crf", "23", out])
            return out
        last_err = "koi video nahi mili"
    except Exception as e:
        last_err = f"video: {e}"[:100]
    raise RuntimeError(f"Wikimedia fail ({last_err or 'no result'})")


def _keyed_fetch(query, key, workdir, dur, ratio):
    """Sirf Pexels/Pixabay (key wali services)."""
    try:
        return pexels_fetch(query, key, workdir, dur, ratio)
    except Exception as e1:
        try:
            return pixabay_fetch(query, key, workdir, dur, ratio)
        except Exception as e2:
            raise RuntimeError(f"{e1} | {e2}")


def stock_fetch(query, key, workdir, dur, ratio="9:16"):
    """B-roll lao: pehle Pexels/Pixabay (key ho to), warna Wikimedia (free)."""
    last = ""
    if key.strip():
        try:
            return _keyed_fetch(query, key, workdir, dur, ratio)
        except Exception as e:
            last = str(e)
    else:
        last = "key nahi di"
    try:
        return wikimedia_fetch(query, workdir, dur, ratio)
    except Exception as e2:
        raise RuntimeError(f"{last} | Wikimedia: {e2}")


def test_gemini_key(api_key):
    """(ok, msg): Gemini key + generateContent dono test karo."""
    if not api_key.strip():
        return False, "Key khali hai"
    try:
        models = _gemini_models(api_key.strip())
    except Exception as e:
        return False, str(e)[:150]
    if not models:
        return False, "Key sahi, lekin koi model nahi mila"
    for ver in ("v1beta", "v1"):
        try:
            _gemini_generate(models[0], 'Reply with exactly: {"ok": true}',
                             api_key.strip(), ver)
            return True, f"OK — {len(models)} models, AI generate chal raha ({ver}/{models[0]})"
        except Exception as e:
            last = f"{ver}: {e}"[:280]
    return False, f"List OK ({len(models)} models) lekin AI generate fail: {last}"


def test_stock_key(key):
    """(ok, msg): Pexels/Pixabay key sahi hai ya nahi (sirf key wali)."""
    if not key.strip():
        return False, "Key khali hai"
    import tempfile
    d = tempfile.mkdtemp(prefix="keytest_")
    try:
        _keyed_fetch("dog", key.strip(), d, 2, "9:16")
        return True, "OK — key sahi hai, B-roll mil gaya"
    except Exception as e:
        return False, str(e)[:180]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _plan_broll_slots(dur):
    """(start, dur) slots: 20s+ par 1, 35s+ par 2."""
    """(start, dur) slots: 20s+ par 1, 35s+ par 2."""
    slots = []
    if dur >= 20:
        slots.append((dur * 0.40, 3.5))
    if dur >= 35:
        slots.append((dur * 0.68, 3.5))
    return [(s, d) for s, d in slots if s + d < dur - 1]


def add_broll(clip_path, slots, out_path, ratio="9:16"):
    """slots=[(start, dur, broll_path)]: B-roll se main video cover karo,
    audio/captions same rehte hain."""
    W, H = RATIOS.get(ratio, RATIOS["9:16"])
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", clip_path],
        capture_output=True, text=True)
    D = float(r.stdout.strip())
    slots = sorted(slots)
    n = len(slots)
    bounds = [0.0]
    for (st, du, _) in slots:
        bounds += [st, st + du]
    bounds.append(D)
    fc = ["[0:v]split=%d%s" % (n + 1, "".join(f"[m{i}]" for i in range(n + 1)))]
    for i in range(n + 1):
        a, b = bounds[2 * i], bounds[2 * i + 1]
        fc.append(f"[m{i}]trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS,"
                  f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                  f"crop={W}:{H},fps=30[p{i}]")
    for i in range(1, n + 1):
        fc.append(f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
                  f"crop={W}:{H},fps=30,setpts=PTS-STARTPTS[q{i}]")
    seq = []
    for i in range(n + 1):
        seq.append(f"[p{i}]")
        if i < n:
            seq.append(f"[q{i + 1}]")
    fc.append("".join(seq) + f"concat=n={2 * n + 1}:v=1:a=0[vout]")
    cmd = ["ffmpeg", "-y", "-i", clip_path]
    for (_, _, bp) in slots:
        cmd += ["-i", bp]
    cmd += ["-filter_complex", ";".join(fc),
            "-map", "[vout]", "-map", "0:a",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", out_path]
    run_cmd(cmd)


def _cu_crop(cx):
    """Tight close-up crop (face par focus)."""
    return (f"crop=iw*0.62:ih:x=clip(iw*{cx:.3f}-iw*0.31\\,0\\,iw*0.38):y=0")


def _tight_crop(sw, sh, box, cx_fallback=0.5):
    """Asal tight face close-up: chehre ke gird 2.3x chaudai, 2.9x unchai.
    box=(cx, cy, fh) fractions me; None ho to head-and-shoulders fallback."""
    if box is None:
        cw, ch = int(sw * 0.50), int(sh * 0.58)
        x = int(min(max(sw * cx_fallback - cw / 2, 0), sw - cw))
        y = int(sh * 0.04)
    else:
        cx, cy, fh = box
        cw = int(sh * fh * 0.75 * 2.3)
        ch = int(sh * fh * 2.9)
        cw = min(max(cw, 120), sw)
        ch = min(max(ch, 120), sh)
        x = int(min(max(sw * cx - cw / 2, 0), sw - cw))
        y = int(min(max(sh * cy - ch * 0.40, 0), sh - ch))
    return f"crop={cw}:{ch}:x={x}:y={y}"


def _listener_crop(sw, sh, box):
    """Neeche wale panel ke liye: dusra banda (sunne wala) — medium shot."""
    cx, cy, fh = box
    cw, ch = int(sw * 0.68), int(sh * 0.72)
    x = int(min(max(sw * cx - cw / 2, 0), sw - cw))
    y = int(min(max(sh * cy - ch * 0.45, 0), sh - ch))
    return f"crop={cw}:{ch}:x={x}:y={y}"


def _src_size(video):
    """Source video ki (width, height)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", video],
            capture_output=True, text=True, timeout=30)
        w, h = r.stdout.strip().split(",")[:2]
        return int(w), int(h)
    except Exception:
        return 1920, 1080


_WIDE_CROP = "crop=iw*0.96:ih*0.96:x=iw*0.02:y=ih*0.02"


def _sc_wh(w, h):
    """Scale-to-cover + SAR fix (double frame panels ke liye)."""
    return (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},setsar=1,fps=30,format=yuv420p")


def cut_clip(video, ass_path, start, end, out_path, title="", emoji="",
             kept=None, face_cx=None, ratio="9:16", captions=True, hook="",
             df=None):
    W, H = RATIOS.get(ratio, RATIOS["9:16"])
    ass_esc = ass_path.replace(":", "\\:").replace("'", "")
    af = ["loudnorm=I=-16:TP=-1.5:LRA=11"]  # ek jaisi awaz har clip me
    if kept:
        expr = "+".join(f"between(t\\,{a}\\,{b})" for a, b in kept)
        af.insert(0, f"aselect='{expr}',asetpts=N/SR/TB")
    if df and ratio == "9:16":
        # 6 double frame styles — upar ASAL tight face close-up, neeche wide
        style = df.get("style", "classic")
        box1 = df.get("box1")
        box2 = df.get("box2")
        cx1 = box1[0] if box1 else df.get("cx1", 0.5)
        sw, sh = _src_size(video)
        cu1 = _tight_crop(sw, sh, box1, cx1)
        cu2 = _tight_crop(sw, sh, box2, box2[0] if box2 else 0.5) if box2 else None
        # neeche wala panel: dusra banda (listener) ho to us par, warna wide
        bot_panel = _listener_crop(sw, sh, box2) if box2 else _WIDE_CROP
        bar_dur = sum(b - a for a, b in kept) if kept else (end - start)
        hook_y = 90
        parts = []
        if kept:
            parts.append(f"select='{expr}',setpts=N/FRAME_RATE/TB")
        parts.append("split[a][b]")
        bar = (f"color=c=0x0d1b3d:s=1080x200:r=30:d={bar_dur:.1f},"
               "format=yuv420p[bar]")
        if style == "pip":
            parts += [
                f"[a]{cu1},{_sc_wh(440, 440)}[ins]",
                f"[b]{_sc_wh(1080, 1920)}[bg]",
                "[bg][ins]overlay=x=W-w-40:y=170,format=yuv420p[v]",
            ]
        elif style == "clean":
            parts += [
                f"[a]{cu1},{_sc_wh(1080, 960)}[top]",
                f"[b]{bot_panel},{_sc_wh(1080, 960)}[bot]",
                "[top][bot]vstack=inputs=2[v]",
            ]
        elif style == "duo":
            parts.append(bar)
            bot_src = cu2 if cu2 else _WIDE_CROP
            parts += [
                f"[a]{cu1},{_sc_wh(1080, 860)}[top]",
                f"[b]{bot_src},{_sc_wh(1080, 860)}[bot]",
                "[top][bar][bot]vstack=inputs=3[v]",
            ]
        elif style == "reverse":
            parts.append(bar)
            parts += [
                f"[a]{_WIDE_CROP},{_sc_wh(1080, 700)}[top]",
                f"[b]{cu1},{_sc_wh(1080, 1020)}[bot]",
                "[top][bar][bot]vstack=inputs=3[v]",
            ]
        elif style == "bartop":
            parts.append(bar)
            hook_y = 240
            parts += [
                f"[a]{cu1},{_sc_wh(1080, 1020)}[mid]",
                f"[b]{bot_panel},{_sc_wh(1080, 700)}[bot]",
                "[bar][mid][bot]vstack=inputs=3[v]",
            ]
        else:  # classic
            parts.append(bar)
            parts += [
                f"[a]{cu1},{_sc_wh(1080, 1020)}[top]",
                f"[b]{bot_panel},{_sc_wh(1080, 700)}[bot]",
                "[top][bar][bot]vstack=inputs=3[v]",
            ]
        if captions:
            parts.append(f"[v]subtitles='{ass_esc}'[vout]")
        else:
            parts.append("[v]null[vout]")
        if hook:
            t = _burnable_title(hook).replace(":", "\\:").replace("'", "").replace(",", "\\,")
            if t:
                # hook ko vout par lagao: chain ko dobara label karo
                parts.append(
                    f"[vout]drawtext=font='DejaVu Sans':"
                    f"text='{t}':fontsize=54:fontcolor=white:borderw=3:bordercolor=black:"
                    f"x=(w-text_w)/2:y={hook_y}[vout2]")
                vlabel = "[vout2]"
            else:
                vlabel = "[vout]"
        else:
            vlabel = "[vout]"
        cmd = ["ffmpeg", "-y", "-ss", str(start), "-to", str(end), "-i", video,
               "-filter_complex", ";".join(parts),
               "-map", vlabel, "-map", "0:a?"]
        if af:
            cmd += ["-af", ",".join(af)]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", out_path]
        run_cmd(cmd)
        return
    else:
        # scale-to-cover phir smart crop: chehre par focus (vertical), warna center
        vf = [f"scale={W}:{H}:force_original_aspect_ratio=increase"]
        if face_cx is not None and W <= H:
            vf.append(f"crop={W}:{H}:x='clip(in_w*{face_cx:.3f}-{W/2},0,in_w-{W})'"
                      f":y='(in_h-{H})/2'")
        else:
            vf.append(f"crop={W}:{H}")
        if kept:
            expr = "+".join(f"between(t\\,{a}\\,{b})" for a, b in kept)
            vf.append(f"select='{expr}',setpts=N/FRAME_RATE/TB")
    if captions:
        vf.append(f"subtitles='{ass_esc}'")
    if hook:
        t = _burnable_title(hook).replace(":", "\\:").replace("'", "").replace(",", "\\,")
        if t:
            vf.append(
                "drawtext=font='DejaVu Sans':"
                f"text='{t}':fontsize=54:fontcolor=white:borderw=3:bordercolor=black:"
                "x=(w-text_w)/2:y=90")
    # title overlay hata diya (user ki request) — title sirf app UI me dikhega
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
            auto_emoji=True, pexels_key="", broll=True, ratio="9:16",
            captions=True, caption_color="", show_hook=False, hook_text="",
            df_style="off", animated_captions=False):
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
            if not cues:
                # captions blocked -> downloaded video ko Whisper se transcribe karo
                job.message = "Captions blocked, AI transcribe kar raha hai..."
                cues = transcribe_upload(video)
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
        ai_note = ""
        try:
            if moment.strip():
                highlights = find_moments(cues, moment.strip(), api_key,
                                          n_clips, min_dur, max_dur)
            elif api_key.strip():
                highlights = ai_highlights(cues, api_key.strip(),
                                           n_clips, min_dur, max_dur)
            else:
                highlights = fallback_highlights(duration, n_clips, min_dur, max_dur)
        except RuntimeError as e:
            # AI key/model fail -> auto mode se clips banao, rukna nahi
            ai_note = f"AI nahi chala ({str(e)[:90]}), auto mode use hua"
            job.message = ai_note + "..."
            highlights = fallback_highlights(duration, n_clips, min_dur, max_dur)

        outdir = os.path.join(wd, "clips")
        os.makedirs(outdir, exist_ok=True)
        broll_errs, broll_used = [], 0
        # Hook: khud likha ho to wahi, warna AI se har clip ke liye banwao
        if show_hook and not hook_text.strip():
            job.message = "AI hooks likh raha hai..."
            _segs = []
            for h in highlights:
                s0, e0 = h["start"], h["end"]
                _segs.append([c for c in cues if c["end"] > s0 and c["start"] < e0])
            auto_hooks = ai_hooks(highlights, _segs, api_key)
        else:
            auto_hooks = [hook_text] * len(highlights)
        for i, h in enumerate(highlights):
            job.message = f"Clip {i+1}/{len(highlights)} ban raha hai..."
            job.progress = 55 + int(40 * (i + 1) / len(highlights))
            s, e = h["start"], h["end"]

            seg = [c for c in cues if c["end"] > s and c["start"] < e]
            kept = None
            ks_abs = None
            if remove_silence:
                sil = detect_silences(video, s, e)
                ks = kept_segments(s, e, sil)
                if ks and sum(b - a for a, b in ks) >= 3:
                    ks_abs = ks
                    kept = [(a - s, b - s) for a, b in ks]
            if animated_captions:
                # lafz-ba-lafz animated captions (karaoke style)
                wlist = []
                for c in cues:
                    ws = c.get("words") or _even_words(c["start"], c["end"], c["text"])
                    wlist.extend(ws)
                if ks_abs:
                    wlist, _ = retime_cues(wlist, ks_abs)
                else:
                    wlist = [{"start": max(w["start"], s) - s,
                              "end": min(w["end"], e) - s,
                              "text": w["text"]} for w in wlist
                             if w["end"] > s and w["start"] < e
                             and min(w["end"], e) - max(w["start"], s) > 0.05]
                active = ("{\\c&H00FFFFFF&}" if style in ("modern", "gold")
                          else "{\\c&H0000FFFF&}")
                mc = 26 if ratio == "9:16" else 48
                rcues = build_karaoke_cues(wlist, max_chars=mc, active_tag=active)
                hl_for_ass = False
            else:
                if kept:
                    rcues, _ = retime_cues(cues, ks_abs)
                else:
                    rcues = [{"start": max(c["start"], s) - s,
                              "end": min(c["end"], e) - s,
                              "text": c["text"]} for c in seg
                             if min(c["end"], e) - max(c["start"], s) > 0.15]
                hl_for_ass = hl_keywords

            ass = os.path.join(wd, f"clip{i}.ass")
            job.message = f"Clip {i+1}/{len(highlights)}: face tracking..."
            face_cx = detect_face_cx(video, s, e)
            df = None
            if df_style != "off" and ratio == "9:16":
                job.message = f"Clip {i+1}: double frame ({df_style}) bana raha hai..."
                b1, b2 = detect_two_face_boxes(video, s, e)
                if b1 is None:
                    # face nahi mila to bhi style ZAROOR lagao — center crop se
                    cx_fb = face_cx if face_cx is not None else 0.5
                    b1 = (cx_fb, 0.35, 0.22)
                df = {"style": df_style, "box1": b1, "box2": b2,
                      "cx1": b1[0]}
            if df and df["style"] in ("classic", "duo", "reverse", "bartop"):
                anchor = {"classic": "middle", "duo": "duo",
                          "reverse": "reversebar", "bartop": "topbar"}[df["style"]]
            else:
                anchor = "bottom"
            write_ass(rcues, ass, style=style,
                      keywords=h.get("keywords"), hl_on=hl_for_ass,
                      color_override=(CAPTION_COLORS.get(caption_color)
                                      if caption_color else None),
                      ratio=ratio, anchor=anchor,
                      raw_tags=animated_captions)
            out = os.path.join(outdir, f"clip{i+1}.mp4")
            emoji = h.get("emoji", "") if auto_emoji else ""
            cut_clip(video, ass, s, e, out,
                     title=h["title"], emoji=emoji, kept=kept,
                     face_cx=face_cx, ratio=ratio, captions=captions,
                     hook=(auto_hooks[i] if show_hook else ""),
                     df=df)
            # AI B-roll: related photos/videos se main video cover karo
            # (key ho to HD, warna Wikimedia se free — key zaroori nahi)
            if broll:
                try:
                    dur = sum(b - a for a, b in kept) if kept else (e - s)
                    slots = _plan_broll_slots(dur)
                    queries = _broll_queries(seg, h.get("keywords"))
                    bro = []
                    for (bs, bd), q in zip(slots, queries):
                        job.message = f"Clip {i+1}: B-roll '{q}' lag raha hai..."
                        try:
                            p = stock_fetch(q, pexels_key.strip(), wd, bd, ratio)
                        except Exception as se:
                            p = None
                            broll_errs.append(str(se)[:90])
                        if p:
                            bro.append((bs, bd, p))
                    if bro:
                        broll_used += len(bro)
                        tmp = out + ".broll.mp4"
                        add_broll(out, bro, tmp, ratio)
                        os.replace(tmp, out)
                except Exception as be:
                    broll_errs.append(str(be)[:90])
                # B-roll fail -> original clip rehne do
            job.clips.append({
                "file": f"{job.job_id}/clips/clip{i+1}.mp4",
                "title": h["title"], "reason": h.get("reason", ""),
                "start": round(s, 1), "end": round(e, 1),
                "upload_title": make_upload_title(h, seg),
            })

        job.progress = 100
        job.status = "done"
        notes = []
        if ai_note:
            notes.append("⚠ " + ai_note)
        if broll and broll_used == 0:
            err = broll_errs[-1] if broll_errs else "koi visual nahi mila"
            notes.append(f"⚠ B-roll nahi laga ({err})")
        job.message = f"{len(job.clips)} clips taiyar!" + (
            " " + " ".join(notes) if notes else "")
    except Exception as e:
        job.status = "error"
        job.message = str(e)[:300]
