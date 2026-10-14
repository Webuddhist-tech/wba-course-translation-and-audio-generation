#!/usr/bin/env python3
"""Extract text and quiz content from an Open edX course export (.tar.gz).

Reads the archive in memory (nothing is unpacked to disk) and writes:

  <out>/blocks.json      flat list of every translatable block, in course order
  <out>/structure.json   chapter > sequential > vertical > component tree
  <out>/by_day/*.md      human-readable review copy, one file per day

Usage:
  python3 -I scripts/extract_course.py COURSE.tar.gz [--out data/extracted]
"""
import argparse
import html
import json
import re
import tarfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

BLOCK_TAGS = {"p", "div", "br", "tr", "table", "section", "blockquote", "ul", "ol"}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


class TextExtractor(HTMLParser):
    """HTML -> list of segments: {"type": "heading|paragraph|list_item", "text": str}.

    Drops <style>/<script> and all attributes (this removes the large base64
    Canva clipboard blobs). Collects links and image alt/src for reference.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.segments = []
        self.links = []
        self.images = []
        self._buf = []
        self._kind = "paragraph"
        self._skip = 0
        self._href = None
        self._link_buf = []

    def _flush(self):
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        if text:
            self.segments.append({"type": self._kind, "text": text})
        self._buf = []
        self._kind = "paragraph"

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("style", "script"):
            self._skip += 1
        elif tag in HEADING_TAGS:
            self._flush()
            self._kind = "heading"
        elif tag == "li":
            self._flush()
            self._kind = "list_item"
        elif tag in BLOCK_TAGS:
            self._flush()
        elif tag == "a":
            self._href, self._link_buf = a.get("href"), []
        elif tag == "img":
            src = a.get("src") or ""
            self.images.append({"alt": a.get("alt", ""),
                                "src": "" if src.startswith("data:") else src})

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self._skip = max(0, self._skip - 1)
        elif tag in HEADING_TAGS or tag in BLOCK_TAGS or tag == "li":
            self._flush()
        elif tag == "a" and self._href:
            self.links.append({"text": re.sub(r"\s+", " ", "".join(self._link_buf)).strip(),
                               "href": self._href})
            self._href = None

    def handle_data(self, data):
        if self._skip:
            return
        self._buf.append(data)
        if self._href is not None:
            self._link_buf.append(data)

    def result(self):
        self._flush()
        return self.segments, self.links, self.images


def extract(markup):
    p = TextExtractor()
    p.feed(markup)
    p.close()
    return p.result()


def inner_xml(el):
    """Serialise an element's text + children (not the element tag itself)."""
    out = [html.escape(el.text or "", quote=False)]
    for child in el:
        out.append(ET.tostring(child, encoding="unicode"))
    return "".join(out)


def seg_text(segments):
    return "\n".join(s["text"] for s in segments)


class Course:
    def __init__(self, path):
        self.files = {}
        with tarfile.open(path, "r:gz") as tf:
            for m in tf:
                if m.isfile() and m.name.endswith((".xml", ".html")) \
                        and "/static/" not in m.name:
                    self.files[m.name.split("/", 1)[1]] = tf.extractfile(m).read().decode("utf-8")

    def xml(self, kind, uid):
        return ET.fromstring(self.files[f"{kind}/{uid}.xml"])

    def html_body(self, uid):
        return self.files[f"html/{uid}.html"]


def html_block(course, uid, ctx):
    name = course.xml("html", uid).get("display_name")
    raw = course.html_body(uid)
    segments, links, images = extract(raw)
    # CSS "cards" that only wrap a link (slides, PDF downloads) carry no lesson
    # prose: they have a <style> block and almost no text. Prose blocks that
    # merely embed a <style> block are kept as text.
    words = sum(len(s["text"].split()) for s in segments)
    is_card = "<style" in raw and (words <= 30 or "Raw HTML editor" in raw)
    kind = "link_card" if is_card else "text"
    return {"id": uid, "type": "html", "kind": kind, "display_name": name, **ctx,
            "text": seg_text(segments), "segments": segments,
            "links": links, "images": images,
            "source_file": f"html/{uid}.html"}


def problem_block(course, uid, ctx, library=None):
    root = course.xml("problem", uid)
    # Question prompt: everything inside the response except <choicegroup>,
    # plus any loose content directly under <problem> (except <solution>).
    prompt_parts, choices, images = [], [], []
    sol = root.find("solution")  # may also be nested inside the response
    for child in root:
        if child.tag == "solution":
            continue
        if child.tag.endswith("response"):
            for sub in child:
                if sub.tag == "solution":
                    sol = sub
                elif sub.tag == "choicegroup":
                    for ch in sub.findall("choice"):
                        hint = ch.find("choicehint")
                        hint_segs = extract(ET.tostring(hint, encoding="unicode"))[0] if hint is not None else []
                        body = ET.Element("div")
                        body.text = ch.text
                        for c in ch:
                            if c.tag != "choicehint":
                                body.append(c)
                        segs, _, imgs = extract(ET.tostring(body, encoding="unicode"))
                        images += imgs
                        choices.append({"text": seg_text(segs),
                                        "correct": ch.get("correct") == "true",
                                        "hint": seg_text(hint_segs)})
                else:
                    segs, _, imgs = extract(ET.tostring(sub, encoding="unicode"))
                    prompt_parts += segs
                    images += imgs
        else:
            segs, _, imgs = extract(ET.tostring(child, encoding="unicode"))
            prompt_parts += segs
            images += imgs
    solution = seg_text(extract(ET.tostring(sol, encoding="unicode"))[0]) if sol is not None else ""
    solution = re.sub(r"^Explanation\n?", "", solution)
    return {"id": uid, "type": "problem", "display_name": root.get("display_name"), **ctx,
            "library": library, "question": seg_text(prompt_parts),
            "choices": choices, "solution": solution, "images": images,
            "source_file": f"problem/{uid}.xml"}


