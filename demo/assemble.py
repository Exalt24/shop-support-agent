"""Mux the recording with narration + burned subtitles, and cut the dead air.

Rules being honoured (each learned by breaking it):
  - Audio sits at the REAL on-screen mark, never at a cumulative audio offset.
  - Subtitles live in a bottom LETTERBOX BAR made with pad, never over the UI, and carry
    the FULL verbatim narration. Long lines are shown as PROGRESSIVE cues of <=2 lines,
    because a 430-character beat wraps to 6 lines and overflowed the bar on take one.
  - ffmpeg runs FROM this dir so `subtitles=` gets a relative path (a Windows drive-colon
    breaks the filter parser).
  - Loudness is normalised ONCE on the finished mix, not per clip.
  - GAP CAPPING: waiting for real data to render left ~40s of silence between beats. Those
    stretches are cut, and EVERY mark is then recomputed on the post-cut timeline, because
    a cut shifts everything after it.
"""
import json
import os
import re
import subprocess
import sys

# Copied from Operations/tools/narration/assemble.py for this demo. The only change: it works in demo/work/
# (marks.json, narration_final/, and the outputs live there), so the shared tool is left as it was.
SCRATCH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "work")
os.chdir(SCRATCH)
BAR, W, H = 132, 1600, 900
LEAD, TAIL = 0.9, 0.55          # keep before a mark / after its narration
MAX_GAP = 1.4                   # longest silence allowed between beats


def sh(args, capture=False):
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"FAILED: {' '.join(map(str, args))[:150]}\n{r.stderr[-1500:]}")
    return r.stdout if capture else None


