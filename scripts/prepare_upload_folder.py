#!/usr/bin/env python3
"""Collect every generated mp3 into one flat folder (and numbered batch folders) ready to upload to S3.

Uses hard links, so no extra disk space is used. Writes:
  data/audio_upload/audio/            every mp3, flat  (upload this folder as `audio/` with the AWS CLI)
  data/audio_upload/batch_NN/         the same files in batches (open one, select all files, drop them into the
                                      bucket's audio/ folder when dragging in the S3 console)
  data/audio_upload/expected_files.txt  every file name with its size in bytes, to check the bucket against
  data/audio_upload/to_upload/        ONLY the files not yet uploaded (new, or changed since the last upload)
Run again after generating more audio; it only adds what is new.

Workflow for drag and drop:
  1. python3 -I scripts/prepare_upload_folder.py            -> upload batch_01.., then run:
  2. python3 -I scripts/prepare_upload_folder.py --mark-uploaded    (records what is now in the bucket)
  3. later, after more audio is generated: run it again and upload just the files in to_upload/

  python3 -I scripts/prepare_upload_folder.py [--batch-size 250] [--mark-uploaded]
"""
import argparse
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ap = argparse.ArgumentParser()
ap.add_argument("--batch-size", type=int, default=250)
ap.add_argument("--mark-uploaded", action="store_true",
                help="record every file currently in data/audio as uploaded (do this after uploading)")
ap.add_argument("--uploaded-file", default=str(ROOT / "data/audio_upload/uploaded_files.txt"))
args = ap.parse_args()

manifest = json.loads((ROOT / "data/audio/manifest.json").read_text(encoding="utf-8"))
files = sorted((name, ROOT / "data/audio" / rec["lang"] / name) for name, rec in manifest.items())
files = [(n, p) for n, p in files if p.exists()]
out = ROOT / "data/audio_upload"
flat = out / "audio"
flat.mkdir(parents=True, exist_ok=True)


def link(src, dst):
    if dst.exists() and dst.stat().st_ino == src.stat().st_ino:
        return
    if dst.exists():
        dst.unlink()
    os.link(src, dst)


for n, p in files:
    link(p, flat / n)
for i in range(0, len(files), args.batch_size):
    b = out / f"batch_{i // args.batch_size + 1:02d}"
    b.mkdir(exist_ok=True)
    for n, p in files[i:i + args.batch_size]:
        link(p, b / n)
uploaded = {}
uf = Path(args.uploaded_file)
if uf.exists():
    for line in uf.read_text(encoding="utf-8").splitlines():
        name, size = line.split("\t")
        uploaded[name] = int(size)
if args.mark_uploaded:
    uf.write_text("".join(f"{n}\t{p.stat().st_size}\n" for n, p in files), encoding="utf-8")
    print(f"recorded {len(files)} files as uploaded in {uf}")
    uploaded = {n: p.stat().st_size for n, p in files}
new = out / "to_upload"
new.mkdir(exist_ok=True)
for old in new.iterdir():
    old.unlink()
todo = [(n, p) for n, p in files if uploaded.get(n) != p.stat().st_size]
for n, p in todo:
    link(p, new / n)
print(f"to_upload/: {len(todo)} files not yet uploaded" + (" (nothing recorded as uploaded yet)" if not uploaded else ""))
(out / "expected_files.txt").write_text("".join(f"{n}\t{p.stat().st_size}\n" for n, p in files), encoding="utf-8")
print(f"{len(files)} files, {sum(p.stat().st_size for _, p in files) / 1e6:.0f} MB -> {flat} and "
      f"{(len(files) + args.batch_size - 1) // args.batch_size} batch folders")
