#!/usr/bin/env python3
"""Targeted repair of individual translated segments with Gemini.

Run after apply_review_fixes.py. Finds segments that still have a known,
systematic problem and asks Gemini to correct only that problem:

  term       a recurring mistranslation (RULES below) — Gemini decides from the
             English context whether the word really needs changing
  foreign    letters from another script (e.g. Burmese inside Tibetan)
  slash      a glossary "a / b" alternative copied verbatim
  anchor     the course label "Anchor Line" not using the fixed per-language label

A repair is accepted only if it removes the problem without rewriting the
segment (most of the text must stay the same). Each change is recorded in the
translation file under "review_fixes" with severity "repair".

Usage:
  python3 -I scripts/repair_translations.py [--langs hi,bo,ne] [--dry-run]
"""
import argparse
import difflib
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from google import genai
from google.genai import types

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from translate import LANGS, foreign_scripts, load_key  # noqa: E402

TR = ROOT / "data/translations"

# (lang, English pattern, suspect rendering, guidance for Gemini)
RULES = [
    ("bo", r"\bmessage", r"བརྡ་ལན", "བརྡ་ལན means 'reply'. Where the English means a message (what someone communicates), use བརྗོད་དོན or བསླབ་བྱ (Dharma message: ཆོས་ཀྱི་བསླབ་བྱ); messenger: འཕྲིན་སྐྱེལ་མཁན."),
    ("bo", r"master", r"བྱང་ཆུབ", "བྱང་ཆུབ means Buddhist enlightenment. Where the English means mastery of a skill, use རྩལ་ལ་མཁས་པ / མཁས་པ."),
    ("bo", r"climax", r"ཛ་དྲག", "ཛ་དྲག means 'emergency'. The climax of a story is རྩེར་སོན་པའི་དུས་ཚོད."),
    ("bo", r"resonan", r"ཟུག", "སེམས་ལ་ཟུག sounds like 'piercing/stinging the mind'. For resonance use སེམས་ལ་ཕོག་པ."),
    ("bo", r"objection", r"དགག་བྱ", "དགག་བྱ is the philosophical 'object of negation'. Objections/doubts of an audience: དོགས་པ."),
    ("bo", r"enlightenment", r"བྱང་ཆུབ", "If 'Enlightenment' here is the step of the 5-part content framework (a hook that sheds light on the audience's pain), use གསལ་སྟོན. If it really means Buddhist awakening, keep བྱང་ཆུབ."),
    ("hi", r"\bvalue", r"मूल्य", "If 'value' means practical usefulness of content, use 'काम की बात (value)' or 'वैल्यू'. If it means moral/ethical values or price, keep मूल्य."),
    ("hi", r"\btalk", r"बातचीत", "If 'talk' means a Dharma talk/teaching, use प्रवचन. If it means talking or a conversation, keep बातचीत."),
    ("hi", r"piece of content", r"हिस्सा", "'Piece of content' means one post or video, not 'part of the content'."),
    ("hi", r"enlightenment", r"बोधि", "If 'Enlightenment' here is the step of the 5-part content framework (shedding light on the pain), use 'रोशनी डालना'. If it means Buddhist awakening, keep बोधि."),
    ("ne", r"vulnerab", r"संवेदनशील", "संवेदनशीलता means sensitivity. Vulnerability (openly showing one's struggles) is 'आफ्नो कमजोरी खुलेर देखाउनु'."),
    ("ne", r"\bvalue", r"मूल्य", "If 'value' means practical usefulness of content, use 'काम लाग्ने कुरा (value)'. If it means moral/ethical values or price, keep मूल्य."),
    ("ne", r"authentic", r"वास्तविकता", "For authenticity use प्रामाणिकता (वास्तविकता means reality)."),
    ("ne", r"urgen", r"तत्परता", "तत्परता means readiness. Urgency is 'तुरुन्तै गर्नुपर्ने भावना'."),
    ("ne", r"\btalk", r"कुराकानी|वार्तालाप", "If 'talk' means a Dharma talk/teaching, use प्रवचन. If it means talking or a conversation, keep it."),
    ("ne", r"enlightenment", r"बोधि", "If 'Enlightenment' here is the step of the 5-part content framework (shedding light on the pain), use 'प्रकाश पार्नु'. If it means Buddhist awakening, keep बोधि."),
    ("ne", r"relatab", r"सान्दर्भिक", "सान्दर्भिक means 'relevant'. Relatable is 'आफूसँग जोडिएको महसुस हुने'."),
]
ANCHOR = {"hi": "मुख्य बात", "ne": "मुख्य भनाइ", "bo": "གནད་འགག་གི་སྐད་ཆ།"}
ANCHOR_WORKBOOK = {"hi": "वर्कबुक के लिए मुख्य बात", "ne": "Workbook को लागि मुख्य भनाइ",
                   "bo": "ལས་དེབ་ཀྱི་གནད་འགག་གི་སྐད་ཆ།"}


def segments(rec):
    if rec["type"] == "html":
        return [(s["en"], s) for s in rec["segments"]]
    return [(rec["question"]["en"], rec["question"])] + [(c["en"], c) for c in rec["choices"]]


