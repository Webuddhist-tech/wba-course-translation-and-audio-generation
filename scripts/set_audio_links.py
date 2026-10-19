#!/usr/bin/env python3
"""Point every audio player in a multilingual course export at its real audio file name.

Names follow generate_audio.py:
  lesson text pages   /static/audio_d<day>_t<nn>_<lang>.mp3
  quizzes             /static/audio_d<day>_q<nn>_<lang>.mp3      (lang: en, hi, ne, bo)

The day and number of a lesson page come from the course structure (chapter order, then the order of the
multilingual text blocks inside the chapter); quizzes use the order in data/extracted/blocks.json, the same
order the audio files were named with. Nothing else in the archive is changed.

Usage:
  python3 -I scripts/set_audio_links.py PATCHED.tar.gz --out NEW.tar.gz [--dry-run]
                                        [--base-url https://d123.cloudfront.net/audio]
"""
import argparse
import io
import json
import re
import sys
import tarfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import generate_audio as g  # noqa: E402

CODE = {"English": "en", "Hindi": "hi", "Nepali": "ne", "Tibetan": "bo"}
ROW = re.compile(r'(<span class="audio-title">(English|Tibetan|Hindi|Nepali)</span>.*?<source src=")[^"]*(")', re.S)


def set_sources(raw, stem, base="/static"):
    """Rewrite every player's file name; stem like 'audio_d01_t02'. -> (new text, number of players changed)
    `base` is what comes before the file name: /static (Studio uploads) or e.g. https://xxxx.cloudfront.net/audio."""
    n = 0

    def sub(m):
        nonlocal n
        n += 1
        return f"{m.group(1)}{base}/{stem}_{CODE[m.group(2)]}.mp3{m.group(3)}"

    return ROW.sub(sub, raw), n


def read_xml(archive):
    files = {}
    with tarfile.open(archive, "r:gz") as tf:
        for m in tf:
            if m.isfile() and m.name.endswith(".xml") and "/static/" not in m.name:
                files[m.name[len("course/"):]] = tf.extractfile(m).read().decode("utf-8")
    return files


def page_numbers(files):
    """html page id -> (day, text block number), by walking chapters in order."""
    root = ET.fromstring(files["course.xml"])
    course = ET.fromstring(files[f"course/{root.get('url_name')}.xml"])
    pages = {}
    for day, ch in enumerate(course.findall("chapter"), 1):
        n = 0
        for seq in ET.fromstring(files[f"chapter/{ch.get('url_name')}.xml"]):
            for vert in ET.fromstring(files[f"sequential/{seq.get('url_name')}.xml"]):
                for comp in ET.fromstring(files[f"vertical/{vert.get('url_name')}.xml"]):
                    if comp.tag == "multilingual_text":
                        n += 1
                        for lang_page in json.loads(comp.get("language_blocks")).values():
                            pages[lang_page.rsplit("/", 1)[-1]] = (day, n)
    return pages


def quiz_numbers():
    blocks = json.loads((ROOT / "data/extracted/blocks.json").read_text(encoding="utf-8"))
    return {it["block"]["id"]: (it["day"], it["number"])
            for it in g.build_items(blocks, ["en"], kinds="q")}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("archive")
    ap.add_argument("--out", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--base-url", default="/static",
                    help="what precedes the file name, e.g. https://d123.cloudfront.net/audio (default /static)")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    files = read_xml(args.archive)
    pages, quizzes = page_numbers(files), quiz_numbers()
    expected = {d: sum(1 for p, (dd, _) in pages.items() if dd == d) // 4 for d in range(1, 31)}
    blocks = json.loads((ROOT / "data/extracted/blocks.json").read_text(encoding="utf-8"))
    for d in range(1, 31):
        have = sum(1 for b in blocks if b["chapter_index"] == d and b["type"] == "html" and b.get("kind") == "text")
        if have != expected[d]:
            sys.exit(f"day {d}: {expected[d]} text blocks in the course file but {have} in blocks.json - stopping")
    print(f"{len(pages)} lesson pages in {len(set(pages.values()))} blocks, {len(quizzes)} quizzes mapped")

    stats = {"pages": 0, "quizzes": 0, "players": 0}
    out = None if args.dry_run else tarfile.open(args.out, "w:gz", compresslevel=6)
    try:
        with tarfile.open(args.archive, "r:gz") as tf:
            for m in tf:
                pid = Path(m.name).stem
                stem = None
                if m.name.startswith("course/html/") and m.name.endswith(".html") and pid in pages:
                    d, n = pages[pid]
                    stem, kind = f"audio_d{d:02d}_t{n:02d}", "pages"
                elif m.name.startswith("course/problem/") and m.name.endswith(".xml") and pid in quizzes:
                    d, n = quizzes[pid]
                    stem, kind = f"audio_d{d:02d}_q{n:02d}", "quizzes"
                if stem is None:
                    if out is not None:
                        out.addfile(m, tf.extractfile(m) if m.isfile() else None)
                    continue
                new, k = set_sources(tf.extractfile(m).read().decode("utf-8"), stem, base)
                if k not in ((1, 4) if kind == "pages" else (4,)):
                    sys.exit(f"{m.name}: expected {'1 player' if kind == 'pages' else '4 players'}, found {k}")
                stats[kind] += 1
                stats["players"] += k
                if out is not None:
                    data = new.encode("utf-8")
                    m.size = len(data)
                    out.addfile(m, io.BytesIO(data))
    finally:
        if out is not None:
            out.close()
    print(f"lesson pages updated: {stats['pages']} | quizzes updated: {stats['quizzes']} | players rewritten: {stats['players']}"
          + (" (dry run)" if args.dry_run else f" -> {args.out}"))


if __name__ == "__main__":
    main()
