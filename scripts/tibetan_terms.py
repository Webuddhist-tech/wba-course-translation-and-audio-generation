#!/usr/bin/env python3
"""Build the list of English terms in the Tibetan audio text and their spelling in Tibetan script.

The Tibetan voice reads English letters badly, so for the audio only, English words are replaced by a
phonetic Tibetan spelling (the course text itself is not changed). This script:

  1. scans the spoken Tibetan text (same cleaning as generate_audio.py) for English words and for
     phrases containing numbers/symbols (e.g. "$3K", "30%"),
  2. asks Gemini for a first-draft Tibetan spelling of each unique word / phrase,
  3. writes data/tibetan_terms.json (used by the audio script) and data/tibetan_terms_review.csv
     (sorted by how often a term occurs, for a Tibetan speaker to correct).

Corrections go into data/tibetan_terms.json (or the csv, re-imported with --import-csv); entries marked
"reviewed": true are never overwritten by a later run.

  .venv/bin/python scripts/tibetan_terms.py            # collect + draft the missing spellings
  .venv/bin/python scripts/tibetan_terms.py --import-csv data/tibetan_terms_review.csv
"""
import argparse
import csv
import json
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_audio as g  # noqa: E402

ROOT = g.ROOT
OUT = ROOT / "data/tibetan_terms.json"
TIBETAN = re.compile(r"[ༀ-࿿]")
WORD = re.compile(r"[A-Za-z][A-Za-z'’]*")
ATOM = r"[A-Za-z0-9$%&+#@./'’\-–]*[A-Za-z0-9$%][A-Za-z0-9$%&+#@./'’\-–]*"
SPAN = re.compile(rf"{ATOM}(?:\s+{ATOM})*")


def join_thousands(text):
    """30,000 -> 30000 so a number is one item (also used by the Tibetan audio text)."""
    return re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)


def spoken_texts():
    blocks = json.loads((ROOT / "data/extracted/blocks.json").read_text(encoding="utf-8"))
    for b in blocks:
        tr = ROOT / "data/translations/bo" / f"{b['id']}.json"
        if b["type"] == "html" and b.get("kind") == "text":
            rec = json.loads(tr.read_text(encoding="utf-8"))
            for i, s in enumerate(b["segments"]):
                if not g.IMAGE_CREDIT.match(s["text"]):
                    yield join_thousands(g.clean(rec["segments"][i]["text"]))
        elif b["type"] == "problem":
            rec = json.loads(tr.read_text(encoding="utf-8"))
            yield join_thousands(g.clean(rec["question"]["text"]))
            for c in rec["choices"]:
                yield join_thousands(g.clean(c["text"]))


def collect():
    """-> (word counts keyed by lowercase word, phrase counts for spans containing digits/symbols)"""
    words, phrases = Counter(), Counter()
    for text in spoken_texts():
        for span in SPAN.findall(text):
            span = span.strip(" .,'’-–/")
            if not span:
                continue
            if re.search(r"[\d$%&+#@/]", span):
                phrases[span] += 1
            else:
                for w in WORD.findall(span):
                    words[w.lower()] += 1
    return words, phrases


PROMPT = """You help prepare a Tibetan text-to-speech script. English words appear inside spoken Tibetan text, and the \
voice cannot read English letters. For each item below write how a Tibetan speaker would SAY it, spelled in Tibetan \
script (Uchen), so the Tibetan voice pronounces it like a Tibetan person saying the English word.

Rules:
- Phonetic spelling with ordinary Tibetan letters; separate syllables with the tsheg (་). Keep it short and natural.
- Brand names and people's names are spelled phonetically too (YouTube, Instagram, Canva, Milan, Jaimin ...).
- Acronyms are spelled letter by letter the way people say them (AI -> ཨེ་ཨའི, ROI, CMO ...).
- Items with numbers or symbols: write the whole thing as it would be SAID aloud in Tibetan: numbers in Tibetan \
numerals or words, and units and symbols spoken (30% -> percent, $3K -> three thousand dollars, 21+ -> more than \
twenty one, 8.94M -> eight point nine four million). Use the natural Tibetan word for the number where one exists.
- The output must contain ONLY Tibetan script (plus Tibetan numerals, spaces and the shad །). No Latin letters.

Return JSON: a list of {"en": <the item exactly as given>, "bo": <Tibetan>}, one entry per item, same order."""


def draft(client, model, items, retries=4):
    from google.genai import types
    schema = {"type": "array", "items": {"type": "object", "properties": {"en": {"type": "string"},
              "bo": {"type": "string"}}, "required": ["en", "bo"]}}
    cfg = types.GenerateContentConfig(system_instruction=PROMPT, response_mime_type="application/json",
                                      response_json_schema=schema)
    for attempt in range(retries):
        try:
            r = client.models.generate_content(model=model, contents=json.dumps(items, ensure_ascii=False), config=cfg)
            got = {x["en"]: x["bo"].strip() for x in json.loads(r.text)}
            bad = [i for i in items if i not in got or not TIBETAN.search(got[i]) or re.search(r"[A-Za-z]", got[i])]
            if len(bad) > max(1, len(items) // 20):
                raise ValueError(f"{len(bad)} unusable entries, e.g. {bad[:3]}")
            return {i: got[i] for i in items if i not in bad}
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(5 * 2 ** attempt)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--model", default="gemini-pro-latest")
    ap.add_argument("--batch", type=int, default=80)
    ap.add_argument("--import-csv")
    args = ap.parse_args()

    data = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {"words": {}, "phrases": {}}
    if args.import_csv:
        n = 0
        for row in csv.DictReader(open(args.import_csv, encoding="utf-8-sig")):
            kind = "phrases" if row["type"] == "phrase" else "words"
            key = row["english"] if kind == "phrases" else row["english"].lower()
            if row["tibetan"].strip() and data[kind].get(key, {}).get("bo") != row["tibetan"].strip():
                data[kind][key] = {"bo": row["tibetan"].strip(), "reviewed": True}
                n += 1
        OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"imported {n} corrected spellings")
        return

    words, phrases = collect()
    todo = [("words", w) for w in words if w not in data["words"]] + [("phrases", p) for p in phrases if p not in data["phrases"]]
    print(f"{len(words)} unique words, {len(phrases)} unique number/symbol phrases; {len(todo)} still need a spelling")
    if todo:
        from google import genai
        client = genai.Client(api_key=g.load_key())
        batches = [todo[i:i + args.batch] for i in range(0, len(todo), args.batch)]

        def run(batch):
            return batch, draft(client, args.model, [t for _, t in batch])

        with ThreadPoolExecutor(max_workers=4) as pool:
            for batch, got in pool.map(run, batches):
                for kind, term in batch:
                    if term in got:
                        data[kind][term] = {"bo": got[term], "reviewed": False}
                print(f"  drafted {len(got)}/{len(batch)}", flush=True)
        OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    missing = [t for k, t in todo if t not in data[k]]
    rows = [("word", w, c) for w, c in words.items()] + [("phrase", p, c) for p, c in phrases.items()]
    with open(ROOT / "data/tibetan_terms_review.csv", "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.writer(f)
        wr.writerow(["type", "english", "tibetan", "times used", "reviewed"])
        for kind, term, c in sorted(rows, key=lambda r: -r[2]):
            e = data["phrases" if kind == "phrase" else "words"].get(term, {})
            wr.writerow([kind, term, e.get("bo", ""), c, "yes" if e.get("reviewed") else ""])
    print(f"wrote {OUT.name} and tibetan_terms_review.csv | still missing: {len(missing)} {missing[:10]}")


if __name__ == "__main__":
    main()
