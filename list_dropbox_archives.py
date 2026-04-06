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
    --workers N      Number of parallel workers (default: 1 = sequential)

Directories are recursed automatically — specifying a directory finds all
.tgz, .gz, and .zip files within it and all subdirectories.
"""

import argparse
import glob
import logging
import os
import sys
import tarfile
import threading
import time
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

import requests
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Column

__version__ = "1.0.0"

_console = Console()
_logger = logging.getLogger(__name__)


class _GzipCheckpoint(NamedTuple):
    """Saved decompressor state at a known compressed byte offset."""
    http_pos: int    # byte offset in the compressed HTTP stream
    decompressor: object  # zlib.decompressobj copy


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


class ResumableGzipStream:
    """HTTP-backed gzip stream with checkpoint-based resumption.

    Decompresses gzip data using zlib directly so the decompressor state can
    be saved (via zlib.decompressobj.copy()) at tar entry boundaries. On
    connection failure, a new HTTP Range request picks up from the saved byte
    offset and the restored decompressor continues seamlessly — avoiding a
    full re-download from byte 0.

    Progress advances are batched and flushed every 0.25 s so Rich's
    speed-estimate window covers a meaningful period.
    """

    _CHECKPOINT_INTERVAL = 256 * 1024 * 1024  # save checkpoint every 256 MB compressed
    _FLUSH_INTERVAL = 0.25

    def __init__(
        self,
        url: str,
        checkpoint: _GzipCheckpoint | None = None,
        progress: Progress | None = None,
        task_id: TaskID | None = None,
        agg_task_id: TaskID | None = None,
    ) -> None:
        self._url = url
        self._progress = progress
        self._task_id = task_id
        self._agg_task_id = agg_task_id
        self._http_pos = 0
        self._buf = b""
        self._pending = 0
        self._pending_agg = 0
        self._last_flush = time.monotonic()
        self._bytes_since_checkpoint = 0
        self._checkpoint_http_pos = 0
        self._checkpoint_decompressor: object | None = None

        if checkpoint:
            self._http_pos = checkpoint.http_pos
            self._decompressor = checkpoint.decompressor.copy()
        else:
            # MAX_WBITS | 16 tells zlib to expect and strip the gzip header
            self._decompressor = zlib.decompressobj(zlib.MAX_WBITS | 16)

        self._response: requests.Response | None = None
        self._raw = None
        self._connect()

    def _connect(self) -> None:
        if self._response is not None:
            self._response.close()
        headers = {"Range": f"bytes={self._http_pos}-"} if self._http_pos > 0 else {}
        # timeout=(connect_s, read_s): read timeout fires if no data arrives for
        # 60 s, turning a silently stalled connection into a retryable error.
        self._response = requests.get(self._url, stream=True, headers=headers, timeout=(30, 60))
        self._response.raise_for_status()
        self._raw = self._response.raw

    def maybe_checkpoint(self) -> None:
        """Conditionally save a checkpoint at a tar entry boundary.

        Checkpoints are only saved when the internal buffer is empty, which
        guarantees that self._http_pos and the decompressor state correspond
        exactly to the bytes tarfile has already consumed. Restoring a
        checkpoint is then always safe regardless of where in the tar stream
        the caller is.
        """
        if self._bytes_since_checkpoint >= self._CHECKPOINT_INTERVAL and not self._buf:
            self._checkpoint_http_pos = self._http_pos
            self._checkpoint_decompressor = self._decompressor.copy()
            self._bytes_since_checkpoint = 0

    @property
    def checkpoint(self) -> _GzipCheckpoint | None:
        if self._checkpoint_decompressor is None:
            return None
        return _GzipCheckpoint(self._checkpoint_http_pos, self._checkpoint_decompressor)

    def read(self, n: int = -1) -> bytes:
        target = n if n >= 0 else float("inf")
        while len(self._buf) < target:
            compressed = self._raw.read(65536)
            if not compressed:
                break
            self._http_pos += len(compressed)
            self._bytes_since_checkpoint += len(compressed)
            self._pending += len(compressed)
            self._pending_agg += len(compressed)
            now = time.monotonic()
            if now - self._last_flush >= self._FLUSH_INTERVAL:
                self._do_flush()
                self._last_flush = now
            self._buf += self._decompressor.decompress(compressed)
        if n < 0:
            result, self._buf = self._buf, b""
        else:
            result, self._buf = self._buf[:n], self._buf[n:]
        return result

    def flush_progress(self) -> None:
        self._do_flush()

    def _do_flush(self) -> None:
        if self._progress and self._task_id is not None and self._pending:
            self._progress.advance(self._task_id, self._pending)
            self._pending = 0
        if self._progress and self._agg_task_id is not None and self._pending_agg:
            self._progress.advance(self._agg_task_id, self._pending_agg)
            self._pending_agg = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False


_RETRYABLE_ERRORS = (
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ConnectionError,
    requests.exceptions.ReadTimeout,
)
_MAX_RETRIES = 50


def list_archive_contents(
    url: str,
    file_size: int | None,
    label: str,
    progress: Progress | None = None,
    task_id: TaskID | None = None,
    agg_task_id: TaskID | None = None,
    max_retries: int = _MAX_RETRIES,
) -> list[str]:
    """Stream a TGZ archive and return all entry names.

    On connection failure, retries up to max_retries times. Each retry
    resumes from the last saved checkpoint (HTTP Range request + restored
    decompressor state) rather than re-downloading from byte 0.
    """
    collected: list[str] = []
    last_checkpoint: _GzipCheckpoint | None = None

    for attempt in range(max_retries + 1):
        skip = len(collected)
        if attempt > 0:
            if progress is not None and task_id is not None:
                progress.reset(task_id, total=file_size)
            if progress is not None:
                if last_checkpoint:
                    resume_str = f"resuming from {last_checkpoint.http_pos / 1024 ** 3:.1f} GB"
                else:
                    resume_str = "restarting from beginning"
                msg = (
                    f"[RETRY] {label} — connection dropped after {skip} entries,"
                    f" {resume_str} (attempt {attempt}/{max_retries})..."
                )
                progress.console.print(msg, markup=False)
                _logger.info(msg)
        stream = ResumableGzipStream(
            url,
            checkpoint=last_checkpoint,
            progress=progress,
            task_id=task_id,
            # Only advance the aggregate task on the first attempt to
            # avoid double-counting retried bytes.
            agg_task_id=agg_task_id if attempt == 0 else None,
        )
        try:
            # mode="r|" — raw uncompressed streaming tar; gzip is handled by ResumableGzipStream
            with tarfile.open(fileobj=stream, mode="r|") as tar:
                for i, member in enumerate(tar):
                    stream.maybe_checkpoint()
                    cp = stream.checkpoint
                    if cp is not None:
                        last_checkpoint = cp
                    if i >= skip:
                        collected.append(member.name)
            stream.flush_progress()
            return collected
        except _RETRYABLE_ERRORS as exc:
            stream.flush_progress()
            bytes_str = f"{stream._http_pos / 1024 ** 3:.1f} GB" if stream._http_pos else "0 bytes"
            _logger.info(
                f"[ERROR] {label} — {type(exc).__name__}: {exc}"
                f" (attempt {attempt}/{max_retries}, downloaded {bytes_str},"
                f" {len(collected)} entries so far)"
            )
            if attempt >= max_retries:
                raise


def list_zip_contents(url: str) -> list[str]:
    with zipfile.ZipFile(RangeRequestFile(url)) as zf:
        return zf.namelist()


def local_to_dropbox_path(local_path: Path, dropbox_root: Path) -> str:
    relative = local_path.relative_to(dropbox_root)
    return "/" + str(relative).replace(os.sep, "/")


_ARCHIVE_SUFFIXES = {".tgz", ".gz", ".zip"}


def resolve_paths(patterns: list[str]) -> list[tuple[Path, Path]]:
    """Expand wildcards and resolve to absolute paths, deduplicating.

    Returns a list of (archive_path, display_root) pairs. For directory
    arguments, display_root is the directory so paths can be shown relative
    to it. For individual files or globs, display_root is the file's parent
    (equivalent to showing just the filename).

    If a pattern resolves to a directory, all archive files within it are
    found recursively.
    """
    seen: set[Path] = set()
    paths: list[tuple[Path, Path]] = []
    for pattern in patterns:
        candidate = Path(os.path.expanduser(pattern)).resolve()
        if candidate.is_dir():
            matches = sorted(p for p in candidate.rglob("*") if p.is_file() and p.suffix.lower() in _ARCHIVE_SUFFIXES)
            if not matches:
                print(f"[WARN] No archives found in: {pattern}", file=sys.stderr)
            for path in matches:
                if path not in seen:
                    seen.add(path)
                    paths.append((path, candidate))
            continue
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
            if path.suffix.lower() not in _ARCHIVE_SUFFIXES:
                continue
            paths.append((path, path.parent))
    return paths


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s"


def process_one(
    archive_path: Path,
    display_root: Path,
    dropbox_root: Path,
    token: str,
    progress: Progress,
    agg_task_id: TaskID,
    agg_lock: threading.Lock,
    stop_new: threading.Event,
    sort: bool = True,
    max_retries: int = _MAX_RETRIES,
) -> None:
    if stop_new.is_set():
        return

    raw_name = archive_path.name
    label = str(archive_path.relative_to(display_root))
    stem = raw_name[: -len(".tar.gz")] if raw_name.endswith(".tar.gz") else archive_path.stem
    output_path = archive_path.parent / (stem + ".txt")
    output_label = str(output_path.relative_to(display_root))

    if output_path.exists():
        msg = f"[SKIP] {label} — listing already exists ({output_label})"
        progress.console.print(msg, markup=False)
        _logger.info(msg)
        return

    try:
        dropbox_path = local_to_dropbox_path(archive_path, dropbox_root)
    except ValueError:
        msg = f"[FAIL] {label} — not under Dropbox root ({dropbox_root})"
        progress.console.print(msg, markup=False)
        _logger.info(msg)
        return

    file_start = time.perf_counter()

    try:
        metadata = get_file_metadata(token, dropbox_path)
        file_size = metadata.get("size")  # bytes, may be absent for placeholders
    except requests.HTTPError as e:
        if e.response.status_code == 401:
            stop_new.set()
            msg = f"[FAIL] {label} — HTTP 401: {e.response.text}"
            progress.console.print(msg, markup=False)
            _logger.info(msg)
            return
        file_size = None

    if file_size:
        with agg_lock:
            current_total = progress.tasks[agg_task_id].total or 0
            progress.update(agg_task_id, total=current_total + file_size)

    task_id = None
    try:
        link = get_temporary_link(token, dropbox_path)
        task_id = progress.add_task(label, total=file_size)

        if archive_path.suffix.lower() == ".zip":
            contents = list_zip_contents(link)
        else:
            contents = list_archive_contents(link, file_size, label, progress, task_id, agg_task_id, max_retries)

        output_path.write_text("\n".join(sorted(contents) if sort else contents) + "\n")
        elapsed = time.perf_counter() - file_start
        size_str = f"{file_size / 1_048_576:,.1f} MB" if file_size else "? MB"
        msg = (
            f"[ OK ] {label} — {len(contents)} entries"
            f" | {size_str} | {_fmt_duration(elapsed)}"
            f" → {output_label}"
        )
        progress.console.print(msg, markup=False)
        _logger.info(msg)

    except requests.HTTPError as e:
        elapsed = time.perf_counter() - file_start
        if e.response.status_code == 401:
            stop_new.set()
        msg = (
            f"[FAIL] {label} — HTTP {e.response.status_code}: {e.response.text}"
            f" ({_fmt_duration(elapsed)})"
        )
        progress.console.print(msg, markup=False)
        _logger.info(msg)
    except tarfile.TarError as e:
        elapsed = time.perf_counter() - file_start
        msg = f"[FAIL] {label} — tar error: {e} ({_fmt_duration(elapsed)})"
        progress.console.print(msg, markup=False)
        _logger.info(msg)
    except zipfile.BadZipFile as e:
        elapsed = time.perf_counter() - file_start
        msg = f"[FAIL] {label} — bad zip: {e} ({_fmt_duration(elapsed)})"
        progress.console.print(msg, markup=False)
        _logger.info(msg)
    except ValueError as e:
        elapsed = time.perf_counter() - file_start
        msg = f"[FAIL] {label} — {e} ({_fmt_duration(elapsed)})"
        progress.console.print(msg, markup=False)
        _logger.info(msg)
    except Exception as e:
        elapsed = time.perf_counter() - file_start
        msg = f"[FAIL] {label} — {e} ({_fmt_duration(elapsed)})"
        progress.console.print(msg, markup=False)
        _logger.info(msg)
    finally:
        if task_id is not None:
            progress.remove_task(task_id)


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
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        metavar="N",
        help="Number of parallel workers (default: 1 = sequential)",
    )
    parser.add_argument(
        "--no-sort",
        action="store_true",
        help="Write archive entries in original order instead of sorted",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=_MAX_RETRIES,
        metavar="N",
        help=f"Max retries per archive on connection failure (default: {_MAX_RETRIES})",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
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
        for archive_path, display_root in archive_files:
            label = str(archive_path.relative_to(display_root))
            raw_name = archive_path.name
            stem = raw_name[: -len(".tar.gz")] if raw_name.endswith(".tar.gz") else archive_path.stem
            indexed = "[yes]" if (archive_path.parent / (stem + ".txt")).exists() else "[ no]"
            try:
                dropbox_path = local_to_dropbox_path(archive_path, dropbox_root)
            except ValueError:
                print(f"{'ERR':>8}  {indexed}  {label}")
                continue
            try:
                metadata = get_file_metadata(token, dropbox_path)
                file_size = metadata.get("size")
                if file_size is not None:
                    if file_size >= 1_073_741_824:
                        size_str = f"{file_size / 1_073_741_824:,.1f} GB"
                    else:
                        size_str = f"{file_size / 1_048_576:,.1f} MB"
                else:
                    size_str = "?"
            except requests.HTTPError as e:
                size_str = f"HTTP {e.response.status_code}"
            print(f"{size_str:>8}  {indexed}  {label}")
        sys.exit(0)

    log_path = Path(f"dropbox_lister_{datetime.now():%Y%m%d_%H%M%S}.log")
    _fh = logging.FileHandler(log_path)
    _fh.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    _logger.addHandler(_fh)
    _logger.setLevel(logging.INFO)

    print(f"Processing {len(archive_files)} archive(s) with {args.workers} worker(s)\n")
    print(f"Logging to {log_path}\n")
    _logger.info(f"START: processing {len(archive_files)} archive(s) with {args.workers} worker(s)")

    total_start = time.perf_counter()

    futures = {}
    with Progress(
        TextColumn("{task.description}"),
        BarColumn(),
        DownloadColumn(table_column=Column(justify="right", no_wrap=True, min_width=14, max_width=14)),
        TransferSpeedColumn(table_column=Column(justify="right", no_wrap=True, min_width=10, max_width=10)),
        TimeRemainingColumn(table_column=Column(justify="right", no_wrap=True, min_width=8, max_width=8)),
        console=_console,
        refresh_per_second=2,
    ) as progress:
        agg_task_id = progress.add_task("Total", total=0)
        agg_lock = threading.Lock()
        stop_new = threading.Event()
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            for archive_path, display_root in archive_files:
                fut = executor.submit(process_one, archive_path, display_root, dropbox_root, token, progress, agg_task_id, agg_lock, stop_new, not args.no_sort, args.max_retries)
                futures[fut] = archive_path

            try:
                for fut in as_completed(futures):
                    fut.result()  # surface any unhandled worker exceptions
                    if stop_new.is_set():
                        n = sum(1 for f in futures if f.cancel())
                        progress.console.print(
                            "The temporary 4-hour access token has expired.\n"
                            "Generate a new one at: https://www.dropbox.com/developers/apps"
                            + (f"\n{n} pending archive(s) cancelled" if n else ""),
                            markup=False,
                        )
                        _logger.info(
                            "The temporary 4-hour access token has expired."
                            + (f" {n} pending archive(s) cancelled" if n else "")
                        )
                        break
            except KeyboardInterrupt:
                cancelled = sum(1 for f in futures if f.cancel())
                in_progress = sum(1 for f in futures if not f.done() and not f.cancelled())

                progress.stop()

                if in_progress == 0:
                    sys.stderr.write("\n[INTERRUPTED]\n")
                    sys.stderr.flush()
                    executor.shutdown(wait=False, cancel_futures=True)
                    os._exit(130)

                sys.stderr.write(
                    f"\n^C Interrupted — {in_progress} file(s) in progress"
                    + (f", {cancelled} pending cancelled" if cancelled else "")
                    + f". Finish in-progress files? [y/N]: "
                )
                sys.stderr.flush()

                try:
                    answer = sys.stdin.readline().strip().lower()
                except (EOFError, KeyboardInterrupt):
                    sys.stderr.write("\n[INTERRUPTED]\n")
                    sys.stderr.flush()
                    executor.shutdown(wait=False, cancel_futures=True)
                    os._exit(130)

                if answer not in ("y", "yes"):
                    executor.shutdown(wait=False, cancel_futures=True)
                    sys.stderr.write("[INTERRUPTED]\n")
                    os._exit(130)

                sys.stderr.write(
                    f"Finishing {in_progress} in-progress file(s)... (Ctrl+C again to stop immediately)\n"
                )
                sys.stderr.flush()
                progress.start()
                try:
                    for fut in list(futures.keys()):
                        if not fut.cancelled():
                            try:
                                fut.result()
                            except Exception:
                                pass
                except KeyboardInterrupt:
                    executor.shutdown(wait=False, cancel_futures=True)
                    sys.stderr.write("\n[INTERRUPTED]\n")
                    os._exit(130)

    total_elapsed = time.perf_counter() - total_start
    print(f"\nDone in {_fmt_duration(total_elapsed)}")
    _logger.info(f"DONE in {_fmt_duration(total_elapsed)}")


if __name__ == "__main__":
    main()
