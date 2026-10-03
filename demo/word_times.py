"""Write work/word_times.json: when each word is spoken in each FINISHED narration clip.

record.mjs reads it so every on-screen action is tied to the word being said ("click the scenario as he says 'asks about that
same order'") instead of to a number of seconds that goes stale the moment the narration changes. Times are seconds from the
start of the clip, which is where the beat's mark sits in the video.

    python demo/word_times.py
"""
import glob
import json
import os
import re
import site

WORK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "work")
os.chdir(WORK)
for d in glob.glob(os.path.join(site.getusersitepackages(), "nvidia", "*", "bin")):
    os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]
from faster_whisper import WhisperModel

model = WhisperModel("large-v3", device="cuda", compute_type="float16")
out = {}
for b in json.load(open("narration_final/final.json"))["beats"]:
    segs, _ = model.transcribe(f"narration_final/{b['name']}.wav", language="en", beam_size=5,
                               word_timestamps=True, condition_on_previous_text=False)
    out[b["name"]] = [{"w": re.sub(r"[^a-z0-9']", "", w.word.lower()), "t": round(w.start, 2)}
                      for s in segs for w in s.words if re.sub(r"[^a-z0-9']", "", w.word.lower())]
    print(b["name"], len(out[b["name"]]), "words, last at", out[b["name"]][-1]["t"], "s")
json.dump(out, open("word_times.json", "w"), indent=1)
