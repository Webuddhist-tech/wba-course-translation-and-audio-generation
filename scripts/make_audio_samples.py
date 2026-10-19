#!/usr/bin/env python3
"""Make a small set of sample clips to compare voices and models before the full run.

Writes data/audio_samples/<lang>/<model>__<voice>__<item>.mp3 and an index.html with players.
  .venv/bin/python scripts/make_audio_samples.py [--voices Kore,Charon,Sulafat] [--langs en,hi,ne]
"""
import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_audio as g  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--langs", default="en,hi,ne")
ap.add_argument("--voices", default="Kore,Charon,Sulafat")
ap.add_argument("--lite-voice", default="Kore", help="also try the cheaper lite model with this voice ('' to skip)")
args = ap.parse_args()

cfg = g.load_config()
blocks = json.loads((g.ROOT / "data/extracted/blocks.json").read_text(encoding="utf-8"))
wanted = {"audio_d01_t01": "lesson", "audio_d01_q02": "quiz"}
out = g.ROOT / "data/audio_samples"
combos = [("gemini-3.8-flash-tts", v) for v in args.voices.split(",")]
if args.lite_voice:
    combos.append(("gemini-3.8-flash-lite-tts", args.lite_voice))

from google import genai  # noqa: E402
client = genai.Client(api_key=g.load_key())
jobs = []
for lang in args.langs.split(","):
    for it in g.build_items(blocks, [lang], {1}):
        if it["name"].rsplit("_", 1)[0] in wanted:
            for model, voice in combos:
                jobs.append((lang, model, voice, it))


def run(job):
    lang, model, voice, it = job
    c = {**cfg, "model": model}
    label = f"{model.replace('gemini-3.8-', '').replace('-tts', '')}__{voice}__{wanted[it['name'].rsplit('_', 1)[0]]}"
    item = {**it, "name": f"{label}.mp3"}
    info = g.make_file(client, c, item, out, voice)
    return lang, label, info


rows = {}
try:
    with ThreadPoolExecutor(max_workers=4) as pool:
        for lang, label, info in pool.map(run, jobs):
            rows.setdefault(lang, []).append((label, info["seconds"]))
            print(lang, label, info["seconds"], "s", flush=True)
except g.OutOfCredit as e:
    sys.exit(f"out of credit: {str(e)[:120]}")

html = ["<!doctype html><meta charset=utf-8><title>Audio samples</title><body style='font-family:sans-serif;max-width:760px;margin:2em auto'>",
        "<h1>Audio samples</h1><p>Same text, different voices/models. Listen and tell me which you prefer per language.</p>"]
for lang, items in rows.items():
    html.append(f"<h2>{lang}</h2>")
    for label, secs in sorted(items, key=lambda x: (x[0].split('__')[2], x[0])):
        html.append(f"<p><b>{label}</b> ({secs}s)<br><audio controls preload=none src='{lang}/{label}.mp3'></audio></p>")
(out / "index.html").write_text("\n".join(html), encoding="utf-8")
print("open", out / "index.html")
