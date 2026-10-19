#!/usr/bin/env python3
"""Upload the generated audio (data/audio/<lang>/*.mp3) to the course's S3 bucket.

Safety
  * Uses ONLY the named AWS profile in AUDIO_AWS_PROFILE (never the default profile), so it cannot touch the
    AWS account that the command line is normally logged in to.
  * Before anything is uploaded it checks that the profile belongs to AUDIO_AWS_ACCOUNT_ID and that the bucket
    is owned by that same account; otherwise it stops.
  * Never deletes anything in the bucket. Files whose size already matches are skipped, so it can be re-run.
  * Without --yes it asks before uploading.

One-time setup (the keys are typed into your own terminal and never go through chat):
  1. In AWS (IAM) create a user for this job with only this permission, then make an access key for it:
       {"Version": "2012-10-17", "Statement": [
         {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": "arn:aws:s3:::BUCKET"},
         {"Effect": "Allow", "Action": ["s3:PutObject"], "Resource": "arn:aws:s3:::BUCKET/audio/*"}]}
  2. aws configure --profile webuddhist-audio        (paste the access key; region ap-south-1)
  3. Add to .env (not secret values):
       AUDIO_S3_BUCKET=your-bucket-name
       AUDIO_S3_PREFIX=audio/                (optional, default audio/)
       AUDIO_AWS_PROFILE=webuddhist-audio
       AUDIO_AWS_ACCOUNT_ID=123456789012     (the 12-digit id of the account that owns the bucket)
       AUDIO_AWS_REGION=ap-south-1           (optional)

Usage:
  python3 -I scripts/upload_audio.py --dry-run     # what would be uploaded; nothing is sent (no AWS calls)
  python3 -I scripts/upload_audio.py --check       # verify account + bucket + what is already there; uploads nothing
  python3 -I scripts/upload_audio.py               # check account + bucket, ask, upload, verify
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LANGS = ["en", "hi", "ne", "bo"]


def env(name, default=None):
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip()
    return default


def aws(profile, region, *args):
    """Run an aws command with the named profile; returns stdout, or exits with the error text."""
    cmd = ["aws", *args, "--profile", profile, "--region", region]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"aws {' '.join(args[:2])} failed: {r.stderr.strip()[:400]}")
    return r.stdout


def local_files(langs):
    manifest = json.loads((ROOT / "data/audio/manifest.json").read_text(encoding="utf-8"))
    files = {}
    for name, rec in manifest.items():
        p = ROOT / "data/audio" / rec["lang"] / name
        if rec["lang"] in langs and p.exists():
            files[name] = (p, p.stat().st_size)
    return files


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="only list what would be uploaded (no AWS calls)")
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    ap.add_argument("--check", action="store_true",
                    help="check the account, the bucket and what is already uploaded, then stop (nothing is uploaded)")
    ap.add_argument("--langs", default=",".join(LANGS))
    ap.add_argument("--cache-seconds", type=int, default=86400, help="Cache-Control max-age for the files")
    args = ap.parse_args()

    bucket, profile = env("AUDIO_S3_BUCKET"), env("AUDIO_AWS_PROFILE")
    account, region = env("AUDIO_AWS_ACCOUNT_ID"), env("AUDIO_AWS_REGION", "ap-south-1")
    prefix = (env("AUDIO_S3_PREFIX", "audio/") or "").lstrip("/")
    prefix = prefix if prefix.endswith("/") or not prefix else prefix + "/"
    langs = args.langs.split(",")
    files = local_files(langs)
    mb = sum(s for _, s in files.values()) / 1e6
    print(f"local audio files: {len(files)} ({mb:.0f} MB) in {', '.join(langs)}")

    if args.dry_run:
        print(f"[dry run] would upload to s3://{bucket or '<AUDIO_S3_BUCKET not set>'}/{prefix} using profile "
              f"{profile or '<AUDIO_AWS_PROFILE not set>'}; nothing was sent.")
        for n in sorted(files)[:5]:
            print("   ", n)
        return

    missing_cfg = [k for k, v in {"AUDIO_S3_BUCKET": bucket, "AUDIO_AWS_PROFILE": profile,
                                  "AUDIO_AWS_ACCOUNT_ID": account}.items() if not v]
    if missing_cfg:
        sys.exit(f"set {', '.join(missing_cfg)} in .env first (see the top of this script)")
    if profile == "default":
        sys.exit("refusing to use the default AWS profile; create a separate one (aws configure --profile ...)")

    ident = json.loads(aws(profile, region, "sts", "get-caller-identity", "--output", "json"))
    print(f"profile '{profile}' is account {ident['Account']} ({ident['Arn']})")
    if ident["Account"] != account:
        sys.exit(f"STOP: this profile belongs to account {ident['Account']}, not the expected {account}")
    aws(profile, region, "s3api", "head-bucket", "--bucket", bucket, "--expected-bucket-owner", account)
    print(f"bucket {bucket} exists and belongs to account {account}")

    def remote():
        out = aws(profile, region, "s3api", "list-objects-v2", "--bucket", bucket, "--prefix", prefix,
                  "--output", "json")
        return {o["Key"][len(prefix):]: o["Size"] for o in (json.loads(out or "{}").get("Contents") or [])}

    have = remote()
    todo = {n: v for n, v in files.items() if have.get(n) != v[1]}
    print(f"already in the bucket with the same size: {len(files) - len(todo)} | to upload: {len(todo)}")
    if args.check:
        print("[check only] nothing was uploaded")
        return
    if not todo:
        print("nothing to upload")
    else:
        if not args.yes and input(f"Upload {len(todo)} files to s3://{bucket}/{prefix} in account {account}? type yes: ").strip() != "yes":
            sys.exit("cancelled")
        for lang in langs:
            if any(rec_lang(n) == lang for n in todo):
                subprocess.run(["aws", "s3", "sync", str(ROOT / "data/audio" / lang), f"s3://{bucket}/{prefix}",
                                "--exclude", "*", "--include", "*.mp3", "--size-only",
                                "--content-type", "audio/mpeg",
                                "--cache-control", f"public, max-age={args.cache_seconds}",
                                "--only-show-errors", "--profile", profile, "--region", region], check=True)

    have = remote()
    bad = [n for n, (_, size) in files.items() if have.get(n) != size]
    print(f"verified in bucket: {len(files) - len(bad)} of {len(files)}" + (f" | MISSING or wrong size: {bad[:10]}" if bad else " - all present"))
    sys.exit(1 if bad else 0)


def rec_lang(name):
    return name.rsplit("_", 1)[1][:-4]


if __name__ == "__main__":
    main()
