"""Pull one frame per beat at a chosen offset after its mark (post-cut timeline) and tile them into contact
sheets that can be read. Offsets are the moments each beat's narration is describing what is on screen.

    python demo/frames.py           (run from anywhere; works in demo/work)
"""
import json
import os
import subprocess

WORK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "work")
os.chdir(WORK)
marks = json.load(open("assembled.json"))["marks_new"]
# beat -> seconds after its mark. Chosen at the moment the narration describes the screen.
AT = {
    "01_intro": 20.0, "02_dashboard": 8.5, "03_inbox": 9.0, "04_privacy": 12.5, "05_legal": 10.5,
    "06_audit": 10.5, "07_policy": 8.0, "08_stores": 10.0, "09_agent": 11.0, "09b_resolve": 10.0, "10_close": 5.0,
}
import sys
AT_MARK = len(sys.argv) > 1 and sys.argv[1] == "mark"      # frames AT each mark (+0.8s) instead of mid-beat
if AT_MARK:
    AT = {k: 0.8 for k in AT}
tag = "m_" if AT_MARK else ""
os.makedirs("frames", exist_ok=True)
names = []
for k, off in AT.items():
    t = marks[k] + off
    out = f"frames/{tag}{k}.png"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", "demo_final.mp4", "-frames:v", "1", out], check=True)
    names.append(out)
    print(f"{k}: mark {marks[k]:.2f}s, frame at {t:.2f}s")


def sheet(files, dest, cols):
    args = ["ffmpeg", "-y", "-v", "error"]
    for f in files:
        args += ["-i", f]
    rows = -(-len(files) // cols)
    layout = "|".join(f"{(i % cols) * 800}_{(i // cols) * 516}" for i in range(len(files)))   # 1600x1032 frame scaled to 800x516
    scaled = "".join(f"[{i}:v]scale=800:-1[s{i}];" for i in range(len(files)))
    stack = "".join(f"[s{i}]" for i in range(len(files)))
    filt = f"{scaled}{stack}xstack=inputs={len(files)}:layout={layout}[v]"
    args += ["-filter_complex", filt, "-map", "[v]", "-frames:v", "1", dest]
    subprocess.run(args, check=True)


sheet(names[:4], f"sheet_{tag}a.png", 2)
sheet(names[4:8], f"sheet_{tag}b.png", 2)
sheet(names[8:], f"sheet_{tag}c.png", 2)
print(f"sheets: sheet_{tag}a.png sheet_{tag}b.png sheet_{tag}c.png")
