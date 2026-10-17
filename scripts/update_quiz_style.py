#!/usr/bin/env python3
"""Replace the quiz stylesheet in an already patched course export.

patch_course_export.py puts one <style> block (QUIZ_AUDIO_CSS) in every quiz. When that stylesheet
changes, run this on the patched archive instead of rebuilding from the original export.
Everything except the <style> block of the quiz files is copied unchanged.

Usage:
  python3 -I scripts/update_quiz_style.py PATCHED.tar.gz --out NEW.tar.gz
"""
import argparse
import io
import re
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from patch_course_export import QUIZ_AUDIO_CSS  # noqa: E402

STYLE = re.compile(r"<style>(?:(?!</style>).)*\.quiz-audio(?:(?!</style>).)*</style>", re.S)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("archive")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    changed = missing = 0
    with tarfile.open(args.archive, "r:gz") as src, tarfile.open(args.out, "w:gz", compresslevel=6) as out:
        for m in src:
            if m.isfile() and m.name.startswith("course/problem/") and m.name.endswith(".xml"):
                raw = src.extractfile(m).read().decode("utf-8")
                new, n = STYLE.subn(lambda _: QUIZ_AUDIO_CSS, raw, count=1)
                missing += n == 0
                changed += new != raw
                data = new.encode("utf-8")
                m.size = len(data)
                out.addfile(m, io.BytesIO(data))
            else:
                out.addfile(m, src.extractfile(m) if m.isfile() else None)
    print(f"quiz stylesheets updated: {changed} | quizzes without the quiz stylesheet: {missing}")


if __name__ == "__main__":
    main()
