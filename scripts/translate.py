#!/usr/bin/env python3
"""Translate extracted course blocks into Hindi, Tibetan and Nepali with Gemini.

Input:  data/extracted/blocks.json  (from extract_course.py)
        data/glossary.json          (fixed term translations + keep-English list)
        .env                        (GEMINI_API_KEY=...)
Output: data/translations/<lang>/<block_id>.json   one file per block per language
        data/translations/review/<lang>/day_XX.md  English / translation side by side

Text blocks are translated paragraph by paragraph (headings, paragraphs, list
items keep their order). Problems translate the question and the options only;
feedback hints and explanations are not translated.

Finished blocks are skipped on reruns unless their English source changed, so
an interrupted run can simply be started again.

Usage:
  python3 -I scripts/translate.py                      # everything, all languages
  python3 -I scripts/translate.py --langs bo --days 1  # Tibetan, Day 1 only
  python3 -I scripts/translate.py --ids html1,html2    # specific blocks
  python3 -I scripts/translate.py --review-only        # just rebuild review files
"""
import argparse
import hashlib
import json
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from google import genai
from google.genai import types

ROOT = Path(__file__).resolve().parent.parent
LANGS = {
    "hi": {
        "name": "Hindi",
        "script": re.compile(r"[ऀ-ॿ]"),
        "style": "Simple, natural, conversational standard Hindi in Devanagari script — the way a "
                 "friendly teacher speaks, not heavily Sanskritised. Use the glossary's Buddhist "
                 "terms; otherwise prefer everyday words.",
    },
    "ne": {
        "name": "Nepali",
        "script": re.compile(r"[ऀ-ॿ]"),
        "style": "Simple, natural, conversational standard Nepali in Devanagari script — the way a "
                 "friendly teacher speaks. Use the glossary's Buddhist terms; otherwise prefer "
                 "everyday words.",
    },
    "bo": {
        "name": "Tibetan",
        "script": re.compile(r"[ༀ-࿿]"),
        "style": "SPOKEN Tibetan in Tibetan (Uchen) script — standard colloquial Tibetan as used in "
                 "the exile/Himalayan communities, the way a lama explains things to ordinary "
                 "listeners. It must be easy to understand when HEARD. Do NOT use literary or "
                 "classical phrasing; use short sentences, everyday vocabulary and spoken grammar "
                 "and particles (e.g. ཡིན་པ་རེད། / ཡོད་རེད། / གི་ཡོད་རེད།), not written forms "
                 "like ཡིན་ནོ། / སོ། / ལགས་སོ།. Prefer spoken words over written ones, e.g. "
                 "འདི་ཚོ་ / དེ་ཚོ་ not འདི་དག / དེ་དག, and avoid the literary topic marker ནི་.",
    },
}


def load_key():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip()
    sys.exit("GEMINI_API_KEY not found in .env")


def source_units(block):
    """The English strings to translate for a block, in order."""
    if block["type"] == "html":
        return [{"type": s["type"], "text": s["text"]} for s in block["segments"]]
    return ([{"type": "question", "text": block["question"]}]
            + [{"type": f"option {i + 1}", "text": c["text"]} for i, c in enumerate(block["choices"])])


def source_hash(units):
    return hashlib.sha256(json.dumps(units, ensure_ascii=False).encode()).hexdigest()[:16]


def system_prompt(lang, glossary):
    L = LANGS[lang]
    terms = "\n".join(
        f"- {t['en']} → {t[lang]}" + (f"   ({t['note']})" if t.get("note") else "")
        for t in glossary["terms"])
    keep = ", ".join(glossary["keep_english"])
    return f"""You translate an online course for Buddhist monks, nuns and lay practitioners \
("WeBuddhist Influencer Starter Course") from English into {L['name']}. The course teaches \
monastics how to share the Dharma on social media. The translation will later be read aloud \
as audio, so it must sound natural when spoken.

STYLE
{L['style']}

RULES
1. Translate the meaning faithfully and completely. Do not add, drop, summarise or explain content.
2. Technical, social-media and marketing terms, tool/platform names and the course's own framework \
names stay in ENGLISH, written in Latin script, inside the {L['name']} sentence. This list includes: \
{keep}. Treat similar technical terms the same way.
3. Names of people, channels, organisations, books and brands are never translated or transliterated; \
keep them exactly as in the English. Keep numbers, prices, percentages, URLs, emojis and symbols (→, •) as they are.
4. Use these fixed translations for Buddhist terms wherever the concept appears:
{terms}
5. You receive a numbered list of segments (headings, paragraphs, list items, or a quiz question and \
its options). Return exactly one translation per segment, with the same index, in the same order. \
Never merge or split segments. A heading stays short like a heading.
6. Quiz options must stay parallel and must not reveal which option is correct.
"""


