#!/usr/bin/env python3
"""Generate the lesson and quiz audio with Gemini text-to-speech.

For every lesson text block and every quiz question, and for each language, one mp3 file:

  lessons   audio_d<day>_t<nn>_<lang>.mp3   (nn = text block number within the day)
  quizzes   audio_d<day>_q<nn>_<lang>.mp3   (nn = question number within the day)

Spoken text
  lessons   the paragraphs in order (image credits, emojis, links and fill-in blanks are skipped)
  quizzes   the question, then "Option one ... Option two ..." and each option; never the answer,
            the feedback or the explanation

Long pages are split at paragraph boundaries, voiced in pieces and joined. Gemini returns 24 kHz mono
WAV; the result is converted to a 64 kbps mono mp3. Finished files are recorded in
<out>/manifest.json, so an interrupted run can simply be started again.

Needs the newer SDK:  .venv/bin/python scripts/generate_audio.py ...   (google-genai >= 2.25)

Usage:
  .venv/bin/python scripts/generate_audio.py --dry-run                 # names, minutes, cost estimate
  .venv/bin/python scripts/generate_audio.py --days 1                  # Day 1, en/hi/ne
  .venv/bin/python scripts/generate_audio.py --langs hi --days 1-3 --workers 4
"""
import argparse
import base64
import hashlib
import io
import json
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "model": "gemini-3.8-flash-tts",
    "style": "calm, clear and warm, at a steady pace",
    "voices": {"en": "Kore", "hi": "Kore", "ne": "Kore", "bo": "lhasa_female"},
    "max_chars": {"en": 1400, "hi": 900, "ne": 900, "bo": 1100},
    "bo_model": "monlamai-tts",  # Tibetan comes from the Monlam API, not Gemini
    "bo_gap_seconds": 0.6,       # silence between Tibetan paragraphs / options
    "mp3_kbps": 64,
    "loudnorm": "I=-19:TP=-2:LRA=11",  # same loudness for every file ("" to switch off)
    "chunk_gap_seconds": 0.4,
}
OPTION = {
    "en": ("Option", ["one", "two", "three", "four", "five", "six"]),
    "hi": ("विकल्प", ["एक", "दो", "तीन", "चार", "पाँच", "छह"]),
    "ne": ("विकल्प", ["एक", "दुई", "तीन", "चार", "पाँच", "छ"]),
    "bo": ("གདམ་ག་", ["དང་པོ", "གཉིས་པ", "གསུམ་པ", "བཞི་པ", "ལྔ་པ", "དྲུག་པ"]),  # draft wording
}
MONLAM_UNITS_PER_CHAR = 0.000032  # observed cost per character (units not stated by Monlam)
# estimate only: (input, output) USD per 1M tokens, standard tier until 2026-12-31
PRICES = {"gemini-3.8-flash-tts": (0.50, 9.00), "gemini-3.8-flash-lite-tts": (0.50, 6.00)}
AUDIO_TOKENS_PER_SECOND = 25

PAUSE = " <short pause> "
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍✨ ]")
IMAGE_CREDIT = re.compile(r"^\s*Image\s*\d+\s*Credit", re.I)


class OutOfCredit(Exception):
    pass


def load_config(path=None):
    cfg = json.loads(json.dumps(DEFAULTS))
    f = Path(path) if path else ROOT / "data/audio_config.json"
    if f.exists():
        for k, v in json.loads(f.read_text(encoding="utf-8")).items():
            cfg[k] = {**cfg[k], **v} if isinstance(cfg.get(k), dict) else v
    return cfg


def env_value(name):
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    sys.exit(f"{name} not found in .env")


def load_key():
    return env_value("GEMINI_API_KEY")


def model_for(cfg, lang):
    return cfg["bo_model"] if lang == "bo" else cfg["model"]


# ---------------------------------------------------------------- what to say
def clean(text):
    """Make text comfortable to hear: no links, emojis, arrows or blanks."""
    t = re.sub(r"https?://\S+|www\.\S+", "", text)
    t = EMOJI.sub(" ", t)
    t = t.replace("→", ", ").replace("•", " ").replace("<", " ").replace(">", " ")
    t = re.sub(r"_{2,}", " ", t)
    t = re.sub(r"\s+([,.;:!?।])", r"\1", t)
    t = re.sub(r"\s+", " ", t).strip(" ,;")
    return t if re.search(r"\w", t) else ""


def lesson_pieces(block, tr, lang):
    """-> list of spoken paragraphs for a lesson block (tr = translation record, None for English)."""
    pieces = []
    for i, seg in enumerate(block["segments"]):
        if IMAGE_CREDIT.match(seg["text"]):
            continue
        text = seg["text"] if tr is None else tr["segments"][i]["text"]
        text = clean(text)
        if text:
            pieces.append(text)
    return pieces


