# The demo video

`demo/` regenerates a 3 minute narrated walkthrough of the console. `beats.json` is the script, `record.mjs` drives the real UI with Playwright and takes a mark at each moment a scene is actually on screen, `assemble.py` places the voice at those marks, cuts dead air and burns the subtitles into a bar below the frame, and `frames.py` pulls a frame at every mark so each one can be checked against its subtitle. Voice generation uses the shared tools in `Operations/tools/narration/` (three candidates per beat, an ASR gate, then a finishing pass). Record against a production build, not `next dev`.

```
python -m console_api.seed                                  # clean fake data; the run claims and resolves a case
cd console_web && npm run build && npx next start -p 3100   # and the API on 8010 with CONSOLE_ALLOWED_ORIGINS=http://localhost:3100
node demo/record.mjs && python demo/assemble.py && python demo/frames.py mark
```

The narration is Daniel's own voice, cloned and generated. The recording touches only the local app and the development store, and `record.mjs` refuses a non-local URL.
