# DropboxTgzLister

Lists the contents of `.tgz` archives stored in Dropbox without downloading them, using the Dropbox API to stream just the tar headers. Designed for use with Dropbox Smart Sync, where archive files exist as local placeholders but live in the cloud.

## Requirements

- Python 3.12+
- `pipenv`
- A Dropbox API token with `files.content.read` permission

## Setup

Install dependencies and enter the virtual environment:

```bash
pipenv install
pipenv shell
```

Export your Dropbox token:

```bash
export DROPBOX_TOKEN=your_token_here
```

## Usage

```bash
python3 list_dropbox_archives.py <file_or_pattern> [<file_or_pattern> ...]
```

### Examples

```bash
# Single file
python3 list_dropbox_archives.py ~/Dropbox/Archives/backup.tgz

# Glob pattern
python3 list_dropbox_archives.py ~/Dropbox/Archives/*.tgz

# Multiple patterns
python3 list_dropbox_archives.py ~/Dropbox/2024/*.tgz ~/Dropbox/2025/*.tgz

# If your Dropbox folder is not ~/Dropbox
python3 list_dropbox_archives.py --dropbox-root ~/Documents/Dropbox *.tgz
```

## Output

For each archive, a `.txt` file is written alongside it with the same base name:

```
backup.tgz  →  backup.txt
```

The `.txt` file contains one entry per line, matching `tar -tzf` output. If a `.txt` file already exists for an archive, it is skipped.

## Status output

```
Processing 3 archive(s)

[ OK ] backup-2025-01.tgz — 1423 entries → /Users/andym/Dropbox/Archives/backup-2025-01.txt
[SKIP] backup-2025-02.tgz — listing already exists (...)
[FAIL] backup-2025-03.tgz — HTTP 409: ...
```
