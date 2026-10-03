"""Transcribe a time slice of the finished video's audio with the strongest settings, to settle whether a diff from
verify_mix.py is the voice or the recogniser.

    python demo/check_mix_segment.py <start_sec> <end_sec>
"""
import glob
import os
import site
import subprocess
import sys

WORK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "work")
os.chdir(WORK)
for d in glob.glob(os.path.join(site.getusersitepackages(), "nvidia", "*", "bin")):
    os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]
from faster_whisper import WhisperModel

a, b = float(sys.argv[1]), float(sys.argv[2])
subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(a), "-to", str(b), "-i", "demo_final.mp4", "-vn", "-ac", "1", "-ar", "16000", "seg.wav"], check=True)
m = WhisperModel("large-v3", device="cuda", compute_type="float16")
for beam in (5, 1):
    segs, _ = m.transcribe("seg.wav", language="en", beam_size=beam, condition_on_previous_text=False)
    print(f"beam {beam}:", " ".join(s.text.strip() for s in segs))
