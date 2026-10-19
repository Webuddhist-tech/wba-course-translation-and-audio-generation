#!/usr/bin/env python3
"""Build data/audio/review_day<NN>.html: each generated clip with its player and the text it speaks.

  .venv/bin/python scripts/audio_review_page.py --days 1
"""
import argparse
import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_audio as g  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--days", default="1")
args = ap.parse_args()
days = g.parse_days(args.days)
cfg = g.load_config()
blocks = json.loads((g.ROOT / "data/extracted/blocks.json").read_text(encoding="utf-8"))
out = g.ROOT / "data/audio"
manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
items = g.build_items(blocks, ["en", "hi", "ne", "bo"], days)
rows = {}
for it in items:
    rows.setdefault((it["day"], it["kind"], it["number"]), []).append(it)
page = ["<!doctype html><meta charset=utf-8><title>Audio review</title><body style='font-family:sans-serif;max-width:900px;margin:2em auto'>",
        f"<h1>Audio review: day(s) {args.days}</h1>"]
for (d, kind, n), group in rows.items():
    b = group[0]["block"]
    title = b.get("display_name") if kind == "t" else (b["question"][:90])
    page.append(f"<hr><h3>Day {d} · {'lesson text' if kind == 't' else 'quiz'} {n:02d} · {html.escape(title or '')}</h3>")
    for it in group:
        m = manifest.get(it["name"])
        if not m:
            page.append(f"<p><b>{it['lang']}</b>: not generated</p>")
            continue
        spoken = html.escape(" ".join(g.spoken_chunks(it, cfg)))
        page.append(f"<p><b>{it['lang']}</b> · {it['name']} · {m['seconds']}s<br>"
                    f"<audio controls preload=none src='{it['lang']}/{it['name']}'></audio><br>"
                    f"<small style='color:#555'>{spoken[:700]}{'…' if len(spoken) > 700 else ''}</small></p>")
dest = out / f"review_day{min(days):02d}.html"
dest.write_text("\n".join(page), encoding="utf-8")
print("wrote", dest)