def quiz_pieces(block, tr, lang):
    if tr is None:
        question, options = block["question"], [c["text"] for c in block["choices"]]
    else:
        question, options = tr["question"]["text"], [c["text"] for c in tr["choices"]]
    label, words = OPTION[lang]
    stop = "." if lang == "en" else "།" if lang == "bo" else "।"
    sep = "" if lang == "bo" else " "
    pieces = [clean(question)]
    for n, o in enumerate(options):
        pieces.append(f"{label}{sep}{words[n]}{stop} {clean(o)}")
    return [p for p in pieces if p]


def chunks(pieces, max_chars):
    """Group paragraphs into requests of at most max_chars (a single long paragraph stays whole)."""
    out, cur = [], []
    for p in pieces:
        if cur and sum(len(x) for x in cur) + len(p) > max_chars:
            out.append(cur)
            cur = []
        cur.append(p)
    if cur:
        out.append(cur)
    return [PAUSE.join(c) for c in out]


# --- Tibetan: English words are replaced by their Tibetan spelling (data/tibetan_terms.json) -----------
_ATOM = r"[A-Za-z0-9$%&+#@./'’\-–]*[A-Za-z0-9$%][A-Za-z0-9$%&+#@./'’\-–]*"
_SPAN = re.compile(rf"{_ATOM}(?:\s+{_ATOM})*")
_WORD = re.compile(r"[A-Za-z][A-Za-z'’]*")
_TERMS = None


def tibetan_text(text):
    """Spoken Tibetan: thousands commas removed and every English word/phrase spelled in Tibetan script.
    Raises if any English letter would be left, so nothing English reaches the Tibetan voice."""
    global _TERMS
    if _TERMS is None:
        _TERMS = json.loads((ROOT / "data/tibetan_terms.json").read_text(encoding="utf-8"))
    missing = []

    def span(m):
        raw = m.group(0)
        core = raw.strip(" .,'’-–/")
        if not core:
            return raw
        lead = raw[:raw.index(core)]
        trail = raw[len(lead) + len(core):]
        if re.search(r"[\d$%&+#@/]", core):
            e = _TERMS["phrases"].get(core)
            if not e:
                missing.append(core)
                return raw
            return lead + e["bo"] + trail

        def word(w):
            e = _TERMS["words"].get(w.group(0).lower())
            if not e:
                missing.append(w.group(0))
                return w.group(0)
            return e["bo"]

        return lead + _WORD.sub(word, core) + trail

    out = _SPAN.sub(span, re.sub(r"(?<=\d),(?=\d{3}\b)", "", text))
    out = re.sub(r"\s+", " ", out).strip()
    if missing or re.search(r"[A-Za-z]", out):
        raise ValueError(f"no Tibetan spelling for: {sorted(set(missing)) or re.findall(r'[A-Za-z]+', out)[:5]}"
                         " - run scripts/tibetan_terms.py")
    return out


def split_long(text, max_chars):
    """Keep a Tibetan paragraph in requests of at most max_chars, splitting only after a sentence end (།)."""
    if len(text) <= max_chars:
        return [text]
    out, cur = [], ""
    for sent in re.split(r"(?<=།)\s+", text):
        if cur and len(cur) + len(sent) + 1 > max_chars:
            out.append(cur)
            cur = ""
        cur = f"{cur} {sent}".strip()
    return out + ([cur] if cur else [])


def build_items(blocks, langs, days=None, kinds="tq", ids=None):
    """Every audio file to make, in course order, with its final file name."""
    counters, items = {}, []
    for b in blocks:
        if b["type"] == "html" and b.get("kind") == "text":
            kind = "t"
        elif b["type"] == "problem":
            kind = "q"
        else:
            continue
        d = b["chapter_index"]
        counters[(d, kind)] = counters.get((d, kind), 0) + 1
        n = counters[(d, kind)]
        if (days and d not in days) or kind not in kinds or (ids and b["id"] not in ids):
            continue
        for lang in langs:
            items.append({"name": f"audio_d{d:02d}_{kind}{n:02d}_{lang}.mp3", "block": b, "kind": kind,
                          "day": d, "number": n, "lang": lang})
    return items


def spoken_chunks(item, cfg):
    lang, b = item["lang"], item["block"]
    tr = None if lang == "en" else json.loads(
        (ROOT / "data/translations" / lang / f"{b['id']}.json").read_text(encoding="utf-8"))
    pieces = (lesson_pieces if item["kind"] == "t" else quiz_pieces)(b, tr, lang)
    if lang == "bo":  # one request per paragraph / option; the silence between them is added when joining
        out = []
        for p in pieces:
            p = tibetan_text(p)
            if p:
                out += split_long(p, cfg["max_chars"]["bo"])
        return out
    return chunks(pieces, cfg["max_chars"][lang])


