# ADR 001: TGZ Checkpoint/Resume Architecture

## Status

Accepted

## Context

Dropbox CDN connections drop after approximately 60 minutes. A 50 GB archive streamed at ~11 MB/s takes ~78 minutes, so a naive retry-from-byte-0 approach can never complete — each attempt is cut before it finishes, and the next attempt starts over.

We need a way to resume a partial TGZ download from a known point rather than restarting from scratch.

## Decision

Use `zlib` directly for decompression rather than the `gzip` module, and save a checkpoint at tar entry boundaries.

### Why zlib directly

`zlib.decompressobj` supports `.copy()`, which takes a snapshot of the decompressor's internal state. The `gzip` module wraps zlib but does not expose this. By owning the decompressor directly in `ResumableGzipStream`, we can checkpoint it at any point and restore it later.

### Checkpoint contents (`_GzipCheckpoint`)

Saved every 256 MB of compressed data at tar entry boundaries:

| Field | Purpose |
|---|---|
| `http_pos` | Byte offset in the compressed HTTP stream — used for the Range request on resume |
| `decompressor` | `zlib.decompressobj.copy()` — restores mid-stream decompression state |
| `buf` | Decompressed bytes buffered but not yet consumed by tarfile at save time |
| `bytes_to_skip` | Bytes to drain on restore so tarfile's first read lands on the next header |
| `entries_before` | Number of tar entries already collected before this checkpoint |

### The `bytes_to_skip` field

When a checkpoint is saved after collecting entry N, tarfile has read N's header but not yet consumed N's file data. `_buf` starts with that data. On restore, a fresh `tarfile.open()` expects a header at byte 0 of what we hand it — if we feed it data instead, it raises `invalid header`.

`bytes_to_skip` is set to the padded size of entry N's data (`((size + 511) // 512) * 512`). The `read()` method drains this many bytes before returning anything to tarfile, so tarfile's first read sees the next entry's header.

### The `entries_before` field

On retry the tarfile loop variable `i` starts at 0, but the stream position is mid-archive. `entries_before` records how many entries precede the checkpoint so the skip logic (`if abs_i >= skip`) stays correct across retries.

### `bufsize=tarfile.BLOCKSIZE` (512 bytes)

`tarfile.open(mode="r|")` wraps the fileobj in an internal `_Stream` that reads ahead in `RECORDSIZE` (10 KB) chunks and buffers the excess in `_Stream.buf`. Our checkpoint saves `ResumableGzipStream._buf` but has no visibility into `_Stream.buf`, so on restore those pre-read bytes are silently lost and the stream is misaligned.

Setting `bufsize=tarfile.BLOCKSIZE` forces `_Stream` to read exactly one 512-byte tar block at a time, keeping `_Stream.buf` always empty. All buffering stays in `ResumableGzipStream` where the checkpoint can capture it.

### Fallback: restart from scratch on bad resume

Two cases trigger a full restart rather than a fail:

1. **Server returns 200 instead of 206** on a Range request — CDN nodes behind Dropbox temporary links occasionally ignore `Range` headers and return the full file from byte 0. Feeding byte-0 data into a mid-stream decompressor produces garbage. Detected via `_RangeNotSupported`; checkpoint is discarded and the next attempt starts from the beginning.

2. **`TarError` or `zlib.error` after a checkpoint resume** — treated as a sign the restored stream didn't land at a valid header boundary. Checkpoint discarded, next attempt restarts from scratch. This is a safety net; in practice the `bytes_to_skip` approach prevents this.

### urllib3 exceptions must be caught directly

`ResumableGzipStream` reads from `response.raw` rather than the requests response object. At that level, urllib3 raises its own exceptions (`ProtocolError`, `ReadTimeoutError`, `IncompleteRead`) instead of the requests wrappers (`ChunkedEncodingError`, `ConnectionError`, etc.). These are different exception types and do not inherit from each other.

If only requests exceptions are listed in `_RETRYABLE_ERRORS`, urllib3 errors escape the retry loop entirely and cause an immediate `[FAIL]` with no retries. Both sets must be listed explicitly.

### Connection timeout strategy

The streaming request uses `timeout=(30, 60)`:
- **30s connect timeout** — fail fast if the CDN is unreachable
- **60s read timeout** — fires if no data arrives for 60 seconds, converting a silently stalled connection into a retryable `ReadTimeout`

The read timeout applies to the arrival of each chunk, not the full response, so it does not interfere with legitimately slow large downloads.

### Retry budget

The default retry limit is 50. This is intentionally generous: because checkpoint resumes pick up close to where the stream dropped, each retry costs only minutes rather than hours. In practice, Dropbox drops connections roughly once per hour; even a 500 GB archive would complete well within 50 retries.

## Consequences

- A 50 GB file that drops at 60 min (after ~40 GB) resumes from ~39.75 GB and completes in ~15 more minutes rather than restarting from 0.
- Checkpoints add minimal overhead: `.copy()` on a zlib decompressor is fast, and checkpoints are only taken every 256 MB.
- The `bufsize=BLOCKSIZE` setting means tarfile issues many small reads, but the bottleneck is the network, so this has no measurable impact.
- Retry count is capped at 50 (configurable via `--max-retries`). In practice, Dropbox drops connections roughly every 60 minutes, so a 50-retry budget is more than enough for any realistic archive size.
