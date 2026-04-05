#!/usr/bin/env python3
"""
List contents of Dropbox Smart Sync placeholder .tgz and .zip files without downloading them.

For .tgz files, the archive is streamed and only the tar headers are read.
For .zip files, HTTP Range requests fetch just the central directory index from the end
of the file (~3 requests total, regardless of archive size).

Usage:
    DROPBOX_TOKEN=xxx python3 list_dropbox_archives.py <pattern> [<pattern> ...]
    DROPBOX_TOKEN=xxx python3 list_dropbox_archives.py *.tgz
    DROPBOX_TOKEN=xxx python3 list_dropbox_archives.py *.zip
    DROPBOX_TOKEN=xxx python3 list_dropbox_archives.py *.tgz *.zip
    DROPBOX_TOKEN=xxx python3 list_dropbox_archives.py archive1.tgz archive2.zip
    DROPBOX_TOKEN=xxx python3 list_dropbox_archives.py /path/to/*.tgz /other/path/*.zip

Arguments:
    --dropbox-root   Local Dropbox root path (default: ~/Dropbox)
    --list           Just list archive files and their sizes without downloading
"""

import argparse
import glob
import os
import sys
import tarfile
import time
import zipfile
from pathlib import Path

import requests
from tqdm import tqdm


def get_file_metadata(token: str, dropbox_path: str) -> dict:
    resp = requests.post(
        "https://api.dropboxapi.com/2/files/get_metadata",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"path": dropbox_path},
    )
    resp.raise_for_status()
    return resp.json()


class RangeRequestFile:
    """Seekable file-like object backed by HTTP Range requests."""

    def __init__(self, url: str) -> None:
        # Use a minimal range GET (not HEAD) so redirects are followed and we
        # can confirm the server supports range requests via the 206 status.
        resp = requests.get(url, headers={"Range": "bytes=0-0"})
        resp.raise_for_status()
        if resp.status_code != 206:
            raise ValueError(
                f"Server does not support range requests (got {resp.status_code}, expected 206)"
            )
        content_range = resp.headers.get("Content-Range", "")
        if not content_range.startswith("bytes "):
            raise ValueError(f"Unexpected Content-Range header: {content_range!r}")
        self._url = url
        self._size = int(content_range.split("/")[1])
        self._pos = 0

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            self._pos = offset
        elif whence == 1:
            self._pos += offset
        elif whence == 2:
            self._pos = self._size + offset
        self._pos = max(0, min(self._pos, self._size))
        return self._pos

    def tell(self) -> int:
        return self._pos

    def read(self, n: int = -1) -> bytes:
        if n == 0:
            return b""
        start = self._pos
        end = self._size - 1 if n == -1 else min(self._pos + n - 1, self._size - 1)
        if start > end:
            return b""
        resp = requests.get(self._url, headers={"Range": f"bytes={start}-{end}"})
        resp.raise_for_status()
        data = resp.content
        self._pos += len(data)
        return data


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


class _ProgressStream:
    """Wraps a urllib3 response stream to update a tqdm bar as bytes are read."""

    def __init__(self, raw, bar: tqdm):
        self._raw = raw
        self._bar = bar

    def read(self, amt=None):
        chunk = self._raw.read(amt)
        if chunk:
            self._bar.update(len(chunk))
        return chunk

    # tarfile also calls readable() / seekable() on some paths
    def readable(self):
        return True

    def seekable(self):
        return False


def list_archive_contents(url: str, file_size: int | None, label: str) -> list[str]:
    with requests.get(url, stream=True) as r:
        r.raise_for_status()
        r.raw.decode_content = True
        with tqdm(
            total=file_size,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc=label,
            leave=False,
            dynamic_ncols=True,
        ) as bar:
            stream = _ProgressStream(r.raw, bar)
            with tarfile.open(fileobj=stream, mode="r|gz") as tar:
                return [member.name for member in tar]


def list_zip_contents(url: str) -> list[str]:
    with zipfile.ZipFile(RangeRequestFile(url)) as zf:
        return zf.namelist()


def local_to_dropbox_path(local_path: Path, dropbox_root: Path) -> str:
    relative = local_path.relative_to(dropbox_root)
    return "/" + str(relative).replace(os.sep, "/")


def resolve_paths(patterns: list[str]) -> list[Path]:
    """Expand wildcards and resolve to absolute paths, deduplicating."""
    seen = set()
    paths = []
    for pattern in patterns:
        expanded = glob.glob(os.path.expanduser(pattern))
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