# ------------------------------------------------------------ talking to Gemini
def synthesize(client, cfg, voice, text, retries=5):
    last = None
    for attempt in range(retries):
        try:
            r = client.interactions.create(
                model=cfg["model"],
                input=[{"type": "user_input", "content": [{
                    "type": "text", "text": text,
                    "annotations": [{"type": "speech_metadata", "style": cfg["style"]}]}]}],
                response_format={"type": "audio"},
                generation_config={"speech_config": [{"voice": voice}]})
            wav = base64.b64decode(r.output_audio.data)
            u = r.usage
            tokens_in = sum(m.tokens for m in (u.input_tokens_by_modality or []))
            tokens_out = sum(m.tokens for m in (u.output_tokens_by_modality or []))
            return wav, tokens_in, tokens_out
        except Exception as e:
            last = e
            msg = str(e)
            if "402" in msg or "depleted" in msg.lower() or "billing" in msg.lower():
                raise OutOfCredit(msg)
            time.sleep(min(60, 4 * 2 ** attempt))
    raise RuntimeError(f"gave up after {retries} attempts: {last}")


def fix_stream_wav(raw):
    """Monlam's streamed WAV has no real length in its header; rebuild it as an ordinary WAV."""
    i = raw.find(b"data")
    channels, rate = struct.unpack("<HI", raw[22:28])
    (bits,) = struct.unpack("<H", raw[34:36])
    frame = channels * bits // 8
    pcm = raw[i + 8:]
    pcm = pcm[:len(pcm) - len(pcm) % frame]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(bits // 8)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def synthesize_bo(http, voice, text, retries=5):
    """One Tibetan clip from Monlam's streaming endpoint (WAV, 24 kHz mono). -> (wav bytes, characters sent)"""
    url, key = env_value("MONLAM_API_URL") + "/api/v1/text-to-speech/stream", env_value("MONLAM_API_KEY")
    last = None
    for attempt in range(retries):
        try:
            r = http.post(url, json={"text": text, "voice_name": voice}, headers={"X-API-Key": key}, timeout=300)
            if r.status_code in (401, 402, 403):
                raise OutOfCredit(f"Monlam HTTP {r.status_code}: {r.text[:120]}")
            if r.status_code == 200 and r.content[:4] == b"RIFF":
                return fix_stream_wav(r.content), len(text)
            last = f"HTTP {r.status_code}: {r.text[:100]}"
        except OutOfCredit:
            raise
        except Exception as e:
            last = e
        time.sleep(min(60, 5 * 2 ** attempt))
    raise RuntimeError(f"Monlam gave up after {retries} attempts: {last}")


def join_wavs(wavs, gap_seconds):
    """Concatenate same-format WAV byte strings with a short silence between them."""
    frames, params = [], None
    for raw in wavs:
        with wave.open(io.BytesIO(raw)) as w:
            if params is None:
                params = w.getparams()
            frames.append(w.readframes(w.getnframes()))
    silence = b"\x00" * int(params.framerate * gap_seconds) * params.sampwidth * params.nchannels
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setparams(params)
        out.writeframes(silence.join(frames))
    return buf.getvalue(), sum(len(f) for f in frames) / (params.framerate * params.sampwidth * params.nchannels)


def to_mp3(wav_bytes, out_path, kbps, loudnorm=""):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
        tmp.write(wav_bytes)
        tmp.flush()
        filt = ["-af", f"loudnorm={loudnorm}"] if loudnorm else []
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", tmp.name, *filt, "-ar", "24000", "-ac", "1",
                        "-b:a", f"{kbps}k", str(out_path)], check=True)


def make_file(client, cfg, item, out_dir, voice):
    parts = spoken_chunks(item, cfg)
    wavs, t_in, t_out = [], 0, 0
    bo = item["lang"] == "bo"
    for p in parts:
        if bo:
            wav, i = synthesize_bo(client, voice, p)
            o = 0
        else:
            wav, i, o = synthesize(client, cfg, voice, p)
        wavs.append(wav)
        t_in, t_out = t_in + i, t_out + o
    wav, seconds = join_wavs(wavs, cfg["bo_gap_seconds"] if bo else cfg["chunk_gap_seconds"])
    path = out_dir / item["lang"] / item["name"]
    to_mp3(wav, path, cfg["mp3_kbps"], cfg["loudnorm"])
    return {"seconds": round(seconds, 1), "bytes": path.stat().st_size, "chunks": len(parts),
            "tokens_in": t_in, "tokens_out": t_out}


def fingerprint(item, cfg, voice):
    return hashlib.sha256(json.dumps([spoken_chunks(item, cfg), model_for(cfg, item["lang"]), voice, cfg["style"]],
                                     ensure_ascii=False).encode()).hexdigest()[:16]


