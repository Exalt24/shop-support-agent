"""ADVISORY check on the SHIPPED video: cut each beat's window out of the final file, transcribe it with large-v3, and
compare word for word with the script.

It is advisory, not a gate, and the reason was measured: a recogniser's reading of the MIXED file varies with the silence
around a clip (a leading "So" dropped after 0.6s of silence, a final phrase blurred at the end of a slice) even when the
clip inside it is exact. The hard gates are verify_clips.py (each clip, raw and as encoded for the video, must match the
script word for word) and verify_placement.py (each clip is present in the shipped audio, intact, on its mark). Use this one
to LOOK for a problem, then settle it with those two.

An earlier version used word-level timestamps over the whole file and reported a phrase that was never spoken.

    python demo/verify_mix.py
"""
import difflib
import glob
import json
import os
import site
import subprocess
import sys

WORK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "work")
sys.path.insert(0, r"C:\Projects\Professional\Operations\tools\narration")
sys.argv = [sys.argv[0]]
import pick_narration as pn

os.chdir(WORK)
for d in glob.glob(os.path.join(site.getusersitepackages(), "nvidia", "*", "bin")):
    os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]
from faster_whisper import WhisperModel

beats = {b["name"]: b for b in json.load(open("narration_final/final.json"))["beats"]}
marks = json.load(open("assembled.json"))["marks_new"]
model = WhisperModel("large-v3", device="cuda", compute_type="float16")
failed = []
for name in sorted(marks, key=lambda k: marks[k]):
    a, b = max(0.0, marks[name] - 0.2), marks[name] + beats[name]["dur"] + 0.3
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{a:.2f}", "-to", f"{b:.2f}", "-i", "demo_final.mp4", "-vn", "-ac", "1", "-ar", "16000", "seg.wav"], check=True)
    segs, _ = model.transcribe("seg.wav", language="en", beam_size=5, condition_on_previous_text=False)
    heard = pn.words(" ".join(s.text.strip() for s in segs))
    want = pn.words(beats[name]["text"])
    sm = difflib.SequenceMatcher(None, want, heard)
    diffs = [(" ".join(want[i1:i2]) or "-", " ".join(heard[j1:j2]) or "-") for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]
    print(f"{name:12s} similarity {sm.ratio():.3f}  {'OK' if not diffs else 'CHANGED ' + str(diffs)}")
    if diffs:
        failed.append(name)
print("\nthe recogniser heard the script exactly in every beat" if not failed else
      f"\nADVISORY: the recogniser's reading differs in {failed}; settle with verify_clips.py and verify_placement.py")