def build(course):
    blocks, tree = [], []
    chapters = [c.get("url_name") for c in course.xml("course", "2026") if c.tag == "chapter"] \
        if "course/2026.xml" in course.files else None
    if chapters is None:  # fall back to the url_name named in course.xml
        root = ET.fromstring(course.files["course.xml"])
        chapters = [c.get("url_name") for c in course.xml("course", root.get("url_name"))
                    if c.tag == "chapter"]
    for ci, cid in enumerate(chapters, 1):
        ch = course.xml("chapter", cid)
        day = ch.get("display_name")
        ch_node = {"id": cid, "title": day, "index": ci, "sequentials": []}
        for sid in [s.get("url_name") for s in ch]:
            seq = course.xml("sequential", sid)
            s_node = {"id": sid, "title": seq.get("display_name"), "verticals": []}
            for vid in [v.get("url_name") for v in seq]:
                vert = course.xml("vertical", vid)
                v_node = {"id": vid, "title": vert.get("display_name"), "components": []}
                ctx = {"chapter": day, "chapter_index": ci,
                       "sequential": s_node["title"], "vertical": v_node["title"]}

                def add(comp_tag, uid, library=None):
                    if comp_tag == "html":
                        b = html_block(course, uid, ctx)
                    elif comp_tag == "problem":
                        b = problem_block(course, uid, ctx, library)
                    else:
                        return
                    b["order"] = len(blocks)
                    blocks.append(b)

                for comp in vert:
                    u = comp.get("url_name")
                    entry = {"type": comp.tag, "id": u}
                    if comp.tag == "video":
                        v = course.xml("video", u)
                        entry.update(display_name=v.get("display_name"),
                                     youtube_id=v.get("youtube_id_1_0"))
                    elif comp.tag == "library_content":
                        lc = course.xml("library_content", u)
                        entry.update(display_name=lc.get("display_name"),
                                     max_count=lc.get("max_count"),
                                     problems=[p.get("url_name") for p in lc])
                        for p in lc:
                            add("problem", p.get("url_name"), library=u)
                    else:
                        add(comp.tag, u)
                    v_node["components"].append(entry)
                s_node["verticals"].append(v_node)
            ch_node["sequentials"].append(s_node)
        tree.append(ch_node)
    return blocks, tree


def write_markdown(blocks, out):
    d = out / "by_day"
    d.mkdir(parents=True, exist_ok=True)
    days = {}
    for b in blocks:
        days.setdefault(b["chapter_index"], []).append(b)
    for idx, bs in days.items():
        lines = [f"# {bs[0]['chapter']}", ""]
        last = None
        for b in bs:
            where = (b["sequential"], b["vertical"])
            if where != last:
                lines += [f"## {b['sequential']} / {b['vertical']}", ""]
                last = where
            if b["type"] == "html":
                tag = "TEXT" if b["kind"] == "text" else "LINK CARD"
                lines += [f"<!-- {tag} {b['id']} ({b['display_name']}) -->"]
                for s in b["segments"]:
                    lines.append({"heading": "### ", "list_item": "- "}.get(s["type"], "") + s["text"])
                    lines.append("") if s["type"] != "list_item" else None
                lines.append("")
            else:
                lib = f" library={b['library']}" if b["library"] else ""
                lines += [f"<!-- PROBLEM {b['id']}{lib} -->", f"**Q:** {b['question']}", ""]
                for i, c in enumerate(b["choices"]):
                    lines.append(f"{chr(65 + i)}. {'[correct] ' if c['correct'] else ''}{c['text']}")
                    if c["hint"]:
                        lines.append(f"   - feedback: {c['hint']}")
                if b["solution"]:
                    lines += ["", f"**Explanation:** {b['solution']}"]
                lines.append("")
        (d / f"day_{idx:02d}.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("archive")
    ap.add_argument("--out", default="data/extracted")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    blocks, tree = build(Course(args.archive))
    (out / "blocks.json").write_text(json.dumps(blocks, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "structure.json").write_text(json.dumps(tree, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(blocks, out)
    text = [b for b in blocks if b["type"] == "html" and b["kind"] == "text"]
    cards = [b for b in blocks if b["type"] == "html" and b["kind"] == "link_card"]
    probs = [b for b in blocks if b["type"] == "problem"]
    print(f"chapters: {len(tree)}  text blocks: {len(text)}  link cards: {len(cards)}  problems: {len(probs)}")
    print(f"text words: {sum(len(b['text'].split()) for b in text)}")
    print(f"wrote {out}/blocks.json, structure.json, by_day/")


if __name__ == "__main__":
    main()