def parse_days(s):
    days = set()
    for part in s.split(","):
        a, _, b = part.partition("-")
        days.update(range(int(a), int(b or a) + 1))
    return days


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--langs", default="en,hi,ne")
    ap.add_argument("--days")
    ap.add_argument("--kinds", default="tq", help="t = lesson text, q = quizzes")
    ap.add_argument("--ids", help="comma-separated block ids")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default=str(ROOT / "data/audio"))
    ap.add_argument("--config")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    langs = args.langs.split(",")
    for l in langs:
        if l not in OPTION:
            sys.exit(f"language {l!r} not supported here (use en, hi, ne, bo)")
    blocks = json.loads((ROOT / "data/extracted/blocks.json").read_text(encoding="utf-8"))
    items = build_items(blocks, langs, parse_days(args.days) if args.days else None, args.kinds,
                        set(args.ids.split(",")) if args.ids else None)

    out_dir = Path(args.out)
    manifest_path = out_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}

    todo = []
    for it in items:
        voice = cfg["voices"][it["lang"]]
        fp = fingerprint(it, cfg, voice)
        m = manifest.get(it["name"])
        if not args.force and m and m.get("fingerprint") == fp and (out_dir / it["lang"] / it["name"]).exists():
            continue
        todo.append((it, voice, fp))

    chars = {l: sum(len(" ".join(spoken_chunks(it, cfg))) for it, _, _ in todo if it["lang"] == l) for l in langs}
    minutes = {l: chars[l] / 15 / 60 for l in langs}  # ~15 characters per second of speech
    price_in, price_out = PRICES.get(cfg["model"], (0.5, 9.0))
    gem_minutes = sum(m for l, m in minutes.items() if l != "bo")
    est = gem_minutes * 60 * AUDIO_TOKENS_PER_SECOND / 1e6 * price_out
    line = (f"{len(items)} files in scope, {len(todo)} to make | ~{sum(minutes.values()) / 60:.1f} h of audio | "
            f"Gemini ({cfg['model']}) ~${est:.2f}")
    if "bo" in chars:
        line += f" | Monlam {chars['bo']} characters ~{chars['bo'] * MONLAM_UNITS_PER_CHAR:.2f} units"
    print(line + " (estimates)")
    if args.dry_run or not todo:
        for it, voice, _ in todo[:12]:
            print("  ", it["name"], "|", it["block"]["id"][:12], "|", voice)
        return

    clients = {}
    if any(it["lang"] != "bo" for it, _, _ in todo):
        from google import genai
        clients["gemini"] = genai.Client(api_key=load_key())
    if any(it["lang"] == "bo" for it, _, _ in todo):
        import httpx
        clients["monlam"] = httpx.Client()
    lock, done, failed, spent = threading.Lock(), 0, [], [0, 0, 0]

    def run(job):
        it, voice, fp = job
        info = make_file(clients["monlam" if it["lang"] == "bo" else "gemini"], cfg, it, out_dir, voice)
        return {**info, "fingerprint": fp, "voice": voice, "model": model_for(cfg, it["lang"]), "block": it["block"]["id"],
                "lang": it["lang"], "day": it["day"], "kind": it["kind"], "number": it["number"]}

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run, j): j for j in todo}
        try:
            for f in as_completed(futures):
                it = futures[f][0]
                with lock:
                    done += 1
                    try:
                        rec = f.result()
                        manifest[it["name"]] = rec
                        if it["lang"] == "bo":
                            spent[2] += rec["tokens_in"]  # characters sent to Monlam
                        else:
                            spent[0] += rec["tokens_in"]
                            spent[1] += rec["tokens_out"]
                        manifest_path.parent.mkdir(parents=True, exist_ok=True)
                        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
                        print(f"[{done}/{len(todo)}] {it['name']} {rec['seconds']}s", flush=True)
                    except OutOfCredit as e:
                        print(f"STOPPED: out of credit or access refused ({str(e)[:160]}). Fix the account, then run again; finished files are kept.")
                        for g in futures:
                            g.cancel()
                        raise SystemExit(2)
                    except Exception as e:
                        failed.append({"name": it["name"], "error": str(e)[:300]})
                        print(f"[{done}/{len(todo)}] {it['name']} FAILED: {str(e)[:150]}", flush=True)
        finally:
            cost = spent[0] / 1e6 * price_in + spent[1] / 1e6 * price_out
            print(f"Gemini: tokens in {spent[0]}, audio out {spent[1]} -> about ${cost:.2f}"
                  + (f" | Monlam: {spent[2]} characters -> about {spent[2] * MONLAM_UNITS_PER_CHAR:.2f} units" if spent[2] else ""))
    if failed:
        (out_dir / "_failed.json").write_text(json.dumps(failed, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{len(failed)} failed - see {out_dir / '_failed.json'}; run again to retry them")


if __name__ == "__main__":
    main()
