# ADR 002: Progress Display Library (rich over tqdm)

## Status

Accepted

## Context

The tool supports parallel processing of multiple archives via `--workers N`. Each worker needs its own progress bar, and status messages (OK, SKIP, FAIL, RETRY) must be printed without corrupting the live display.

## Decision

Use `rich.progress.Progress` rather than `tqdm`.

## Reasons

### tqdm limitations with multiple workers

tqdm assigns each bar a fixed `position=` slot. When a bar is removed (file complete), the slot is freed but the terminal line remains, leaving a blank gap between remaining bars. With many workers completing at different times, the display fills with blank lines.

Concurrent `tqdm.write()` calls from multiple threads also cause cursor artifacts — split lines and interleaved output — because tqdm does not serialise writes across threads.

### Why rich solves both

`rich.progress.Progress` manages bar positions dynamically. When a task is removed, the display collapses cleanly with no blank line. All output goes through a single live display that serialises console access, so `progress.console.print()` from any thread is safe and renders without artifacts.

## Progress rate display tuning

At high throughput (~18 MB/s), calling `progress.advance()` on every decompressed chunk saturates rich's internal 1000-sample speed-estimate deque in under 2 seconds. Rich trims to the last 30 seconds of samples, so when the deque fills in 2 seconds, the speed estimate window collapses from 30 seconds to 2 seconds, producing a noisy, erratic rate display.

**Fix:** batch advances in `ResumableGzipStream` and flush every 0.25 seconds (via `_FLUSH_INTERVAL`). At 4 calls/second, the deque holds ~250 seconds of history, well beyond rich's 30-second trim window. The display refresh rate is also set to 2/second (`refresh_per_second=2`) for a calm, readable output.

## Consequences

- `rich` is a required dependency; it is listed in the `Pipfile`
- `tqdm` was an earlier dependency and has been removed
- All console output must go through `progress.console.print()` while the progress display is live, to avoid cursor corruption
