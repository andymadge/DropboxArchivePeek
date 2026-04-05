# DropboxTgzLister

Lists the contents of `.tgz` and `.zip` archives stored in Dropbox without downloading them. Designed for use with Dropbox Smart Sync, where archive files exist as local placeholders but live in the cloud.

- **TGZ**: streams the archive and reads only the tar headers
- **ZIP**: uses HTTP Range requests to fetch just the central directory index from the end of the file (~3 requests, regardless of archive size)

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
python3 list_dropbox_archives.py ~/Dropbox/Archives/takeout.zip

# Glob pattern
python3 list_dropbox_archives.py ~/Dropbox/Archives/*.tgz
python3 list_dropbox_archives.py ~/Dropbox/Archives/*.zip

# Mix TGZ and ZIP
python3 list_dropbox_archives.py ~/Dropbox/Takeout/*.tgz ~/Dropbox/Takeout/*.zip

# If your Dropbox folder is not ~/Dropbox
python3 list_dropbox_archives.py --dropbox-root ~/Documents/Dropbox *.tgz *.zip
```

```bash
pipenv run python3 list_dropbox_archives.py \
  '~/Dropbox/Apps/Google Download Your Data/2025/2025-10/takeout-20251013T105805Z-001.tgz' \
  '~/Dropbox/Apps/Google Download Your Data/2025/2025-12/takeout-20251213T110147Z-001.tgz' \
  '~/Dropbox/Apps/Google Download Your Data/2025/2025-11/takeout-20231114T163220Z-001.zip' \
  '~/Dropbox/Apps/Google Download Your Data/2025/2025-10/takeout-20251013T105805Z-2-001.tgz' \
  '~/Dropbox/Apps/Google Download Your Data/2025/2025-10/takeout-20251013T105805Z-1-001.tgz' \
  '~/Dropbox/Apps/Google Download Your Data/2025/2025-12/takeout-20251213T110147Z-3-001.tgz'

# delete the created listing files
rm \
  ~'/Dropbox/Apps/Google Download Your Data/2025/2025-10/takeout-20251013T105805Z-001.txt' \
  ~'/Dropbox/Apps/Google Download Your Data/2025/2025-12/takeout-20251213T110147Z-001.txt' \
  ~'/Dropbox/Apps/Google Download Your Data/2025/2025-11/takeout-20231114T163220Z-001.txt' \
  ~'/Dropbox/Apps/Google Download Your Data/2025/2025-10/takeout-20251013T105805Z-2-001.txt' \
  ~'/Dropbox/Apps/Google Download Your Data/2025/2025-10/takeout-20251013T105805Z-1-001.txt' \
  ~'/Dropbox/Apps/Google Download Your Data/2025/2025-12/takeout-20251213T110147Z-3-001.txt'
```

## Output

For each archive, a `.txt` file is written alongside it with the same base name:

```
backup.tgz        →  backup.txt
takeout.zip       →  takeout.txt
backup.tar.gz     →  backup.txt
```

The `.txt` file contains one entry per line. If a `.txt` file already exists for an archive, it is skipped.

## Status output

```
Processing 3 archive(s)

[ OK ] backup-2025-01.tgz — 1423 entries → /Users/andym/Dropbox/Archives/backup-2025-01.txt
[SKIP] backup-2025-02.zip — listing already exists (...)
[FAIL] backup-2025-03.tgz — HTTP 409: ...
```
