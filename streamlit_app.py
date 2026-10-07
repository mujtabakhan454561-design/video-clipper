"""AI Video Clipper — Streamlit version (bilkul free hosting ke liye).
Streamlit Community Cloud par deploy karo, bina credit card ke.
"""
import os
import re
import shutil
import tempfile
import threading
import time
import uuid

import streamlit as st

from clipper import Job, run_job, test_gemini_key, test_stock_key

st.set_page_config(page_title="AI Video Clipper", page_icon="✂️", layout="centered")

HIST_DIR = os.path.join(tempfile.gettempdir(), "vc_history_24h")


def _prune_history():
    """24 ghante se purane clips delete karo."""
    try:
        os.makedirs(HIST_DIR, exist_ok=True)
        now = time.time()
        for f in os.listdir(HIST_DIR):
            p = os.path.join(HIST_DIR, f)
            try:
                if now - os.path.getmtime(p) > 24 * 3600:
                    os.remove(p)
            except OSError:
                pass
    except OSError:
        pass


def _save_to_history(workdir, clips):
    """Bane hue clips ko 24h history me copy karo."""
    _prune_history()
    for i, c in enumerate(clips):
        src = os.path.join(workdir, "clips", f"clip{i+1}.mp4")
        if not os.path.isfile(src):
            continue
        ts = time.strftime("%Y%m%d_%H%M%S")
        safe = re.sub(r"[^\w\-. ]", "_", c.get("title", f"clip{i+1}")[:30])
        dst = os.path.join(HIST_DIR, f"{ts}_{uuid.uuid4().hex[:6]}_{safe}.mp4")
        try:
            shutil.copy(src, dst)
        except OSError:
            pass


def _saved_key(name):
    """Streamlit Secrets me ek dafa save ki hui key (dobara paste nahi karni)."""
    try:
        return st.secrets.get(name, "") or ""
    except Exception:
        return ""


_SAVED_GEMINI = _saved_key("GEMINI_API_KEY")
_SAVED_PEXELS = _saved_key("PEXELS_API_KEY")

st.title("✂️ AI Video Clipper")
st.caption("Long video ka link do — AI best moments nikal kar vertical short clips bana dega.")

with st.container():
    url = st.text_input("YouTube / TikTok / Facebook video link",
                        placeholder="https://www.youtube.com/watch?v=...")
    st.caption("— ya —")
    uploaded = st.file_uploader("Video file upload karo", type=["mp4", "mov", "mkv", "webm"])
    api_key = st.text_input("Gemini API key (AI highlights ke liye — free)",
                            type="password", placeholder="AIza...",
                            value=_SAVED_GEMINI,
                            help="Ek dafa: app Settings → Secrets me GEMINI_API_KEY save karo, phir dobara nahi dalni padegi." + (" ✓ saved" if _SAVED_GEMINI else ""))
    pexels_key = st.text_input("Pexels / Pixabay API key (AI B-roll ke liye — free)",
                               type="password", placeholder="...",
                               value=_SAVED_PEXELS,
                               help="Ek dafa: app Settings → Secrets me PEXELS_API_KEY save karo, phir dobara nahi dalni padegi." + (" ✓ saved" if _SAVED_PEXELS else ""))
    moment = st.text_input("Find specific moment (optional)",
                           placeholder="e.g. When Sam talks about GPT-5.")
    if st.button("🔑 API keys test karo", help="Gemini aur Pexels/Pixabay key sahi hain ya nahi, foran check karo"):
        with st.spinner("Keys check ho rahi hain..."):
            ok1, msg1 = test_gemini_key(api_key)
            ok2, msg2 = test_stock_key(pexels_key)
        (st.success if ok1 else st.error)(f"Gemini: {msg1}")
        (st.success if ok2 else st.error)(f"Pexels/Pixabay: {msg2}")
    c1, c2, c3 = st.columns(3)
    n_clips = c1.number_input("Kitne clips", 1, 8, 3)
    min_dur = c2.number_input("Min seconds", 10, 120, 20)
    max_dur = c3.number_input("Max seconds", 15, 180, 60)
    style = st.selectbox("Clip style",
                         ["default", "modern", "neon", "beast", "gold", "minimal",
                          "hormozi", "boxed", "invert", "soft"],
                         format_func=lambda s: {
                             "default": "Default (white)", "modern": "Modern (yellow)",
                             "neon": "Neon (cyan)", "beast": "Beast (red)",
                             "gold": "Gold", "minimal": "Minimal (small)",
                             "hormozi": "Hormozi (bold)", "boxed": "Boxed (black box)",
                             "invert": "Invert (black)", "soft": "Soft (shadow)"}[s])
    r1, r2 = st.columns(2)
    ratio = r1.selectbox("Video ratio",
                         ["9:16", "4:5", "1:1", "16:9"],
                         format_func=lambda s: {
                             "9:16": "9:16 (Reels / TikTok / Shorts)",
                             "4:5": "4:5 (Instagram portrait)",
                             "1:1": "1:1 (Square)",
                             "16:9": "16:9 (YouTube landscape)"}[s])
    caption_color = r2.selectbox("Caption color",
                                 ["", "White", "Yellow", "Cyan", "Lime",
                                  "Red", "Orange", "Pink"],
                                 format_func=lambda s: "Style default" if s == "" else s,
                                 help="Screenshot wala style: keywords auto highlight honge.")
    t1, t2, t3 = st.columns(3)
    remove_silence = t1.checkbox("Remove silences")
    hl_keywords = t2.checkbox("Highlight keywords", value=True)
    auto_emoji = t3.checkbox("Auto emoji", value=True)
    c1, c2 = st.columns(2)
    show_captions = c1.checkbox("Captions dikhao", value=True)
    show_hook = c2.checkbox("Hook text (video ke upar)")
    if show_hook:
        hook_text = st.text_input("Hook text (khali chhoro to AI khud likhega)",
                                  placeholder="e.g. Wait for it... 😱",
                                  help="Khali chhoro to AI har clip ke liye catchy hook banayega (Gemini key chahiye).")
    else:
        hook_text = ""
    broll = st.checkbox("🎞 AI B-roll (video se related photos/videos)", value=True,
                        help="Pexels key chahiye — har clip me related visuals auto lag jayenge.")
    split_screen = st.checkbox("👥 Split screen (2 speakers upar-neeche ek saath)",
                               value=False,
                               help="9:16 me 2 chehre dhoond kar double roll banayega. 1 speaker ho to normal clip.")
    go = st.button("✂️ Clips Banao", use_container_width=True)

