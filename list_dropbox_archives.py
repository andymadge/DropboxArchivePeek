#!/usr/bin/env python3
"""
List contents of Dropbox Smart Sync placeholder .tgz files without downloading them.

Usage:
    DROPBOX_TOKEN=xxx python3 list_archives.py <pattern> [<pattern> ...]
    DROPBOX_TOKEN=xxx python3 list_archives.py *.tgz
    DROPBOX_TOKEN=xxx python3 list_archives.py archive1.tgz archive2.tgz
    DROPBOX_TOKEN=xxx python3 list_archives.py /path/to/*.tgz /other/path/*.tgz

Arguments:
    --dropbox-root   Local Dropbox root path (default: ~/Dropbox)
"""

import argparse
import glob
import os
import sys
import tarfile
from pathlib import Path

import requests


def get_temporary_link(token: str, dropbox_path: str) -> str:
    resp = requests.post(
        "https://api.dropboxapi.com/2/files/get_temporary_link",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"path": dropbox_path},
    )
    resp.raise_for_status()
    return resp.json()["link"]


def list_archive_contents(url: str) -> list[str]:
    with requests.get(url, stream=True) as r:
        r.raise_for_status()
        r.raw.decode_content = True
        with tarfile.open(fileobj=r.raw, mode="r|gz") as tar:
            return [member.name for member in tar]


def local_to_dropbox_path(local_path: Path, dropbox_root: Path) -> str:
    relative = local_path.relative_to(dropbox_root)
    return "/" + str(relative).replace(os.sep, "/")


def resolve_paths(patterns: list[str]) -> list[Path]:
    """Expand wildcards and resolve to absolute paths, deduplicating."""
    seen = set()
    paths = []
    for pattern in patterns:
        expanded = glob.glob(pattern)
        if not expanded:
            print(f"[WARN] No files matched: {pattern}", file=sys.stderr)
            continue
        for match in sorted(expanded):
            path = Path(match).resolve()
            if path in seen:
                continue
            seen.add(path)
            if not path.is_file():
                print(f"[WARN] Not a file, skipping: {path}", file=sys.stderr)
                continue
            paths.append(path)
    return paths


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "files",
        nargs="+",
        help="Filenames or glob patterns (e.g. *.tgz, /path/to/*.tgz)",
    )
    parser.add_argument(
        "--dropbox-root",
        default="~/Dropbox",
        help="Local Dropbox root directory (default: ~/Dropbox)",
    )
    args = parser.parse_args()

    token = os.environ.get("DROPBOX_TOKEN")
    if not token:
        print("Error: DROPBOX_TOKEN environment variable not set", file=sys.stderr)
        sys.exit(1)

    dropbox_root = Path(args.dropbox_root).expanduser().resolve()
    tgz_files = resolve_paths(args.files)

    if not tgz_files:
        print("No files to process.")
        sys.exit(0)

    print(f"Processing {len(tgz_files)} archive(s)\n")

    for tgz_path in tgz_files:
        output_path = tgz_path.parent / (tgz_path.stem + ".txt")

        if output_path.exists():
            print(f"[SKIP] {tgz_path.name} — listing already exists ({output_path})")
            continue

        try:
            dropbox_path = local_to_dropbox_path(tgz_path, dropbox_root)
        except ValueError:
            print(f"[FAIL] {tgz_path} — not under Dropbox root ({dropbox_root})")
            continue

        print(f"[....] {tgz_path.name}", end="", flush=True)

        try:
            link = get_temporary_link(token, dropbox_path)
            contents = list_archive_contents(link)
            output_path.write_text("\n".join(contents) + "\n")
            print(f"\r[ OK ] {tgz_path.name} — {len(contents)} entries → {output_path}")

        except requests.HTTPError as e:
            print(
                f"\r[FAIL] {tgz_path.name} — HTTP {e.response.status_code}: {e.response.text}"
            )
        except tarfile.TarError as e:
            print(f"\r[FAIL] {tgz_path.name} — tar error: {e}")
        except Exception as e:
            print(f"\r[FAIL] {tgz_path.name} — {e}")


if __name__ == "__main__":
    main()
