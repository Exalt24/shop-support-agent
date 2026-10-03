"""Print a clip's loudness over time (dBFS per 0.5 s) so a flagged gap can be judged by looking at it.

    python demo/loudness_profile.py 05_legal [08_stores ...]
"""
import os
import sys

import numpy as np
import soundfile as sf

WORK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "work")
for name in sys.argv[1:]:
    x, sr = sf.read(os.path.join(WORK, "narration_final", name + ".wav"), dtype="float32")
    step = int(sr * 0.5)
    vals = [20 * np.log10(np.sqrt(np.mean(x[i:i + step] ** 2)) + 1e-9) for i in range(0, len(x) - step, step)]
    print(name, "dBFS per 0.5 s:")
    print("  " + " ".join(f"{v:4.0f}" for v in vals))
