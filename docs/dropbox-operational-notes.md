# Dropbox Operational Notes

Observed behaviours of the Dropbox CDN and API that affect how this tool operates.

---

## 1. Streaming connections drop after ~60 minutes

Dropbox CDN connections used for streaming TGZ downloads are cut after approximately 60 minutes, regardless of how much data remains. This is not a network error — it is a server-side timeout on long-lived HTTP connections.

**Evidence from logs:**

```
10:56:34 START: processing 77 archive(s) with 6 worker(s)
11:56:34 [ERROR] takeout-20251013T105805Z-1-002.tgz — ProtocolError: IncompleteRead (attempt 0/50, downloaded 41.6 GB)
11:56:34 [ERROR] takeout-20260213T105755Z-6-002.tgz — ProtocolError: IncompleteRead (attempt 0/50, downloaded 38.2 GB)
11:56:34 [ERROR] takeout-20251013T105805Z-1-004.tgz — ProtocolError: IncompleteRead (attempt 0/50, downloaded 38.2 GB)
... (6 workers all dropped simultaneously, exactly 60 minutes after start)
```

Six workers all hit the drop at the same second because they all started at the same time. When running multiple workers, expect periodic mass drops at 60-minute intervals.

**Impact:** For a 50 GB archive at ~11 MB/s, a single connection covers ~40 GB in 60 minutes. The checkpoint/resume system picks up from the last checkpoint (~256 MB before the drop) and completes in ~15 additional minutes. See `adr-001-tgz-checkpoint-resume.md`.

**ZIP files are unaffected** — ZIP processing uses HTTP Range requests to fetch only the central directory (~3 requests, a few MB at most), so it completes in seconds and never hits this timeout.

---

## 2. CDN nodes may return 200 instead of 206 on Range requests

When resuming a TGZ download via an HTTP `Range` request, some CDN nodes behind Dropbox temporary links return `200 OK` (full file from byte 0) instead of `206 Partial Content`. Feeding byte-0 data into a mid-stream decompressor produces garbage and causes `invalid header` tar errors.

The tool detects this via the `_RangeNotSupported` exception: if a Range request receives a 200, the checkpoint is discarded and the next attempt restarts from byte 0 rather than producing corrupted output.

This has been observed infrequently. Most resume attempts receive a correct 206.

---

## 3. ZIP probing requires a GET request, not HEAD

When opening a ZIP file, `RangeRequestFile.__init__` needs to confirm the server supports range requests and get the total file size. The obvious approach is a HEAD request, but `requests.head()` defaults to `allow_redirects=False`. Dropbox temporary links redirect to a CDN, so a HEAD returns a `302` with no `Content-Length`, causing an immediate `ValueError` before any range requests are attempted.

The fix is a minimal GET with `Range: bytes=0-0`. This:
- Follows redirects automatically
- Returns a `206` response confirming range support
- Includes `Content-Range: bytes 0-0/<total>` from which the full file size is parsed

This also means the range-support check is a real validation, not just an assumption.

---

## 4. Temporary links expire after 4 hours

Temporary download links obtained via `files/get_temporary_link` expire after 4 hours. If a run exceeds 4 hours (e.g. many large archives with multiple workers), subsequent API calls will receive `401 Unauthorized`.

The tool handles this by setting a stop flag on 401: no new archives are started, in-progress ones are allowed to finish (they hold their links), and pending archives are cancelled. A message is printed directing the user to generate a new token.

**Mitigation:** Use `--workers N` to process more archives in parallel and reduce total wall-clock time. See `docs/oauth-refresh-token.md` for notes on implementing automatic token refresh.
