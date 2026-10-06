# ✂️ AI Video Clipper (Private)

Long video ka link do — AI best moments nikal kar **vertical 9:16 short clips** bana dega,
animated captions, title aur keyword highlights ke sath. Bilkul private — tumhare apne server par chalta hai.

## Features (Vizard-style + extra)

- **AI Highlights** — Gemini AI transcript parh kar viral moments chunta hai (free API key)
- **Find Specific Moment** — likho "when she talks about breathing", AI woh lamha dhoond lega
- **Remove Silences** — khamosh hisse kat jate hain, captions dobara sync ho jate hain
- **Clip Styles** — Default (white) / Modern (yellow, bold)
- **Highlight Keywords** — aham lafz captions me rang me nazar aate hain
- **Auto Emoji + Title** — har clip par catchy title (screenshot wala style)
- **Face-Tracking Crop** — speaker ke chehre par focus, center-crop ki jagah
- **Loudness Normalize** — har clip ek jaisi awaz
- **Language Auto-Detect** — YouTube captions ya upload transcription, koi bhi zuban
- **Bina AI key ke bhi chalta hai** — auto mode me barabar clips

## Apne computer par chalao

```bash
cd video-clipper
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# ffmpeg pehle se installed hona chahiye: https://ffmpeg.org
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 7860
```

Phir browser me kholo: **http://localhost:7860**

1. YouTube/TikTok/Facebook video ka link paste karo (ya file upload karo)
2. (Optional) Free Gemini key dalo: https://aistudio.google.com/apikey
3. Clips ki tadad, length, style select karo → **Clips Banao** dabao
4. Neeche preview + Download button aayenge

## Online (Render — free)

1. **github.com** par account banao → **New repository** (`video-clipper`) → **Add file → Upload files** → zip ki saari files upload karo
2. **render.com** par GitHub se **Sign up** karo
3. **New → Web Service** → apni `video-clipper` repository connect karo
4. **Runtime: Docker**, **Instance type: Free** select karo → **Deploy** dabao
5. 3-5 minute me build ho jayega — Render ek link dega jaise `video-clipper.onrender.com` — wohi tumhari app hai, mobile browser me kholo

Note: free service 15 minute istemal na ho to so jata hai, kholne par 1 minute me jaag jata hai.

## Notes

- YouTube links ke liye captions auto nikale jate hain; upload ki hui file faster-whisper se transcribe hoti hai
- Jobs 7 din baad auto-delete ho jate hain
- Lambi videos (1 ghanta+) me waqt lag sakta hai — progress bar dekhta raho
