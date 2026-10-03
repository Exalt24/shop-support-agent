"""Hard gate on the FINISHED narration clips: transcribe each with large-v3 and compare word for word with the script.

The picker checks the raw takes. The finishing pass (tempo change, breath, de-ess, compression) can still change what a
listener hears, and one shipped clip did exactly that ("loop" became "route" after finishing while the raw take was
clean). So this runs on the files the video actually uses, and exits 1 if any word differs.

    python demo/verify_clips.py [beat ...]
"""
import difflib
import glob
import json
import os
import site
import sys

WORK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "work")
sys.path.insert(0, r"C:\Projects\Professional\Operations\tools\narration")
sys.argv, _args = [sys.argv[0]], sys.argv[1:]          # the picker module parses sys.argv at import
import pick_narration as pn                            # reuse its normaliser so both gates agree on what counts as a match

os.chdir(WORK)
for d in glob.glob(os.path.join(site.getusersitepackages(), "nvidia", "*", "bin")):
    os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]
from faster_whisper import WhisperModel

beats = json.load(open("narration_final/final.json"))["beats"]
only = set(_args)
model = WhisperModel("large-v3", device="cuda", compute_type="float16")
failed = []


def encoded_like_the_video(name):
    """The clip after the same loudness normalisation and AAC encode the finished video applies. A clip can be exact on
    its own and still change under that encode (a leading 'So' lost, a final phrase blurred), so both are judged."""
    import subprocess
    out = f"_enc_{name}.m4a"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", f"narration_final/{name}.wav", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                    "-ar", "48000", "-c:a", "aac", "-b:a", "160k", out], check=True)
    return out


for b in beats:
    if only and b["name"] not in only:
        continue
    for label, path in (("clip", f"narration_final/{b['name']}.wav"), ("as-encoded", encoded_like_the_video(b["name"]))):
        # TWO independent decodes, and the clip passes if EITHER reads the script exactly. One decode flips on harmless
        # things (a repeated "who" hallucinated by beam 5 on audio that beam 1 reads perfectly; a soft first "The"), but a
        # genuinely wrong word, like "want" for "bought" or "route" for "loop", came out wrong in EVERY decode.
        want = pn.words(b["text"])
        results = []
        for beam in (5, 1):
            segs, _ = model.transcribe(path, language="en", beam_size=beam, condition_on_previous_text=False)
            heard = pn.words(" ".join(s.text.strip() for s in segs))
            results.append((pn.word_diffs(want, heard), difflib.SequenceMatcher(None, want, heard).ratio()))
        best = min(results, key=lambda r: len(r[0]))
        diffs, ratio = best
        agree = f" (the other decode: {results[0][0] if best is results[1] else results[1][0]})" if (results[0][0] and not results[1][0]) or (results[1][0] and not results[0][0]) else ""
        print(f"{b['name']:12s} {label:10s} similarity {ratio:.3f}  {'OK' + agree if not diffs else 'CHANGED ' + str(diffs)}")
        if diffs and b["name"] not in failed:
            failed.append(b["name"])
print("\nPASS, every finished clip says exactly what the script says" if not failed else f"\nFAIL, words changed in: {failed}")
sys.exit(1 if failed else 0)
