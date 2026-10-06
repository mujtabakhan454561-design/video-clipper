"""AI Video Clipper — Streamlit version (bilkul free hosting ke liye).
Streamlit Community Cloud par deploy karo, bina credit card ke.
"""
import os
import shutil
import tempfile
import threading
import time
import uuid

import streamlit as st

from clipper import Job, run_job

st.set_page_config(page_title="AI Video Clipper", page_icon="✂️", layout="centered")

st.title("✂️ AI Video Clipper")
st.caption("Long video ka link do — AI best moments nikal kar vertical short clips bana dega.")

with st.form("clip_form"):
    url = st.text_input("YouTube / TikTok / Facebook video link",
                        placeholder="https://www.youtube.com/watch?v=...")
    st.caption("— ya —")
    uploaded = st.file_uploader("Video file upload karo", type=["mp4", "mov", "mkv", "webm"])
    api_key = st.text_input("Gemini API key (AI highlights ke liye — free)",
                            type="password", placeholder="AIza...",
                            help="Free key: aistudio.google.com/apikey — nahi doge to auto mode chalega.")
    pexels_key = st.text_input("Pexels API key (AI B-roll visuals ke liye — free)",
                               type="password", placeholder="...",
                               help="Free key: pexels.com/api — nahi doge to B-roll off rahega.")
    moment = st.text_input("Find specific moment (optional)",
                           placeholder="e.g. When Sam talks about GPT-5.")
    c1, c2, c3 = st.columns(3)
    n_clips = c1.number_input("Kitne clips", 1, 8, 3)
    min_dur = c2.number_input("Min seconds", 10, 120, 20)
    max_dur = c3.number_input("Max seconds", 15, 180, 60)
    style = st.selectbox("Clip style",
                         ["default", "modern", "neon", "beast", "gold", "minimal"],
                         format_func=lambda s: {
                             "default": "Default (white)", "modern": "Modern (yellow)",
                             "neon": "Neon (cyan)", "beast": "Beast (red)",
                             "gold": "Gold", "minimal": "Minimal (small)"}[s])
    t1, t2, t3 = st.columns(3)
    remove_silence = t1.checkbox("Remove silences")
    hl_keywords = t2.checkbox("Highlight keywords", value=True)
    auto_emoji = t3.checkbox("Auto emoji", value=True)
    broll = st.checkbox("🎞 AI B-roll (video se related photos/videos)", value=True,
                        help="Pexels key chahiye — har clip me related visuals auto lag jayenge.")
    go = st.form_submit_button("✂️ Clips Banao", use_container_width=True)

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
                    pexels_key=pexels_key.strip(), broll=broll),
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
                if c.get("reason"):
                    st.caption(c["reason"])

st.divider()
st.caption("Tip: lambi videos me waqt lag sakta hai — page band na karo. "
           "Free server sone par 1 minute me jaag jata hai.")