def translate_block(client, model, lang, block, glossary, retries=4):
    units = source_units(block)
    payload = {
        "location": f"{block['chapter']} › {block['sequential']} › {block['vertical']}",
        "kind": "quiz question and options" if block["type"] == "problem" else "lesson text",
        "segments": [{"i": i, "type": u["type"], "text": u["text"]} for i, u in enumerate(units)],
    }
    schema = {
        "type": "object",
        "properties": {"translations": {"type": "array", "items": {
            "type": "object",
            "properties": {"i": {"type": "integer"}, "text": {"type": "string"}},
            "required": ["i", "text"]}}},
        "required": ["translations"],
    }
    config = types.GenerateContentConfig(
        system_instruction=system_prompt(lang, glossary),
        response_mime_type="application/json",
        response_json_schema=schema,
    )
    err = None
    for attempt in range(retries):
        try:
            resp = client.models.generate_content(
                model=model, contents=json.dumps(payload, ensure_ascii=False), config=config)
            got = {t["i"]: t["text"].strip() for t in json.loads(resp.text)["translations"]}
            out = [got.get(i, "") for i in range(len(units))]
            problems = check(units, out, lang)
            if problems:
                raise ValueError("; ".join(problems))
            return units, out, resp
        except Exception as e:  # API error, bad JSON or failed check: back off and retry
            err = e
            wait = (2 ** attempt) * 5 + random.random() * 3
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                wait = max(wait, 30)
            time.sleep(wait)
    raise RuntimeError(f"{block['id']} [{lang}] failed after {retries} attempts: {err}")


def check(units, out, lang):
    """Structural checks on a translation. Returns a list of problems (empty = ok)."""
    problems = []
    if len(out) != len(units):
        problems.append(f"expected {len(units)} segments, got {len(out)}")
    script = LANGS[lang]["script"]
    for i, (u, t) in enumerate(zip(units, out)):
        if not t:
            problems.append(f"segment {i} empty")
            continue
        # Segments that are only names/brands/numbers (capitalised words, e.g. image credits
        # or channel names) may legitimately stay in Latin script, but anything with several
        # ordinary lowercase English words must contain the target script.
        if not script.search(t) and len(re.findall(r"\b[a-z]{3,}", u["text"])) > 3:
            problems.append(f"segment {i} not in {LANGS[lang]['name']} script")
    return problems


