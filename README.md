# DropboxArchivePeek

Lists the contents of `.tgz` and `.zip` archives stored in Dropbox without downloading them. Designed for use with Dropbox Smart Sync, where archive files exist as local placeholders but live in the cloud.

- **TGZ**: streams the archive over HTTP and reads only the tar headers, with checkpoint-based resumption on connection drops
- **ZIP**: uses HTTP Range requests to fetch just the central directory index from the end of the file (~3 requests, regardless of archive size)

## Motivation

[Google Takeout](https://takeout.google.com) lets you export all your Google data as a set of archives. One of the destination options is Dropbox — Takeout uploads the archives directly to a folder in your Dropbox. The problem is that the filenames are completely opaque: just a timestamp and an incrementing sequence number (e.g. `takeout-20250101T120000Z-001.zip`). There's no way to know which archive contains what without downloading and extracting each one. Some archives can be 50 GB or more, so inspecting them all would require enormous amounts of local disk space.

This tool solves that problem. Because the archive files are already in Dropbox, it can fetch their contents over HTTP using the Dropbox API and write a plain-text index alongside each archive — no local copy of the archive is ever saved to disk. For ZIP files, only a small portion at the end of the file (the central directory) needs to be fetched at all. For TGZ files, the entire archive still needs to be streamed to read the headers, so large files will take time and consume bandwidth, but again nothing is written to disk.

While the Google Takeout case is the primary motivation, the tool works for any `.tgz` or `.zip` files stored in Dropbox.

## Requirements

- Python 3.12+
- `pipenv`
- A Dropbox API token with `files.content.read` permission

## Setup

### 1. Create a Dropbox API app

1. Go to [https://www.dropbox.com/developers/apps](https://www.dropbox.com/developers/apps) and click **Create app**
2. Choose **Scoped access**
3. Choose **Full Dropbox** access
4. Give your app a name (e.g. `DropboxArchivePeek`) and click **Create app**
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
python3 peek_dropbox_archives.py <file_or_pattern> [<file_or_pattern> ...]
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--dropbox-root PATH` | `~/Dropbox` | Local Dropbox root directory |
| `--workers N` | `1` | Number of parallel workers |
| `--max-retries N` | `50` | Max retries per archive on connection failure |
| `--no-sort` | — | Write entries in original order instead of sorted |
| `--no-summary` | — | Skip writing `_summary.txt` files |
| `--list` | — | List archive files and sizes without processing |

### Examples

Only `.tgz` and `.zip` files are processed — other files in a path or glob are ignored automatically.

```bash
# Single file
python3 peek_dropbox_archives.py ~/Dropbox/Archives/backup.tgz
python3 peek_dropbox_archives.py ~/Dropbox/Archives/takeout.zip

# Glob pattern (only matching archive type is processed)
python3 peek_dropbox_archives.py ~/Dropbox/Archives/*.tgz
python3 peek_dropbox_archives.py ~/Dropbox/Archives/*.zip

# Mix TGZ and ZIP in one directory — pass the directory directly, 6 parallel workers
python3 peek_dropbox_archives.py --workers 6 ~/Dropbox/Takeout

# Multiple separate paths (directories, globs, or files)
python3 peek_dropbox_archives.py ~/Dropbox/Takeout ~/Dropbox/Backups
python3 peek_dropbox_archives.py ~/Dropbox/Takeout ~/Dropbox/Backups/latest.tgz
python3 peek_dropbox_archives.py ~/Dropbox/Takeout/*.tgz ~/Dropbox/Backups/*.zip

# Entire directory tree, custom Dropbox root
python3 peek_dropbox_archives.py --dropbox-root ~/Documents/Dropbox ~/Documents/Dropbox/Archives
```

## Output

For each archive, a `.txt` file is written alongside it with the same base name:

```
backup.tgz        →  backup.txt
takeout.zip       →  takeout.txt
backup.tar.gz     →  backup.txt
```

The `.txt` file contains one entry per line, sorted alphabetically. If a `.txt` file already exists for an archive, it is skipped.

### Summary files

A `_summary.txt` file is written alongside each archive immediately after its listing is generated, containing the unique second-level directories from the listing — one per line, sorted alphabetically:

```
takeout-20250101T120000Z-001.zip   →  takeout-20250101T120000Z-001.txt
                                      takeout-20250101T120000Z-001_summary.txt
```

This is particularly useful for Google Takeout archives, where second-level directories correspond to Google products (e.g. `Google Photos`, `Drive`, `Gmail`). Glancing at the summary lets you decide whether you need to keep or download an archive without opening it.

If a summary is missing for an archive that already has a listing (e.g. from a previous run), it will be generated automatically. Use `--no-summary` to disable summary generation entirely — useful if your archives don't follow a two-level directory structure.

A timestamped log file (`logs/dropbox_peek_YYYYMMDD_HHMMSS.log`) is written to a `logs/` subdirectory of the current directory (created automatically).

## Status output

```
[ OK ] backup-2025-01.tgz — 1423 entries | 4,820.3 MB | 12m04s → backup-2025-01.txt
[SKIP] backup-2025-02.zip — listing already exists (backup-2025-02.txt)
[FAIL] backup-2025-03.tgz — tar error: invalid header (5m12s)
[RETRY] backup-2025-04.tgz — connection dropped after 1502 entries, resuming from 12.3 GB (attempt 1/50)...
[SUMM] backup-2025-01.tgz → backup-2025-01_summary.txt
```

## Resumption on connection drops

TGZ processing saves checkpoints every 256 MB of compressed data. If the connection drops, the stream resumes from the last checkpoint using an HTTP Range request — avoiding a full re-download from byte 0. Up to 50 retries are attempted per file (configurable with `--max-retries`).