def _display_path(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s"


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "files",
        nargs="+",
        help="Filenames or glob patterns (e.g. *.tgz, *.zip, /path/to/*.tgz)",
    )
    parser.add_argument(
        "--dropbox-root",
        default="~/Dropbox",
        help="Local Dropbox root directory (default: ~/Dropbox)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Just list archive files and their sizes without downloading",
    )
    args = parser.parse_args()

    token = os.environ.get("DROPBOX_TOKEN")
    if not token:
        print("Error: DROPBOX_TOKEN environment variable not set", file=sys.stderr)
        sys.exit(1)

    dropbox_root = Path(args.dropbox_root).expanduser().resolve()
    archive_files = resolve_paths(args.files)

    if not archive_files:
        print("No files to process.")
        sys.exit(0)

    if args.list:
        for archive_path in archive_files:
            display = _display_path(archive_path)
            try:
                dropbox_path = local_to_dropbox_path(archive_path, dropbox_root)
            except ValueError:
                print(f"{'ERR':>8}  {display}")
                continue
            try:
                metadata = get_file_metadata(token, dropbox_path)
                file_size = metadata.get("size")
                if file_size is not None:
                    if file_size >= 1_073_741_824:
                        size_str = f"{file_size / 1_073_741_824:.1f} GB"
                    else:
                        size_str = f"{file_size / 1_048_576:.1f} MB"
                else:
                    size_str = "?"
            except requests.HTTPError as e:
                size_str = f"HTTP {e.response.status_code}"
            print(f"{size_str:>8}  {display}")
        sys.exit(0)

    print(f"Processing {len(archive_files)} archive(s)\n")

    total_start = time.perf_counter()

    try:
        for archive_path in archive_files:
            name = archive_path.name
            stem = name[: -len(".tar.gz")] if name.endswith(".tar.gz") else archive_path.stem
            output_path = archive_path.parent / (stem + ".txt")

            if output_path.exists():
                print(f"[SKIP] {name} — listing already exists ({_display_path(output_path)})")
                continue

            try:
                dropbox_path = local_to_dropbox_path(archive_path, dropbox_root)
            except ValueError:
                print(f"[FAIL] {archive_path} — not under Dropbox root ({dropbox_root})")
                continue

            file_start = time.perf_counter()

            try:
                metadata = get_file_metadata(token, dropbox_path)
                file_size = metadata.get("size")  # bytes, may be absent for placeholders
            except requests.HTTPError:
                file_size = None

            try:
                link = get_temporary_link(token, dropbox_path)

                suffix = archive_path.suffix.lower()
                if suffix == ".zip":
                    contents = list_zip_contents(link)
                elif suffix in (".tgz", ".gz"):
                    contents = list_archive_contents(link, file_size, name)
                else:
                    raise ValueError(f"Unsupported archive type: {archive_path.suffix}")

                output_path.write_text("\n".join(contents) + "\n")
                elapsed = time.perf_counter() - file_start
                size_str = f"{file_size / 1_048_576:.1f} MB" if file_size else "? MB"
                print(
                    f"[ OK ] {name} — {len(contents)} entries"
                    f" | {size_str} | {_fmt_duration(elapsed)}"
                    f" → {_display_path(output_path)}"
                )

            except requests.HTTPError as e:
                elapsed = time.perf_counter() - file_start
                print(
                    f"[FAIL] {name} — HTTP {e.response.status_code}: {e.response.text}"
                    f" ({_fmt_duration(elapsed)})"
                )
            except tarfile.TarError as e:
                elapsed = time.perf_counter() - file_start
                print(f"[FAIL] {name} — tar error: {e} ({_fmt_duration(elapsed)})")
            except zipfile.BadZipFile as e:
                elapsed = time.perf_counter() - file_start
                print(f"[FAIL] {name} — bad zip: {e} ({_fmt_duration(elapsed)})")
            except ValueError as e:
                elapsed = time.perf_counter() - file_start
                print(f"[FAIL] {name} — {e} ({_fmt_duration(elapsed)})")
            except Exception as e:
                elapsed = time.perf_counter() - file_start
                print(f"[FAIL] {name} — {e} ({_fmt_duration(elapsed)})")

    except KeyboardInterrupt:
        print("\n[INTERRUPTED]", file=sys.stderr)
        sys.exit(130)

    total_elapsed = time.perf_counter() - total_start
    print(f"\nDone in {_fmt_duration(total_elapsed)}")


if __name__ == "__main__":
    main()
