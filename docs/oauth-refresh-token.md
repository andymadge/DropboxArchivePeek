# OAuth 2 Offline Access (Avoid 4-Hour Token Expiry)

The script currently requires a manually generated 4-hour access token from the Dropbox developer console. When it expires mid-run, all in-progress workers stop and pending archives are cancelled.

Dropbox supports OAuth 2 **offline access**, which issues a long-lived refresh token alongside a short-lived access token. The refresh token never expires (unless revoked) and can be exchanged for a new access token automatically.

---

## Approach

Keep the existing `requests`-based API calls (no SDK required). Add:
1. A one-time setup script to perform the OAuth flow and capture the refresh token
2. Token refresh logic in the main script — exchange refresh token for access token at startup, and re-refresh on 401

### Credentials needed

Three values, stored as env vars:
- `DROPBOX_APP_KEY` — the app's key
- `DROPBOX_APP_SECRET` — the app's secret
- `DROPBOX_REFRESH_TOKEN` — obtained once via the setup script

The existing `DROPBOX_TOKEN` env var continues to work unchanged for users who prefer the manual approach.

---

## Implementation

### `setup_auth.py` (new file)

A one-time CLI to walk through the OAuth flow and print the refresh token:

1. Print the authorisation URL:
   ```
   https://www.dropbox.com/oauth2/authorize?client_id=APP_KEY&token_access_type=offline&response_type=code
   ```
2. Prompt the user to paste the authorisation code
3. POST to `https://api.dropbox.com/oauth2/token` with `grant_type=authorization_code`
4. Print the resulting `refresh_token` and instructions for storing it

No dependencies beyond `requests` (already in Pipfile).

### `peek_dropbox_archives.py` changes

**New helper (~5 lines):**
```python
def get_access_token(app_key: str, app_secret: str, refresh_token: str) -> str:
    resp = requests.post(
        "https://api.dropbox.com/oauth2/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        auth=(app_key, app_secret),
    )
    resp.raise_for_status()
    return resp.json()["access_token"]
```

**`main()` token resolution (~10 lines added):**
- If `DROPBOX_APP_KEY` + `DROPBOX_APP_SECRET` + `DROPBOX_REFRESH_TOKEN` are all set → call `get_access_token()` at startup; use result as `token`
- Else if `DROPBOX_TOKEN` is set → use it as before
- Else → print error and exit

**Token refresh on 401:**

The existing 401 handler in `process_one` sets `stop_new` and cancels everything. Instead:
- If refresh credentials are available, call `get_access_token()` and update the shared token; do NOT set `stop_new`
- Retry the failed request once with the new token
- Only fall back to stopping if refresh itself fails

**Token sharing across workers:**

Add a mutable container (e.g. a one-element list `[token]`) + `threading.Lock` passed into `process_one`. On 401 with refresh credentials, acquire the lock, refresh if the token hasn't already been refreshed by another worker, release.

---

## Verification

1. Run `python3 setup_auth.py` and confirm it prints a refresh token
2. Set `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`, `DROPBOX_REFRESH_TOKEN` and unset `DROPBOX_TOKEN`
3. Run the lister — confirm it starts without error
4. Confirm existing `DROPBOX_TOKEN` path still works (backward compat)
5. To test 401 refresh: use a revoked/expired access token alongside valid refresh credentials; confirm the run continues rather than stopping