if go:
    if not url.strip() and uploaded is None:
        st.error("Pehle video link ya file do.")
        st.stop()

    workdir = os.path.join(tempfile.gettempdir(), "vc_" + uuid.uuid4().hex[:10])
    os.makedirs(workdir, exist_ok=True)
    upload_path = None
    if uploaded is not None:
        upload_path = os.path.join(workdir, "upload_" + uploaded.name)
        with open(upload_path, "wb") as f:
            f.write(uploaded.getbuffer())

    job = Job(job_id=uuid.uuid4().hex[:10], workdir=workdir)
    thread = threading.Thread(
        target=run_job, daemon=True,
        kwargs=dict(job=job, source_url=url.strip() or None,
                    upload_path=upload_path, api_key=api_key.strip(),
                    n_clips=int(n_clips), min_dur=int(min_dur),
                    max_dur=int(max_dur), moment=moment.strip(),
                    remove_silence=remove_silence, style=style,
                    hl_keywords=hl_keywords, auto_emoji=auto_emoji,
                    pexels_key=pexels_key.strip(), broll=broll,
                    ratio=ratio, captions=show_captions,
                    caption_color=caption_color,
                    show_hook=show_hook, hook_text=hook_text.strip(),
                    split_screen=split_screen),
    )
    thread.start()

    prog = st.progress(0, text="Shuru ho raha hai...")
    with st.status("Clips ban rahe hain...", expanded=False) as s:
        while thread.is_alive():
            time.sleep(3)
            prog.progress(min(job.progress, 100), text=job.message or "Kaam chal raha hai...")
        thread.join()
        s.update(label="Mukammal!", state="complete")
    prog.progress(100, text=job.message)

    if job.status == "error":
        st.error(job.message)
    else:
        st.success(f"🎬 {len(job.clips)} clips taiyar!")
        if "⚠" in job.message:
            st.warning(job.message)
        _save_to_history(workdir, job.clips)  # 24 ghante ke liye save
        for i, c in enumerate(job.clips):
            path = os.path.join(workdir, "clips", f"clip{i+1}.mp4")
            with st.expander(f"{c['title']} ({round(c['end']-c['start'])}s)", expanded=(i == 0)):
                if os.path.isfile(path):
                    st.video(path)
                    with open(path, "rb") as f:
                        st.download_button(f"⬇ Download clip {i+1}",
                                           data=f, file_name=f"clip{i+1}.mp4",
                                           mime="video/mp4",
                                           key=f"dl_{job.job_id}_{i}")
                if c.get("reason") and not c["reason"].startswith("Auto ("):
                    st.caption(c["reason"])

st.divider()

# ---- 24 ghante ki clip history ----
_prune_history()
try:
    hfiles = sorted(
        [f for f in os.listdir(HIST_DIR) if f.endswith(".mp4")],
        reverse=True) if os.path.isdir(HIST_DIR) else []
except OSError:
    hfiles = []
if hfiles:
    with st.expander(f"🕘 Pichle 24 ghante ke clips ({len(hfiles)})",
                     expanded=False):
        for f in hfiles[:30]:
            p = os.path.join(HIST_DIR, f)
            st.video(p)
            with open(p, "rb") as fh:
                st.download_button("⬇ Download", data=fh, file_name=f,
                                   mime="video/mp4", key=f"hist_{f}")

st.caption("Tip: lambi videos me waqt lag sakta hai — page band na karo. "
           "Free server sone par 1 minute me jaag jata hai.")
