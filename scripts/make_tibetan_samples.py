#!/usr/bin/env python3
"""Tibetan sample clips from the Monlam text-to-speech API, to compare voices and English-word handling.

Writes data/audio_samples/bo/<voice>__<variant>.mp3 and data/audio_samples/index_bo.html.
Needs MONLAM_API_KEY and MONLAM_API_URL in .env. Run with the project environment:
  .venv/bin/python scripts/make_tibetan_samples.py [--voices lhasa_female,lhasa_male]
"""
import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_audio as g  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--voices", default="lhasa_female,lhasa_male,yangchen_lhasa_female,sonamtsering_lhasa_male")
args = ap.parse_args()
env = dict(l.strip().split("=", 1) for l in (g.ROOT / ".env").read_text().splitlines() if "=" in l and not l.startswith("#"))

tr = lambda bid: json.loads((g.ROOT / f"data/translations/bo/{bid}.json").read_text(encoding="utf-8"))
lesson = g.clean(tr("html1")["segments"][0]["text"])
q = tr("e4d727e077d54a76b33c9a5f8abce70e")
OPT = ["དང་པོ", "གཉིས་པ", "གསུམ་པ", "བཞི་པ"]  # draft wording: needs a Tibetan speaker's check
quiz = " ".join([g.clean(q["question"]["text"])] + [f"གདམ་ག་{OPT[i]}། {g.clean(c['text'])}" for i, c in enumerate(q["choices"])])
# English words spelled in Tibetan script: my own quick attempt, only to hear whether it helps
SPELL = {"social media": "སོ་ཤལ་མི་ཌི་ཡ", "storytelling": "སྟོ་རི་ཊེལ་ལིང", "content creation": "ཀོན་ཊེནཊ་ཀྲི་ཨེ་ཤན",
         "digital marketplace": "ཌི་ཇི་ཊལ་མར་ཀེ་ཊ་པ་ལེས"}


def spell(t):
    for k, v in SPELL.items():
        t = t.replace(k, v)
    return t


texts = {"lesson_english-as-written": lesson, "lesson_english-spelled-in-tibetan": spell(lesson),
         "quiz_english-as-written": quiz, "quiz_english-spelled-in-tibetan": spell(quiz)}
out = g.ROOT / "data/audio_samples/bo"


def run(job):
    voice, label = job
    r = httpx.post(env["MONLAM_API_URL"] + "/api/v1/text-to-speech/stream", json={"text": texts[label], "voice_name": voice},
                   headers={"X-API-Key": env["MONLAM_API_KEY"]}, timeout=300)
    r.raise_for_status()
    g.to_mp3(r.content, out / f"{voice}__{label}.mp3", 64, "I=-19:TP=-2:LRA=11")
    return voice, label


jobs = [(v, l) for v in args.voices.split(",") for l in texts]
with ThreadPoolExecutor(max_workers=3) as pool:
    done = list(pool.map(run, jobs))
html = ["<!doctype html><meta charset=utf-8><title>Tibetan samples</title><body style='font-family:sans-serif;max-width:800px;margin:2em auto'>",
        "<h1>Tibetan samples (Monlam)</h1><p>For each voice: the same text with English words as written, and with them spelled in Tibetan script (my own rough spelling, only a test).</p>"]
for v in args.voices.split(","):
    html.append(f"<h2>{v}</h2>")
    for l in texts:
        html.append(f"<p><b>{l}</b><br><audio controls preload=none src='bo/{v}__{l}.mp3'></audio></p>")
(g.ROOT / "data/audio_samples/index_bo.html").write_text("\n".join(html), encoding="utf-8")
print(len(done), "clips written; open data/audio_samples/index_bo.html")
for l, t in texts.items():
    print("\n##", l, f"({len(t)} chars)\n", t[:260])
