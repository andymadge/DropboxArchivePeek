# DropboxTgzLister

Lists the contents of `.tgz` and `.zip` archives stored in Dropbox without downloading them. Designed for use with Dropbox Smart Sync, where archive files exist as local placeholders but live in the cloud.

- **TGZ**: streams the archive over HTTP and reads only the tar headers, with checkpoint-based resumption on connection drops
- **ZIP**: uses HTTP Range requests to fetch just the central directory index from the end of the file (~3 requests, regardless of archive size)

## Requirements

- Python 3.12+
- `pipenv`
- A Dropbox API token with `files.content.read` permission

## Setup

### 1. Create a Dropbox API app

1. Go to [https://www.dropbox.com/developers/apps](https://www.dropbox.com/developers/apps) and click **Create app**
2. Choose **Scoped access**
3. Choose **Full Dropbox** access
4. Give your app a name (e.g. `DropboxTgzLister`) and click **Create app**
5. On the app's **Permissions** tab, enable `files.content.read`, then click **Submit**

### 2. Generate a temporary access token

On the app's **Settings** tab, scroll to **OAuth 2** and click **Generate** under "Generated access token". Copy the token — it expires after 4 hours.

### 3. Install dependencies

```bash
pipenv install
pipenv shell
```

### 4. Export your token

```bash
export DROPBOX_TOKEN=your_token_here
```

## Usage

```bash
python3 list_dropbox_archives.py <file_or_pattern> [<file_or_pattern> ...]
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--dropbox-root PATH` | `~/Dropbox` | Local Dropbox root directory |
| `--workers N` | `1` | Number of parallel workers |
| `--max-retries N` | `50` | Max retries per archive on connection failure |
| `--no-sort` | — | Write entries in original order instead of sorted |
| `--list` | — | List archive files and sizes without processing |

### Examples

```bash
# Single file
python3 list_dropbox_archives.py ~/Dropbox/Archives/backup.tgz
python3 list_dropbox_archives.py ~/Dropbox/Archives/takeout.zip

# Glob pattern
python3 list_dropbox_archives.py ~/Dropbox/Archives/*.tgz
python3 list_dropbox_archives.py ~/Dropbox/Archives/*.zip

# Mix TGZ and ZIP, 6 parallel workers
python3 list_dropbox_archives.py --workers 6 ~/Dropbox/Takeout/*.tgz ~/Dropbox/Takeout/*.zip

# Entire directory tree, custom Dropbox root
python3 list_dropbox_archives.py --dropbox-root ~/Documents/Dropbox ~/Documents/Dropbox/Archives
```

## Output

For each archive, a `.txt` file is written alongside it with the same base name:

```
backup.tgz        →  backup.txt
takeout.zip       →  takeout.txt
backup.tar.gz     →  backup.txt
```

The `.txt` file contains one entry per line, sorted alphabetically. If a `.txt` file already exists for an archive, it is skipped.

A timestamped log file (`dropbox_lister_YYYYMMDD_HHMMSS.log`) is written to the current directory.

## Status output

```
[ OK ] backup-2025-01.tgz — 1423 entries | 4,820.3 MB | 12m04s → backup-2025-01.txt
[SKIP] backup-2025-02.zip — listing already exists (backup-2025-02.txt)
[FAIL] backup-2025-03.tgz — tar error: invalid header (5m12s)
[RETRY] backup-2025-04.tgz — connection dropped after 1502 entries, resuming from 12.3 GB (attempt 1/50)...
```

## Resumption on connection drops

TGZ processing saves checkpoints every 256 MB of compressed data. If the connection drops, the stream resumes from the last checkpoint using an HTTP Range request — avoiding a full re-download from byte 0. Up to 50 retries are attempted per file (configurable with `--max-retries`).
