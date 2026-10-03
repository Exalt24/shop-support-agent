"""Merge a single regenerated beat's candidates into the main candidate set, so the picker sees them all.

    python merge_regen.py <main_dir> <regen_dir> <beat_name>

gen_narration.py rewrites candidates.json for whatever beats it was given, so regenerating one beat in place
would drop the other nine from the manifest. This copies the new wavs across and replaces only that beat's
manifest rows.
"""
import json
import os
import shutil
import sys

main_dir, regen_dir, beat = sys.argv[1:4]
main = json.load(open(os.path.join(main_dir, "candidates.json"), encoding="utf-8"))
regen = json.load(open(os.path.join(regen_dir, "candidates.json"), encoding="utf-8"))
new_rows = [r for r in regen["candidates"] if r["name"] == beat]
if not new_rows:
    sys.exit("no candidates for " + beat + " in " + regen_dir)
for r in new_rows:
    dst = os.path.join(main_dir, os.path.basename(r["file"]))
    shutil.copyfile(r["file"], dst)
    r["file"] = dst
main["candidates"] = [r for r in main["candidates"] if r["name"] != beat] + new_rows
json.dump(main, open(os.path.join(main_dir, "candidates.json"), "w", encoding="utf-8"), indent=1)
print("merged %d candidates for %s" % (len(new_rows), beat))