def find_tasks(lang, glossary):
    slash_alts = [t[lang] for t in glossary["terms"] if "/" in t[lang]]
    tasks = []
    for f in sorted((TR / lang).glob("*.json")):
        rec = json.loads(f.read_text(encoding="utf-8"))
        for i, (en, holder) in enumerate(segments(rec)):
            tr, why = holder["text"], []
            for l, en_re, bad, guide in RULES:
                if l == lang and re.search(en_re, en, re.I) and re.search(bad, tr):
                    why.append(("term", guide))
            fs = foreign_scripts(tr, en, lang)
            if fs:
                why.append(("foreign", f"The translation contains {'/'.join(fs)} characters that are not "
                            f"{LANGS[lang]['name']}. Replace each such word with the correct {LANGS[lang]['name']} word."))
            for a in slash_alts:
                if a.rstrip("།") in tr and "/" not in en:
                    why.append(("slash", f"'{a}' is a glossary list of alternatives copied verbatim. Use only the one option that fits; no slash."))
            m = re.search(r"Anchor Line( for Workbook)?", en)
            if m:
                label = ANCHOR_WORKBOOK[lang] if m.group(1) else ANCHOR[lang]
                if label.rstrip("།") not in tr:
                    why.append(("anchor", f"The segment starts with the course label 'Anchor Line'. Its label must be exactly "
                                f"'{label}' (followed by ':' in Hindi/Nepali). Replace whatever label the translation "
                                f"currently starts with by this one; keep the ✨ if present."))
            if why:
                tasks.append({"lang": lang, "file": f, "seg": i, "en": en, "current": tr, "why": why})
    return tasks


def repair(client, model, task):
    instructions = "\n".join(f"- {g}" for _, g in task["why"])
    prompt = f"""You correct one segment of a {LANGS[task['lang']]['name']} translation of a Buddhist course \
about sharing the Dharma on social media. The text will be read aloud.
Style: {LANGS[task['lang']]['style']}

English source:
{task['en']}

Current translation:
{task['current']}

Fix ONLY these issues, if they really apply in this context; change nothing else (keep English \
technical terms, names, numbers and the rest of the wording exactly as they are):
{instructions}

If the current translation is already correct for an issue, leave that part unchanged."""
    resp = client.models.generate_content(
        model=model, contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema={"type": "object", "properties": {
                "changed": {"type": "boolean"}, "text": {"type": "string"}, "note": {"type": "string"}},
                "required": ["changed", "text"]}))
    return json.loads(resp.text)


def acceptable(task, new):
    """Reject repairs that rewrite the segment or leave/introduce problems."""
    lang = task["lang"]
    if not new or foreign_scripts(new, task["en"], lang):
        return False, "foreign script or empty"
    if not LANGS[lang]["script"].search(new):
        return False, "target script missing"
    ratio = difflib.SequenceMatcher(None, task["current"], new).ratio()
    floor = 0.5 if len(task["current"]) < 60 else 0.7
    if ratio < floor:
        return False, f"rewrote too much (similarity {ratio:.2f})"
    return True, ""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--langs", default="hi,bo,ne")
    ap.add_argument("--model", default="gemini-pro-latest")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true", help="only list what would be repaired")
    args = ap.parse_args()
    glossary = json.loads((ROOT / "data/glossary.json").read_text(encoding="utf-8"))
    tasks = [t for lang in args.langs.split(",") for t in find_tasks(lang, glossary)]
    kinds = {}
    for t in tasks:
        for k, _ in t["why"]:
            kinds[(t["lang"], k)] = kinds.get((t["lang"], k), 0) + 1
    print(f"{len(tasks)} segments to repair:", {f"{l}/{k}": n for (l, k), n in sorted(kinds.items())})
    if args.dry_run or not tasks:
        return

    client = genai.Client(api_key=load_key())

    def run(t):
        for _ in range(3):
            try:
                r = repair(client, args.model, t)
                if not r["changed"] or r["text"].strip() == t["current"]:
                    return t, None, "model kept it unchanged"
                ok, why = acceptable(t, r["text"].strip())
                if ok:
                    return t, r["text"].strip(), r.get("note", "")
                last = why
            except Exception as e:  # retry API/JSON errors
                last = str(e)
        return t, None, f"rejected: {last}"

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(run, tasks))

    by_file = {}
    for t, new, note in results:
        if new:
            by_file.setdefault(t["file"], []).append((t, new, note))
        else:
            print(f"  not changed: {t['lang']} {t['file'].stem} seg {t['seg']} — {note}")
    changed = 0
    for f, items in by_file.items():
        rec = json.loads(f.read_text(encoding="utf-8"))
        segs = segments(rec)
        for t, new, note in items:
            holder = segs[t["seg"]][1]
            if holder["text"] != t["current"]:
                continue
            holder["text"] = new
            rec.setdefault("review_fixes", []).append({
                "seg": t["seg"], "severity": "repair", "issue": "; ".join(k for k, _ in t["why"]) + (f" — {note}" if note else ""),
                "before": t["current"]})
            changed += 1
        f.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"repaired {changed} of {len(tasks)} segments")


if __name__ == "__main__":
    main()