def write_result(path, lang, block, units, out, resp, model, h):
    rec = {"id": block["id"], "type": block["type"], "lang": lang,
           "chapter_index": block["chapter_index"], "order": block["order"],
           "model": model, "model_version": getattr(resp, "model_version", None),
           "source_hash": h, "translated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if block["type"] == "html":
        rec["segments"] = [{"type": u["type"], "en": u["text"], "text": t} for u, t in zip(units, out)]
    else:
        rec["question"] = {"en": units[0]["text"], "text": out[0]}
        rec["choices"] = [{"en": u["text"], "text": t, "correct": c["correct"]}
                          for u, t, c in zip(units[1:], out[1:], block["choices"])]
    usage = getattr(resp, "usage_metadata", None)
    if usage:
        rec["usage"] = {"input": usage.prompt_token_count, "output": usage.candidates_token_count,
                        "thinking": getattr(usage, "thoughts_token_count", None)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")


def write_review(blocks, out_dir, langs):
    """Side-by-side English / translation markdown per language and day."""
    for lang in langs:
        days = {}
        for b in blocks:
            f = out_dir / lang / f"{b['id']}.json"
            if f.exists():
                days.setdefault(b["chapter_index"], []).append((b, json.loads(f.read_text(encoding="utf-8"))))
        for idx, items in days.items():
            lines = [f"# {items[0][0]['chapter']} — {LANGS[lang]['name']}", ""]
            for b, r in items:
                lines += [f"## {b['vertical']}  `{b['id']}`", ""]
                pairs = ([(s["en"], s["text"]) for s in r["segments"]] if b["type"] == "html"
                         else [(r["question"]["en"], r["question"]["text"])]
                         + [(f"Option {i + 1}: {c['en']}", f"Option {i + 1}: {c['text']}")
                            for i, c in enumerate(r["choices"])])
                for en, tr in pairs:
                    lines += [f"> {en}", "", tr, ""]
            d = out_dir / "review" / lang
            d.mkdir(parents=True, exist_ok=True)
            (d / f"day_{idx:02d}.md").write_text("\n".join(lines), encoding="utf-8")


def parse_days(s):
    days = set()
    for part in s.split(","):
        a, _, b = part.partition("-")
        days.update(range(int(a), int(b or a) + 1))
    return days


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--langs", default="hi,bo,ne")
    ap.add_argument("--model", default="gemini-pro-latest")
    ap.add_argument("--days", help="e.g. 1 or 1-3,7")
    ap.add_argument("--ids", help="comma-separated block ids")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--force", action="store_true", help="retranslate even if up to date")
    ap.add_argument("--review-only", action="store_true")
    ap.add_argument("--blocks", default=str(ROOT / "data/extracted/blocks.json"))
    ap.add_argument("--glossary", default=str(ROOT / "data/glossary.json"))
    ap.add_argument("--out", default=str(ROOT / "data/translations"))
    args = ap.parse_args()

    langs = args.langs.split(",")
    for l in langs:
        if l not in LANGS:
            sys.exit(f"unknown language {l}; choose from {', '.join(LANGS)}")
    out_dir = Path(args.out)
    all_blocks = json.loads(Path(args.blocks).read_text(encoding="utf-8"))
    blocks = [b for b in all_blocks if b["type"] == "problem" or b.get("kind") == "text"]
    if args.days:
        days = parse_days(args.days)
        blocks = [b for b in blocks if b["chapter_index"] in days]
    if args.ids:
        ids = set(args.ids.split(","))
        blocks = [b for b in blocks if b["id"] in ids]

    if not args.review_only:
        glossary = json.loads(Path(args.glossary).read_text(encoding="utf-8"))
        client = genai.Client(api_key=load_key())
        jobs = []
        for b in blocks:
            h = source_hash(source_units(b))
            for lang in langs:
                path = out_dir / lang / f"{b['id']}.json"
                if not args.force and path.exists() and \
                        json.loads(path.read_text(encoding="utf-8")).get("source_hash") == h:
                    continue
                jobs.append((b, lang, path, h))
        print(f"{len(blocks)} blocks x {len(langs)} languages: {len(jobs)} to translate "
              f"(model {args.model}, {args.workers} workers)")
        lock, done, failures, tokens = threading.Lock(), 0, [], {"input": 0, "output": 0, "thinking": 0}

        def run(job):
            b, lang, path, h = job
            units, out, resp = translate_block(client, args.model, lang, b, glossary)
            write_result(path, lang, b, units, out, resp, args.model, h)
            return resp

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(run, j): j for j in jobs}
            for f in as_completed(futures):
                b, lang, _, _ = futures[f]
                with lock:
                    done += 1
                    try:
                        u = f.result().usage_metadata
                        tokens["input"] += u.prompt_token_count or 0
                        tokens["output"] += u.candidates_token_count or 0
                        tokens["thinking"] += getattr(u, "thoughts_token_count", 0) or 0
                        status = "ok"
                    except Exception as e:
                        failures.append({"id": b["id"], "lang": lang, "error": str(e)})
                        status = "FAILED"
                    print(f"[{done}/{len(jobs)}] {lang} {b['id']} {status}", flush=True)
        print(f"tokens: {tokens}")
        if failures:
            (out_dir / "_failures.json").write_text(json.dumps(failures, ensure_ascii=False, indent=2))
            print(f"{len(failures)} failed — see {out_dir / '_failures.json'}; rerun to retry them")

    write_review([b for b in all_blocks if b["type"] == "problem" or b.get("kind") == "text"],
                 out_dir, langs)
    print(f"review files in {out_dir / 'review'}")


if __name__ == "__main__":
    main()
