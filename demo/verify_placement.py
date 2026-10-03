"""Prove each finished clip is present, intact and in the right place inside the shipped video's audio.

For every beat: cut the mix at the beat's mark, normalise both signals to unit energy, and find the best alignment of the
clip against the mix by normalised cross-correlation. A correlation near 1.0 at a lag near 0 means the mix carries exactly
that clip at exactly that place, so what the clip says is what the viewer hears, whatever a recogniser makes of the mix.

    python demo/verify_placement.py
"""
import json
import os
import subprocess
import sys

import numpy as np
import soundfile as sf
from scipy.signal import fftconvolve

WORK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "work")
os.chdir(WORK)
SR = 16000
subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", "demo_final.mp4", "-vn", "-ac", "1", "-ar", str(SR), "mix_check.wav"], check=True)
mix, _ = sf.read("mix_check.wav", dtype="float32")
beats = {b["name"]: b for b in json.load(open("narration_final/final.json"))["beats"]}
marks = json.load(open("assembled.json"))["marks_new"]

worst = 1.0
bad = []
for name in sorted(marks, key=lambda k: marks[k]):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", f"narration_final/{name}.wav", "-ac", "1", "-ar", str(SR), "clip_check.wav"], check=True)
    clip, _ = sf.read("clip_check.wav", dtype="float32")
    lo = max(0, int((marks[name] - 0.5) * SR))
    hi = min(len(mix), int((marks[name] + beats[name]["dur"] + 0.5) * SR))
    seg = mix[lo:hi]
    # normalised cross-correlation of the clip against the mix slice
    c = fftconvolve(seg, clip[::-1], mode="valid")
    energy = np.sqrt(np.convolve(seg ** 2, np.ones(len(clip)), mode="valid") * np.sum(clip ** 2)) + 1e-9
    ncc = c / energy
    k = int(np.argmax(ncc))
    lag = (lo + k) / SR - marks[name]
    peak = float(ncc[k])
    worst = min(worst, peak)
    ok = peak >= 0.95 and abs(lag) <= 0.25
    print(f"{name:12s} correlation {peak:.3f}  offset from its mark {lag * 1000:+6.0f} ms  {'OK' if ok else 'CHECK'}")
    if not ok:
        bad.append(name)
print(f"\nworst correlation {worst:.3f}")
print("PASS, every clip is in the shipped audio, intact and on its mark" if not bad else f"FAIL: {bad}")
sys.exit(1 if bad else 0)