def ts(sec):
    h, m, s = int(sec // 3600), int(sec % 3600 // 60), sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def lines_of(text, width=78):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 <= width:
            cur = f"{cur} {w}".strip()
        else:
            lines.append(cur); cur = w
    if cur:
        lines.append(cur)
    return lines


def cues(text, start, end, max_lines=2, width=78):
    """Progressive cues: <=2 lines at a time, each cue's slice of the beat weighted by
    its character count so the text tracks the voice."""
    ls = lines_of(text, width)
    groups = [ls[i:i + max_lines] for i in range(0, len(ls), max_lines)]
    total = sum(len(" ".join(g)) for g in groups) or 1
    out, t, span = [], start, max(0.4, end - start)
    for g in groups:
        share = span * (len(" ".join(g)) / total)
        out.append((t, min(end, t + share), "\\N".join(g)))
        t += share
    return out


def main():
    m = json.load(open("marks.json"))
    by = {b["name"]: b for b in json.load(open("narration_final/final.json"))["beats"]}
    marks = m["marks"]
    order = sorted(marks, key=lambda k: marks[k])
    video = m["video"].replace("\\", "/")
    vdur = float(sh(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=nw=1:nk=1", video], capture=True).strip())

    # ---- 1. keep-windows around each beat, merged, with capped gaps ----
    segs = []
    for k in order:
        a = max(0.0, marks[k] / 1000.0 - LEAD)
        b = min(vdur, marks[k] / 1000.0 + by[k]["dur"] + TAIL)
        if segs and a - segs[-1][1] <= MAX_GAP:
            segs[-1][1] = b                      # gap already short: keep it continuous
        else:
            if segs:
                segs[-1][1] = min(vdur, segs[-1][1] + MAX_GAP / 2)
                a = max(segs[-1][1], a - MAX_GAP / 2)
            segs.append([a, b])
    kept = sum(b - a for a, b in segs)
    print(f"source {vdur:.1f}s -> keeping {kept:.1f}s in {len(segs)} segment(s) "
          f"(cut {vdur - kept:.1f}s of dead air)")

    # ---- 2. new mark positions on the post-cut timeline ----
    def remap(t):
        acc = 0.0
        for a, b in segs:
            if t < a:
                return acc
            if t <= b:
                return acc + (t - a)
            acc += b - a
        return acc
    nm = {k: remap(marks[k] / 1000.0) for k in order}
    for k in order:
        print(f"  {k}: {marks[k]/1000:7.2f}s -> {nm[k]:7.2f}s")

    # ---- 3. cut + concat video ----
    parts = []
    for i, (a, b) in enumerate(segs):
        out = f".seg{i}.mp4"
        sh(["ffmpeg", "-y", "-v", "error", "-ss", f"{a:.3f}", "-to", f"{b:.3f}",
            "-i", video, "-an", "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
            "-r", "30", out])
        parts.append(out)
    with open(".concat.txt", "w") as fh:
        for p in parts:
            fh.write(f"file '{p}'\n")
    sh(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", ".concat.txt",
        "-c", "copy", ".cut.mp4"])
    cutdur = float(sh(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                       "-of", "default=nw=1:nk=1", ".cut.mp4"], capture=True).strip())
    print(f"cut video: {cutdur:.1f}s")

    # ---- 4. subtitles on the NEW timeline ----
    srt, n = [], 0
    for i, k in enumerate(order, 1):
        start = nm[k]
        end = start + by[k]["dur"]
        if i < len(order):
            end = min(end, nm[order[i]] - 0.10)
        for a, b, txt in cues(by[k]["text"], start, end):
            n += 1
            srt.append(f"{n}\n{ts(a)} --> {ts(b)}\n{txt}\n")
    open("subs.srt", "w", encoding="utf-8").write("\n".join(srt))
    print(f"subs.srt: {n} cues")

    # ---- 5. mux ----
    ins, filt = ["-i", ".cut.mp4"], []
    for i, k in enumerate(order):
        ins += ["-i", f"narration_final/{k}.wav"]
        d = int(round(nm[k] * 1000))
        filt.append(f"[{i+1}:a]adelay={d}|{d}[a{i}]")
    filt.append("".join(f"[a{i}]" for i in range(len(order))) +
                f"amix=inputs={len(order)}:normalize=0[amix]")
    filt.append("[amix]alimiter=limit=0.891[aout]")
    style = ("FontName=Segoe UI,FontSize=15,PrimaryColour=&H00FFFFFF,"
             "OutlineColour=&HFF000000,BorderStyle=1,Outline=2,Shadow=0,"
             "MarginV=12,Bold=1,Alignment=2")
    filt.append(f"[0:v]pad={W}:{H+BAR}:0:0:black,"
                f"subtitles=subs.srt:force_style='{style}',"
                f"fade=t=in:st=0:d=0.5,fade=t=out:st={cutdur-0.6:.2f}:d=0.5[vout]")
    print("mux")
    sh(["ffmpeg", "-y", "-v", "error", *ins, "-filter_complex", ";".join(filt),
        "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-crf", "20",
        "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", "demo_raw.mp4"])

    # ---- 6. loudness, two pass ----
    out = subprocess.run(["ffmpeg", "-i", "demo_raw.mp4", "-af",
                          "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
                          "-f", "null", "-"], capture_output=True, text=True).stderr
    v = dict(re.findall(r'"(input_[a-z_]+)"\s*:\s*"([-\d.a-zA-Z]+)"', out))
    print("  measured:", v)
    meas = (f":measured_I={v['input_i']}:measured_TP={v['input_tp']}"
            f":measured_LRA={v['input_lra']}:measured_thresh={v['input_thresh']}"
            if {"input_i", "input_tp", "input_lra", "input_thresh"} <= v.keys() else "")
    sh(["ffmpeg", "-y", "-v", "error", "-i", "demo_raw.mp4", "-af",
        f"loudnorm=I=-16:TP=-1.5:LRA=11:linear=true{meas}", "-c:v", "copy",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "demo_final.mp4"])

    for p in parts:
        os.remove(p)
    d = float(sh(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                  "-of", "default=nw=1:nk=1", "demo_final.mp4"], capture=True).strip())
    pk = subprocess.run(["ffmpeg", "-i", "demo_final.mp4", "-af", "volumedetect",
                         "-f", "null", "-"], capture_output=True, text=True).stderr
    mx = re.search(r"max_volume: ([-\d.]+) dB", pk)
    print(f"\ndemo_final.mp4  {d:.1f}s ({d/60:.2f} min)  "
          f"{os.path.getsize('demo_final.mp4')/1e6:.1f} MB  "
          f"max_volume={mx.group(1) if mx else '?'} dB")
    json.dump({"video": "demo_final.mp4", "duration": d, "marks_new": nm},
              open("assembled.json", "w"), indent=1)


if __name__ == "__main__":
    main()
