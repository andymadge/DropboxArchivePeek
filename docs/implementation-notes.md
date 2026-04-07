# Implementation Notes

Miscellaneous non-obvious implementation details.

---

## Ctrl-C handling with ThreadPoolExecutor

Handling `KeyboardInterrupt` correctly with `ThreadPoolExecutor` requires care. The naive approach — catching the interrupt outside the executor's `with` block — doesn't work:

```python
# WRONG
with ThreadPoolExecutor(...) as executor:
    ...
# KeyboardInterrupt caught here — too late
```

When the `with` block exits (even via exception), `ThreadPoolExecutor.__exit__` calls `shutdown(wait=True)`, which blocks until every running thread finishes. A first Ctrl-C therefore hangs until all in-progress downloads complete. A second Ctrl-C then hits Python's atexit thread-join handler and produces a traceback.

**Fix:** catch `KeyboardInterrupt` *inside* the executor block so `shutdown()` hasn't been called yet, then call it explicitly with `wait=False, cancel_futures=True`, and finally `os._exit(130)` to bypass the atexit handler entirely:

```python
with ThreadPoolExecutor(...) as executor:
    try:
        ...
    except KeyboardInterrupt:
        executor.shutdown(wait=False, cancel_futures=True)
        os._exit(130)
```

`os._exit()` skips all cleanup (atexit, `__del__`, etc.) and exits immediately. Exit code 130 is the POSIX convention for termination by SIGINT.

The tool adds a refinement: on the first Ctrl-C it asks whether to finish in-progress files before exiting, only calling `os._exit()` if the user declines or hits Ctrl-C a second time.
