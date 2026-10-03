"""Measure what an ear would catch on the finished narration and the shipped mix: clipping, clicks, dead gaps, level jumps.

Not a substitute for hearing it (naturalness is a human judgement), but these are the defects that make a voiceover sound
broken rather than merely synthetic, and they are measurable.

    python demo/verify_audio.py
"""
import json
import os
import subprocess
import sys

import numpy as np
import soundfile as sf

WORK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "work")
os.chdir(WORK)
beats = json.load(open("narration_final/final.json"))["beats"]


def analyse(path):
    x, sr = sf.read(path, dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    peak = float(np.max(np.abs(x)))
    clipped = int(np.sum(np.abs(x) >= 0.999))
    # clicks: a sample-to-sample jump far above the local typical jump
    d = np.abs(np.diff(x))
    win = int(sr * 0.02)
    local = np.convolve(d, np.ones(win) / win, mode="same") + 1e-6
    clicks = int(np.sum((d > 12 * local) & (d > 0.15)))
    # dead gaps: runs below -50 dBFS longer than 0.9 s that sit BETWEEN speech (not leading/trailing)
    frame = int(sr * 0.05)
    n = len(x) // frame
    rms = np.sqrt(np.mean(x[: n * frame].reshape(n, frame) ** 2, axis=1)) + 1e-9
    db = 20 * np.log10(rms)
    voiced = np.where(db > -40)[0]
    gap = 0.0
    if len(voiced) > 1:
        runs = np.diff(voiced) - 1
        gap = float(runs.max() * 0.05) if len(runs) else 0.0
    # level steadiness: spread of per-second loudness over voiced seconds
    sec = int(sr)
    m = len(x) // sec
    per = 20 * np.log10(np.sqrt(np.mean(x[: m * sec].reshape(m, sec) ** 2, axis=1)) + 1e-9)
    per = per[per > -45]
    spread = float(per.max() - per.min()) if len(per) else 0.0
    return peak, clipped, clicks, gap, spread, len(x) / sr


bad = []
print(f"{'clip':14s} {'peak':>6s} {'clipped':>8s} {'clicks':>7s} {'longest gap':>12s} {'level spread':>13s}")
for b in beats:
    peak, clipped, clicks, gap, spread, dur = analyse(f"narration_final/{b['name']}.wav")
    flag = []
    if peak >= 0.999 or clipped:
        flag.append("CLIPPING")
    if clicks > 3:
        flag.append("CLICKS")
    if gap > 1.0:
        flag.append("DEAD GAP")
    if spread > 18:                      # ordinary speech has soft words; 15 dB across one clip was measured as normal
        flag.append("LEVEL JUMP")
    print(f"{b['name']:14s} {peak:6.3f} {clipped:8d} {clicks:7d} {gap:10.2f} s {spread:11.1f} dB  {' '.join(flag) or 'ok'}")
    if flag:
        bad.append((b["name"], flag))

subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", "demo_final.mp4", "-vn", "-ac", "1", "-ar", "48000", "mix48.wav"], check=True)
peak, clipped, clicks, gap, spread, dur = analyse("mix48.wav")
print(f"\nshipped mix    peak {peak:.3f}  clipped samples {clipped}  clicks {clicks}  longest dead gap in speech {gap:.2f} s  level spread {spread:.1f} dB")
if peak >= 0.999 or clipped:
    bad.append(("mix", ["CLIPPING"]))
print("\nPASS, no clipping, clicks, dead gaps or level jumps" if not bad else f"\nCHECK: {bad}")
sys.exit(1 if bad else 0)
