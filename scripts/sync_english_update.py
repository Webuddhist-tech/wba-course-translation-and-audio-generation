#!/usr/bin/env python3
"""Bring English edits made in the live course into blocks.json and re-translate only what changed.

The course was edited in Studio after the first export. A newer export (the
"multilingual" course, where each text block has an English version) holds the
current English. This script compares that English with data/extracted/blocks.json
and, for paragraphs that really changed, updates blocks.json and re-translates just
those paragraphs into hi/bo/ne.

Ignored on purpose:
  - differences that are only our own typo corrections (data/corrections.json is applied
    to the new English before comparing)
  - headings that the conversion moved into the component title
  - the language label and audio-player placeholder the conversion adds to every page

Usage:
  python3 -I scripts/sync_english_update.py EXPORT.tar.gz --report conversion_report.json [--dry-run]
"""
import argparse
import difflib
import json
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from extract_course import extract, seg_text, write_markdown  # noqa: E402

WRAP = {"English", "Hindi", "Nepali", "Tibetan", "Your browser does not support the audio element."}


def read_html(archive, ids):
    wanted = {f"course/html/{i}.html": i for i in ids}
    out = {}
    with tarfile.open(archive, "r:gz") as tf:
        for m in tf:
            if m.name in wanted:
                out[wanted[m.name]] = tf.extractfile(m).read().decode("utf-8")
                if len(out) == len(wanted):
                    break
    return out


def find_changes(blocks, report, html, corrections):
    """-> {block_id: [(index_in_old_segments, old_text, new_text), ...]} for real edits."""
    changes = {}
    for c in report["converted"]:
        old = c["old_component"]
        b = blocks.get(old)
        if b is None or b["type"] != "html":
            continue
        new = [s["text"] for s in extract(html[c["children"]["English"]])[0] if s["text"] not in WRAP]
        fixed = []
        for p in new:
            for x in corrections.get(old, []):
                p = p.replace(x["find"], x["replace"])
            fixed.append(p)
        ours = [s["text"] for s in b["segments"]]
        for op, a1, a2, b1, b2 in difflib.SequenceMatcher(None, ours, fixed, autojunk=False).get_opcodes():
            if op == "equal":
                continue
            A, B = list(range(a1, a2)), fixed[b1:b2]
            if op == "delete" and len(A) == 1 and a1 == 0:
                continue  # heading moved into the component title
            if a1 == 0 and len(A) == len(B) + 1 and len(ours[0]) < 60:
                A = A[1:]  # heading folded into the title
            if op == "insert" or len(A) != len(B):
                print(f"  ! {old}: structural change ({op}, {len(A)} -> {len(B)} paragraphs) — handle by hand")
                continue
            for i, new_text in zip(A, B):
                changes.setdefault(old, []).append((i, ours[i], new_text))
    return changes


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("export")
    ap.add_argument("--report", required=True, help="conversion_report.json (maps old blocks to the new English pages)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--model", default="gemini-pro-latest")
    args = ap.parse_args()

    blocks_path = ROOT / "data/extracted/blocks.json"
    all_blocks = json.loads(blocks_path.read_text(encoding="utf-8"))
    blocks = {b["id"]: b for b in all_blocks}
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    corrections = {}
    for c in json.loads((ROOT / "data/corrections.json").read_text(encoding="utf-8")):
        corrections.setdefault(c["block"], []).append(c)

    html = read_html(args.export, {c["children"]["English"] for c in report["converted"]})
    changes = find_changes(blocks, report, html, corrections)
    n = sum(len(v) for v in changes.values())
    print(f"{n} paragraphs changed in {len(changes)} blocks")
    for bid, items in changes.items():
        for i, old, new in items:
            print(f"  [{bid}] para {i}\n    OLD: {old[:140]}\n    NEW: {new[:140]}")
    if args.dry_run:
        return

    # 1. update blocks.json (+ review markdown) and keep a log of the English edits
    if changes:
        log = []
        for bid, items in changes.items():
            b = blocks[bid]
            for i, old, new in items:
                b["segments"][i]["text"] = new
                log.append({"block": bid, "segment": i, "old": old, "new": new})
            b["text"] = seg_text(b["segments"])
        blocks_path.write_text(json.dumps(all_blocks, ensure_ascii=False, indent=2), encoding="utf-8")
        (ROOT / "data/english_updates.json").write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
        write_markdown(all_blocks, ROOT / "data/extracted")

    # 2. re-translate only paragraphs whose English changed. Pending work is found by comparing the
    #    English stored with each translation to blocks.json, so an interrupted run can be repeated.
    from google import genai
    from translate import LANGS, load_key, source_hash, source_units, translate_block
    glossary = json.loads((ROOT / "data/glossary.json").read_text(encoding="utf-8"))
    client = None
    for bid, b in blocks.items():
        if b["type"] != "html" or b.get("kind") != "text":
            continue
        for lang in LANGS:
            path = ROOT / "data/translations" / lang / f"{bid}.json"
            rec = json.loads(path.read_text(encoding="utf-8"))
            todo = [i for i, s in enumerate(rec["segments"]) if s["en"] != b["segments"][i]["text"]]
            if not todo:
                continue
            client = client or genai.Client(api_key=load_key())
            partial = {**b, "segments": [{"type": b["segments"][i]["type"], "text": b["segments"][i]["text"]} for i in todo]}
            _, out, _ = translate_block(client, args.model, lang, partial, glossary)
            for i, tr in zip(todo, out):
                rec.setdefault("review_fixes", []).append(
                    {"seg": i, "severity": "source_update", "issue": "English text edited in the live course",
                     "before": rec["segments"][i]["text"]})
                rec["segments"][i]["en"], rec["segments"][i]["text"] = b["segments"][i]["text"], tr
            rec["source_hash"] = source_hash(source_units(b))
            path.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  re-translated {bid} [{lang}] ({len(todo)} paragraphs)")
    print("done" if client else "nothing to re-translate")


if __name__ == "__main__":
    main()
