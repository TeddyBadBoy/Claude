# drivezip

Work with large ZIP archives stored on Google Drive **without downloading them**.

A ZIP keeps its central directory at the *end* of the file, and the Drive API
honours HTTP `Range` requests. Put those two facts together and you can list a
40 GB archive, or pull one file out of the middle of it, while transferring a
few megabytes.

```console
$ drivezip info 1AbCdEf...            # a 40 GiB backup on Drive
archive        nightly-backup.zip
size on drive  40.0 GiB (42949672960 B)
entries        18422 (18119 files, 303 directories)
uncompressed   61.3 GiB
compressed     39.9 GiB
ratio          34.9% saved
[transferred 1.0 MiB of 40.0 GiB (0.00%) in 1 request(s); cache 3 hit / 0 miss]

$ drivezip get 1AbCdEf... 'logs/2026-08-*.json' -d ./out
```

## How it works

`RangeReader` turns any byte-range source into a seekable, read-only file
object with an LRU block cache. Because `zipfile.ZipFile` asks for nothing more
than `seek`/`read`, the standard library parses the remote archive directly —
no reimplementation of the ZIP format, no temporary copy on disk.

```
Drive API  ──Range──▶  DriveRangeSource ──▶ RangeReader ──▶ zipfile.ZipFile
                       (auth, retries)      (block cache,    (stdlib parsing)
                                             read-ahead)
```

Three things keep the byte count down:

- **Tail prefetch** — the last 1 MiB is pulled in a single request, which
  usually contains the whole central directory, so `ls` and `info` cost one
  round trip.
- **Read-ahead** — a cache miss fetches the missing block plus a few
  neighbours, so extracting a member streams in a handful of large requests
  instead of hundreds of small ones.
- **Bounded cache** — memory stays at `block_size × cache_blocks` (32 MiB by
  default) no matter how big the archive or the member is.

## Install

```bash
pip install -e ".[google]"      # runtime + Google auth libraries
pip install -e ".[google,dev]"  # plus pytest and ruff
```

Python 3.11 or newer.

## Authentication

Credentials are resolved in this order, first hit wins:

| Source | How |
| --- | --- |
| Access token | `--access-token …` or `DRIVEZIP_ACCESS_TOKEN=…` |
| Service account | `--service-account key.json` or `GOOGLE_APPLICATION_CREDENTIALS` |
| Cached user token | `~/.config/drivezip/token.json`, refreshed automatically |
| Interactive consent | OAuth client-secrets file, via `drivezip login` |

For the interactive route, create a **Desktop app** OAuth client in the Google
Cloud Console, download the JSON to `~/.config/drivezip/client_secret.json`,
then run:

```bash
drivezip login
```

Only the read-only scope (`drive.readonly`) is ever requested. A service account
must be granted access to the file — share the file with the service account's
email address like you would with any other user. Shared Drives are supported
(`supportsAllDrives` is set on every call).

## Commands

The target is a Drive file id, any Drive share URL, or a path to a local `.zip`
(handy for testing — the same code path serves both).

```bash
drivezip info   <target>                     # size, entry counts, compression ratio
drivezip ls     <target> [glob ...] [-l] [-a] # list members
drivezip cat    <target> <member>            # stream one member to stdout
drivezip get    <target> [glob ...] -d DIR   # extract members to disk
drivezip login                               # cache an OAuth token
```

Useful flags on every archive command:

| Flag | Meaning |
| --- | --- |
| `--stats` | report bytes transferred, requests made, cache hits (to stderr) |
| `--block-size N` | cache block size, default 1 MiB — lower it for many tiny members |
| `--cache-blocks N` | resident blocks, default 32 |
| `--readahead N` | extra blocks per miss, default 3 |
| `--tail N` | bytes prefetched from the end, default 1 MiB — raise it for archives with tens of thousands of entries |
| `--overwrite`, `--flatten` | `get` only |

Examples:

```bash
# What is in there, and how big?
drivezip info 'https://drive.google.com/file/d/1AbCdEf.../view'

# Find one config file among 20 000 entries
drivezip ls 1AbCdEf... '**/settings.yaml' -l

# Read it without writing anything to disk
drivezip cat 1AbCdEf... 'app/config/settings.yaml' | yq .

# Pull a day's worth of logs, flattened into one directory
drivezip get 1AbCdEf... 'logs/2026-08-18/*.json' -d ./today --flatten --stats
```

## Public links — no credentials at all

A Drive file shared with **anyone with the link** can be read over plain HTTP
ranges, skipping OAuth entirely:

```bash
drivezip ls   <file-id-or-url> --public -l
drivezip get  <file-id-or-url> --public 'memory/*' -d ./out --stats
```

The same source works against any endpoint that honours `Range` — a static file
server, an object store, a CDN:

```bash
drivezip ls https://example.com/backups/nightly.zip -l
```

If the server answers a range request with the whole body, `drivezip` raises
`RangeNotSupportedError` instead of quietly downloading everything. If Drive
returns its virus-scan interstitial (which happens when a file is not really
public), the error says so rather than parsing HTML as a ZIP.

## Library use

```python
from drivezip import DriveRangeSource, ZipArchive

source = DriveRangeSource("1AbCdEf...", access_token=token)
with ZipArchive(source) as archive:
    for entry in archive.entries("data/*.parquet"):
        print(entry.name, entry.size, entry.method)

    payload = archive.read("data/manifest.json")  # bytes, in memory
    archive.extract("data/2026-08.parquet", "./out")  # streamed to disk
    print(archive.stats)  # what it actually cost
```

`LocalFileRangeSource` swaps in for a file on disk, `HttpRangeSource` for any
URL that honours `Range`, and anything else implementing `RangeSource` (`name`,
`size`, `fetch(start, end)`, `close()`) works too — S3, a CDN, whatever serves
ranges.

```python
from drivezip import HttpRangeSource, ZipArchive, drive_public_url

source = HttpRangeSource(drive_public_url("1cotRGgt..."))  # link-shared file
with ZipArchive(source) as archive:
    print(archive.summary())
```

## Safety

Extraction refuses members whose paths would escape the destination directory —
absolute paths, `..` traversal, and Windows drive letters are all rejected
before any byte is written ("zip slip"). Members are written to a `.part` file
and renamed on completion, so an interrupted extraction never leaves a truncated
file in place of a good one.

## Development

```bash
ruff check .        # lint
ruff format .       # format
pytest              # 70 tests, no network required
```

The suite exercises the whole stack — including a fake Drive session that serves
ranges out of a real ZIP — and asserts on how many bytes crossed the wire, so a
regression that starts downloading whole archives fails the build.

CI runs the same commands on Python 3.11, 3.12 and 3.13; see
[`.github/workflows/ci.yml`](.github/workflows/ci.yml).

## Limits

- Google-native files (Docs, Sheets, Slides) have no byte size and cannot be
  read this way; the error says so explicitly.
- If Drive ever ignores a `Range` header, the reader raises
  `RangeNotSupportedError` rather than silently downloading everything.
- ZIP64 archives work (the standard library handles them); solid formats such
  as `.tar.gz` cannot be read selectively at all — that is a property of the
  format, not of this tool.

## License

[MIT](LICENSE).
