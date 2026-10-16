#!/usr/bin/env python3
"""Automatic quality checks on the Gemini translations.

Flags, per language:
  missing      block has no translation, or the English changed since it was made
  length       segment much shorter/longer than usual for that language (dropped or added text)
  glossary     a glossary term appears in the English but its fixed translation is not found
  english      Latin-script words left in the translation that are not in the source as
               names and not on the keep-English list
  numbers      a number in the English is missing from the translation
  literary     (Tibetan) written-style forms that should be spoken style

Writes data/translations/qa_report.json and qa_report.md. Flags are things to
look at, not proof of an error (e.g. a glossary term may be correctly inflected).

Usage:
  python3 -I scripts/check_translations.py [--langs hi,bo,ne]
"""
import argparse
import json
import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from translate import source_hash, source_units  # noqa: E402

TIBETAN_LITERARY = ["འདི་དག", "དེ་དག", "ཡིན་ནོ།", "ཡོད་དོ།", "ལགས་སོ།", "སོ།", "ནི་", "གོ།", "ངོ་།", "འོ།"]
# Devanagari (०-९) and Tibetan (༠-༩) digits count as the same number as 0-9.
NATIVE_DIGITS = str.maketrans("०१२३४५६७८९༠༡༢༣༤༥༦༧༨༩", "01234567890123456789")


def variants(s):
    """'a / b (note)' -> ['a', 'b']"""
    s = re.sub(r"\(.*?\)", "", s)
    return [v.strip() for v in s.split("/") if v.strip()]


def target_found(target, text, lang):
    for v in variants(target):
        v = v.rstrip("།་ ")
        if lang == "bo":
            if v in text:
                return True
        else:
            # allow inflection: drop a trailing vowel sign/letter on long words
            if v in text or (len(v) > 3 and v[:-1] in text):
                return True
    return False


def pairs(rec):
    if rec["type"] == "html":
        return [(s["en"], s["text"]) for s in rec["segments"]]
    return [(rec["question"]["en"], rec["question"]["text"])] + [(c["en"], c["text"]) for c in rec["choices"]]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--langs", default="hi,bo,ne")
    ap.add_argument("--out", default=str(ROOT / "data/translations"))
    args = ap.parse_args()
    out = Path(args.out)
    blocks = [b for b in json.loads((ROOT / "data/extracted/blocks.json").read_text(encoding="utf-8"))
              if b["type"] == "problem" or b.get("kind") == "text"]
    glossary = json.loads((ROOT / "data/glossary.json").read_text(encoding="utf-8"))
    keep = {w.lower() for k in glossary["keep_english"] for w in re.findall(r"[A-Za-z]+", k)}

    report = {}
    for lang in args.langs.split(","):
        flags = {k: [] for k in ("missing", "length", "glossary", "english", "numbers", "literary")}
        recs = []
        for b in blocks:
            f = out / lang / f"{b['id']}.json"
            if not f.exists():
                flags["missing"].append({"id": b["id"], "why": "no translation"})
                continue
            rec = json.loads(f.read_text(encoding="utf-8"))
            if rec["source_hash"] != source_hash(source_units(b)):
                flags["missing"].append({"id": b["id"], "why": "English changed since translation"})
            recs.append((b, rec))

        ratios = [len(t) / len(e) for _, r in recs for e, t in pairs(r) if len(e) >= 40]
        med = statistics.median(ratios) if ratios else 1
        for b, rec in recs:
            for i, (en, tr) in enumerate(pairs(rec)):
                where = {"id": b["id"], "day": b["chapter_index"], "seg": i, "en": en, "tr": tr}
                if len(en) >= 40:
                    r = len(tr) / len(en) / med
                    if r < 0.5 or r > 2.0:
                        flags["length"].append({**where, "ratio": round(r, 2)})
                for t in glossary["terms"]:
                    for v in variants(t["en"]):
                        if re.search(r"\b" + re.escape(v) + r"s?\b", en, re.I) and not target_found(t[lang], tr, lang):
                            flags["glossary"].append({**where, "term": v, "expected": t[lang]})
                            break
                src_words = {w.lower() for w in re.findall(r"[A-Za-z]+", en)}
                src_names = {w.lower() for w in re.findall(r"\b[A-Z][A-Za-z]+", en)}
                left = [w for w in re.findall(r"[A-Za-z]{3,}", tr)
                        if w.lower() not in keep and w.lower().rstrip("s") not in keep
                        and w.lower() not in src_names
                        and not (w.lower() in src_words and w[0].isupper())]
                if left:
                    flags["english"].append({**where, "words": sorted(set(left))})
                nums = set(re.findall(r"\d+(?:[.,]\d+)?", en))
                missing = [n for n in nums if n not in tr.translate(NATIVE_DIGITS)]
                if missing:
                    flags["numbers"].append({**where, "missing": missing})
                if lang == "bo":
                    hits = [m for m in TIBETAN_LITERARY if m in tr]
                    if hits:
                        flags["literary"].append({**where, "forms": hits})
        report[lang] = {"blocks": len(recs), "segments": sum(len(pairs(r)) for _, r in recs),
                        "median_length_ratio": round(med, 2), "flags": flags}

    (out / "qa_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Translation QA report", ""]
    for lang, r in report.items():
        lines += [f"## {lang}: {r['blocks']} blocks, {r['segments']} segments", ""]
        lines += [f"- **{k}**: {len(v)}" for k, v in r["flags"].items()]
        lines.append("")
        for k, v in r["flags"].items():
            if not v:
                continue
            lines += [f"### {lang} — {k}", ""]
            for x in v[:200]:
                extra = {kk: vv for kk, vv in x.items() if kk not in ("en", "tr", "id", "day", "seg")}
                lines.append(f"- day {x.get('day', '?')} `{x['id']}` seg {x.get('seg', '-')} {extra}")
                if "en" in x:
                    lines += [f"  - EN: {x['en'][:200]}", f"  - {lang.upper()}: {x['tr'][:200]}"]
            lines.append("")
    (out / "qa_report.md").write_text("\n".join(lines), encoding="utf-8")
    for lang, r in report.items():
        print(lang, {k: len(v) for k, v in r["flags"].items()})
    print(f"wrote {out / 'qa_report.md'}")


if __name__ == "__main__":
    main()
