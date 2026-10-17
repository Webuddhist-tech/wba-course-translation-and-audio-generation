#!/usr/bin/env python3
"""Write the current translations into a multilingual Open edX course export.

The multilingual export (each text block = <multilingual_text> holding English, Hindi,
Nepali and Tibetan html pages) is copied member by member into a NEW .tar.gz. Only the
files that need it are changed; everything else (videos, images, structure) is copied
unchanged, and the original export is never modified.

Changes made:
  1. Hindi / Nepali / Tibetan pages: every paragraph whose text differs from the current
     translation in data/translations/<lang>/ is replaced in place (page layout, styling
     and the audio-player block are untouched).
  2. English pages and quiz (problem) files: the typo fixes in data/corrections.json.
  3. Every quiz gets an audio block (English, Tibetan, Hindi, Nepali players) under the question
     and above the answer options; files are /static/<lang>-quiz-<problem id>.mp3.

A change that cannot be located exactly (e.g. a paragraph split over several formatting
spans) is skipped and listed in the report so it can be handled by hand.

Usage:
  python3 -I scripts/patch_course_export.py EXPORT.tar.gz --report conversion_report.json \
      --out OUT.tar.gz [--dry-run]
"""
import argparse
import difflib
import html
import io
import json
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from extract_course import extract  # noqa: E402

LANGS = {"hi": "Hindi", "ne": "Nepali", "bo": "Tibetan"}
WRAP = {"English", "Hindi", "Nepali", "Tibetan", "Your browser does not support the audio element."}


def replace_once(raw, old, new):
    """Replace the first verbatim occurrence of `old` in the page's visible text (never inside a tag or
    attribute), trying the raw and escaped spellings."""
    import re
    parts = re.split(r"(<[^>]*>)", raw)
    for esc in (lambda s: s, lambda s: html.escape(s, quote=False), html.escape):
        form = esc(old)
        for k in range(0, len(parts), 2):  # even indexes are text, odd indexes are tags
            i = parts[k].find(form)
            if i >= 0:
                parts[k] = parts[k][:i] + esc(new) + parts[k][i + len(form):]
                return "".join(parts), True
    return raw, False


def fix_attributes(raw, fixes):
    """Fix the same misspelled words inside alt/title attributes (image captions)."""
    import re
    n = 0
    for f in fixes:
        a, b = f["find"].split(), f["replace"].split()
        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
            if op == "replace" and i2 - i1 == j2 - j1:
                for x, y in zip(a[i1:i2], b[j1:j2]):
                    def sub(m):
                        nonlocal n
                        n += m.group(0).count(x)
                        return m.group(0).replace(x, y)
                    raw = re.sub(r'\b(?:alt|title)="[^"]*"', sub, raw)
    return raw, n


def rebuild_paragraph(raw, old, new):
    """Fallback for a paragraph split over several formatting spans: replace the whole <p>/<li><p>
    content with one span (keeping the first span's style) holding the new text."""
    import re
    for m in re.finditer(r"<p\b[^>]*>(.*?)</p>", raw, re.S):
        text = " ".join(s["text"] for s in extract(m.group(0))[0])
        if text == old:
            first = re.search(r"<span\b([^>]*)>", m.group(1))
            attrs = first.group(1) if first else ""
            inner = f"<span{attrs}>{html.escape(new, quote=False)}</span>"
            return raw[:m.start(1)] + inner + raw[m.end(1):], True
    return raw, False


