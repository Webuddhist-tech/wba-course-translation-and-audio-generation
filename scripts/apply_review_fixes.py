#!/usr/bin/env python3
"""Apply accepted review fixes to the translation files.

Reads every data/translations/review_findings/*.json (lists of findings with
lang, id, en, current, severity, issue, fix) and replaces the matching segment
in data/translations/<lang>/<id>.json. A finding is applied only if the
segment's current text still equals "current", so stale findings are skipped.
Findings listed in review_findings/_rejected.json (by lang + id + en) are skipped.
Each applied fix is recorded in the translation file under "review_fixes".

Usage:
  python3 -I scripts/apply_review_fixes.py [--dry-run]
"""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TR = ROOT / "data/translations"


def segments(rec):
    """(getter, setter) pairs for every translatable segment of a record."""
    if rec["type"] == "html":
        return [(s["en"], s, "text") for s in rec["segments"]]
    return [(rec["question"]["en"], rec["question"], "text")] + [(c["en"], c, "text") for c in rec["choices"]]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    d = TR / "review_findings"
    rejected_file = d / "_rejected.json"
    rejected = {(r["lang"], r["id"], r["en"]) for r in json.loads(rejected_file.read_text())} \
        if rejected_file.exists() else set()
    applied = skipped = stale = 0
    for f in sorted(d.glob("*.json")):
        if f.name.startswith("_"):
            continue
        for x in json.loads(f.read_text(encoding="utf-8")):
            key = (x["lang"], x["id"], x["en"])
            if key in rejected:
                skipped += 1
                continue
            path = TR / x["lang"] / f"{x['id']}.json"
            rec = json.loads(path.read_text(encoding="utf-8"))
            for i, (en, holder, field) in enumerate(segments(rec)):
                if en == x["en"] and holder[field] == x["current"]:
                    holder[field] = x["fix"]
                    rec.setdefault("review_fixes", []).append(
                        {"seg": i, "severity": x["severity"], "issue": x["issue"], "before": x["current"]})
                    applied += 1
                    if not args.dry_run:
                        path.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
                    break
            else:
                stale += 1
                print(f"not applied (segment not found or already changed): {x['lang']} {x['id']} {x['en'][:60]!r}")
    print(f"applied {applied}, rejected {skipped}, not found/stale {stale}" + (" (dry run)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