def patch_translation(raw, current, label):
    """-> (new_raw, n_changed, problems). `current` = list of paragraphs that should be on the page."""
    page = [s["text"] for s in extract(raw)[0] if s["text"] not in WRAP]
    changed, problems, rebuilt = 0, [], []
    for op, a1, a2, b1, b2 in difflib.SequenceMatcher(None, page, current, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        # The conversion moved a leading heading into the component title, so the page lacks
        # the heading paragraph that the translation file still has.
        if op == "delete" and a1 == 0 and len(page[0]) < 80:
            continue
        if op == "insert" and b1 == 0 and b2 - b1 == 1 and len(current[0]) < 80:
            continue
        if op == "replace" and b1 == 0 and b2 - b1 == a2 - a1 + 1 and len(current[0]) < 80:
            b1 += 1
        if op == "replace" and a1 == 0 and a2 - a1 == b2 - b1 + 1 and len(page[0]) < 80:
            a1 += 1
        if op in ("delete", "insert") or a2 - a1 != b2 - b1:
            problems.append(f"{label}: structure differs ({op}: page {a2 - a1} vs translation {b2 - b1} paragraphs)")
            continue
        for old, new in zip(page[a1:a2], current[b1:b2]):
            raw, ok = replace_once(raw, old, new)
            if not ok:
                raw, ok = rebuild_paragraph(raw, old, new)
                rebuilt.append(label)
            if ok:
                changed += 1
            else:
                problems.append(f"{label}: could not locate paragraph: {old[:70]!r}")
    if rebuilt:
        problems.append(f"{label}: {len(rebuilt)} multi-span paragraph(s) rebuilt as one span (inline emphasis inside them is lost)")
    return raw, changed, problems


def apply_typos(raw, fixes, label):
    """Apply typo fixes. If the phrase is split by markup, fix just the misspelled word(s)."""
    import re
    changed, problems = 0, []
    for f in fixes:
        raw2, ok = replace_once(raw, f["find"], f["replace"])
        if not ok:
            ok = True
            raw2 = raw
            a, b = f["find"].split(), f["replace"].split()
            pairs = [(x, y) for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes()
                     if op == "replace" and i2 - i1 == j2 - j1 for x, y in zip(a[i1:i2], b[j1:j2])]
            for x, y in pairs:
                parts = re.split(r"(<[^>]*>)", raw2)
                pat = re.compile(r"(?<![\w])" + re.escape(x) + r"(?![\w])")
                if sum(len(pat.findall(p)) for p in parts[0::2]) == 1:
                    raw2 = "".join(pat.sub(y, p) if k % 2 == 0 else p for k, p in enumerate(parts))
                else:
                    ok = False
            ok = ok and bool(pairs)
        if ok:
            raw, changed = raw2, changed + 1
        else:
            problems.append(f"{label}: typo fix not found: {f['find'][:60]!r}")
    return raw, changed, problems


QUIZ_AUDIO_LANGS = [("English", "english"), ("Tibetan", "tibetan"), ("Hindi", "hindi"), ("Nepali", "nepali")]
QUIZ_AUDIO_LABEL = "Listen to this question and its answer options:"
QUIZ_AUDIO_CSS = """<style>
    .quiz-audio { margin: 12px 0 16px 0; }
    .quiz-audio .audio-label { font-weight: 600; margin: 0 0 8px 0; }
    .quiz-audio .audio-list-container { display: flex; flex-direction: column; gap: 12px; width: 100%; box-sizing: border-box;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
    .quiz-audio .audio-row { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; width: 100%; }
    .quiz-audio .audio-title { font-weight: 600; font-size: 16px; flex: 0 0 80px; color: #333; }
    .quiz-audio .audio-player-wrapper { flex: 1; min-width: 200px; }
    .quiz-audio audio { width: 100%; max-width: 400px; border-radius: 8px; }
</style>"""


def quiz_audio_block(problem_id, keep=None):
    """`keep` maps a language name to an existing real audio file that must be preserved."""
    keep = keep or {}
    rows = "\n".join(
        f'<div class="audio-row"><span class="audio-title">{name}</span>'
        f'<div class="audio-player-wrapper"><audio controls="controls" preload="none">'
        f'<source src="{keep.get(name, f"/static/{code}-quiz-{problem_id}.mp3")}" type="audio/mpeg"/></audio></div></div>'
        for name, code in QUIZ_AUDIO_LANGS)
    return (f'<div class="quiz-audio">\n<p class="audio-label">{QUIZ_AUDIO_LABEL}</p>\n'
            f'<div class="audio-list-container">\n{rows}\n</div>\n</div>\n')


def _balanced_div_end(raw, start):
    import re
    depth = 0
    for m in re.finditer(r"<div\b|</div>", raw[start:]):
        depth += 1 if m.group(0).startswith("<div") else -1
        if depth == 0:
            return start + m.end()
    return -1


def add_quiz_audio(raw, problem_id, label):
    """Put one audio block (English/Tibetan/Hindi/Nepali) between the question and the answer options.

    An earlier prototype block (its players, CSS, comments and the 'Listen to the audio above' sentence)
    is removed first. Players of the prototype that point at a real uploaded file are kept; players that
    only had the generic placeholder get a per-quiz placeholder name.
    """
    import re
    keep = {}
    i = raw.find('<div class="audio-list-container"')
    if i >= 0:
        j = _balanced_div_end(raw, i)
        for name, src in re.findall(r'<span class="audio-title">(\w+)</span>.*?<source src="([^"]+)"', raw[i:j], re.S):
            if not re.fullmatch(rf"/static/{name.lower()}-question-audio\.mp3", src):
                keep[name] = src
        raw = raw[:i] + raw[j:]
        raw = re.sub(r"<style>(?:(?!</style>).)*audio-list-container(?:(?!</style>).)*</style>\s*", "", raw, flags=re.S)
        raw = re.sub(r"<p>\s*<strong>Listen to the audio above[^<]*</strong>\s*</p>\s*", "", raw)
        raw = re.sub(r"<!--\s*\d\.[^>]*-->\s*", "", raw)
    m = re.search(r"<problem\b[^>]*>", raw)
    c = raw.find("<choicegroup")
    if not m or c < 0:
        return raw, [f"{label}: not a multiple choice problem, audio not added"]
    raw = raw[:c] + quiz_audio_block(problem_id, keep) + raw[c:]
    raw = raw[:m.end()] + "\n" + QUIZ_AUDIO_CSS + raw[m.end():]
    return raw, []


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("export")
    ap.add_argument("--report", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-english-typos", action="store_true", help="leave English pages and quizzes as they are")
    ap.add_argument("--no-quiz-audio", action="store_true", help="do not add the audio block to the quizzes")
    args = ap.parse_args()

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    corrections = {}
    for c in json.loads((ROOT / "data/corrections.json").read_text(encoding="utf-8")):
        corrections.setdefault(c["block"], []).append(c)

    # every quiz file in the export (found by a first quick pass over the archive index)
    with tarfile.open(args.export, "r:gz") as tf:
        quiz_names = [m.name for m in tf if m.name.startswith("course/problem/") and m.name.endswith(".xml")]

    # file in the export -> how to patch it
    jobs = {}
    for c in report["converted"]:
        old = c["old_component"]
        for code, name in LANGS.items():
            rec = json.loads((ROOT / "data/translations" / code / f"{old}.json").read_text(encoding="utf-8"))
            jobs[f"course/html/{c['children'][name]}.html"] = [("translation", f"{name} {old}", [s["text"] for s in rec["segments"]])]
            if old in corrections and not args.no_english_typos:
                jobs[f"course/html/{c['children'][name]}.html"].append(("attrs", f"{name} {old}", corrections[old]))
        if old in corrections and not args.no_english_typos:
            jobs[f"course/html/{c['children']['English']}.html"] = [("typos", f"English {old}", corrections[old]), ("attrs", f"English {old}", corrections[old])]
    if not args.no_english_typos:
        for bid, fixes in corrections.items():
            if not any(c["old_component"] == bid for c in report["converted"]):
                jobs[f"course/problem/{bid}.xml"] = [("typos", f"quiz {bid}", fixes)]

    if not args.no_quiz_audio:
        for name in quiz_names:
            jobs.setdefault(name, []).append(("quizaudio", name, None))
    stats = {"files": 0, "paragraphs": 0, "typos": 0, "members": 0, "quizzes": 0}
    problems, seen = [], set()
    out = None if args.dry_run else tarfile.open(args.out, "w:gz", compresslevel=6)
    try:
        with tarfile.open(args.export, "r:gz") as tf:
            for m in tf:
                stats["members"] += 1
                job = jobs.get(m.name)
                if job is None:
                    if out is not None:
                        out.addfile(m, tf.extractfile(m) if m.isfile() else None)
                    continue
                seen.add(m.name)
                raw = tf.extractfile(m).read().decode("utf-8")
                new = raw
                for kind, label, data in job:
                    if kind == "translation":
                        new, n, p = patch_translation(new, data, label)
                        stats["paragraphs"] += n
                    elif kind == "quizaudio":
                        new, p = add_quiz_audio(new, Path(m.name).stem, f"quiz {Path(m.name).stem}")
                        stats["quizzes"] += not p
                    elif kind == "attrs":
                        new, n = fix_attributes(new, data)
                        stats["typos"] += n
                        p = []
                    else:
                        new, n, p = apply_typos(new, data, label)
                        stats["typos"] += n
                    problems += p
                if new != raw:
                    stats["files"] += 1
                if out is not None:
                    data_bytes = new.encode("utf-8")
                    m.size = len(data_bytes)
                    out.addfile(m, io.BytesIO(data_bytes))
                if args.dry_run and len(seen) == len(jobs):
                    break
    finally:
        if out is not None:
            out.close()
    missing = set(jobs) - seen
    print(f"files changed: {stats['files']} | translated paragraphs replaced: {stats['paragraphs']} | typo fixes applied: {stats['typos']} | quizzes given the audio block: {stats['quizzes']}")
    print(f"members copied/read: {stats['members']}" + (" (dry run, stopped early)" if args.dry_run else f" -> {args.out}"))
    if missing:
        print(f"{len(missing)} expected files not found in the export: {sorted(missing)[:5]}")
    print(f"{len(problems)} things could not be patched:")
    for p in problems:
        print("  -", p)


if __name__ == "__main__":
    main()
