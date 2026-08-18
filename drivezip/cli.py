"""Command line interface for drivezip."""

from __future__ import annotations

import argparse
import fnmatch
import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .archive import DEFAULT_TAIL_PREFETCH, Entry, ZipArchive
from .errors import DriveZipError
from .ranges import LocalFileRangeSource, RangeSource
from .reader import DEFAULT_BLOCK_SIZE, DEFAULT_MAX_CACHED_BLOCKS, DEFAULT_READAHEAD_BLOCKS

UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def human(nbytes: float) -> str:
    """Format a byte count the way `ls -h` would."""
    value = float(nbytes)
    for unit in UNITS:
        if abs(value) < 1024 or unit == UNITS[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} {UNITS[-1]}"  # pragma: no cover - unreachable


# -- source construction ---------------------------------------------------


def open_source(target: str, args: argparse.Namespace) -> RangeSource:
    """Turn a CLI target into a range source: a local path, a URL, or a Drive reference."""
    if target.startswith("local:"):
        return LocalFileRangeSource(target[len("local:") :])
    if os.path.sep in target or target.endswith(".zip"):
        candidate = Path(target)
        if candidate.is_file():
            return LocalFileRangeSource(candidate)

    # A file shared with "anyone with the link" can be read over plain HTTP
    # ranges, with no credentials involved at all.
    if getattr(args, "public", False):
        from .drive import parse_file_id
        from .http import HttpRangeSource, drive_public_url

        return HttpRangeSource(drive_public_url(parse_file_id(target)))

    if target.startswith(("http://", "https://")) and "drive.google.com" not in target:
        from .http import HttpRangeSource

        return HttpRangeSource(target)

    from .auth import resolve  # imported lazily: local archives need no credentials
    from .drive import DriveRangeSource

    credentials = resolve(
        access_token=getattr(args, "access_token", None),
        service_account=getattr(args, "service_account", None),
        client_secrets=getattr(args, "client_secrets", None),
        allow_interactive=not getattr(args, "no_interactive", False),
    )
    return DriveRangeSource(
        target,
        credentials=credentials.google_credentials,
        access_token=credentials.token,
    )


def open_archive_from_args(args: argparse.Namespace) -> ZipArchive:
    source = open_source(args.target, args)
    return ZipArchive(
        source,
        block_size=args.block_size,
        max_cached_blocks=args.cache_blocks,
        readahead_blocks=args.readahead,
        tail_prefetch=args.tail,
    )


def select(archive: ZipArchive, patterns: Sequence[str], *, files_only: bool = True) -> list[Entry]:
    """Resolve positional patterns to entries; no patterns means everything."""
    entries = archive.entries()
    if files_only:
        entries = [entry for entry in entries if not entry.is_dir]
    if not patterns:
        return entries

    chosen: dict[str, Entry] = {}
    for pattern in patterns:
        matched = [
            entry
            for entry in entries
            if entry.name == pattern or fnmatch.fnmatch(entry.name, pattern)
        ]
        if not matched:
            raise DriveZipError(f"nothing in the archive matches {pattern!r}")
        for entry in matched:
            chosen[entry.name] = entry
    return list(chosen.values())


def print_stats(archive: ZipArchive, stream: Any = None) -> None:
    # Resolved at call time, not import time, so redirected streams are honoured.
    stream = sys.stderr if stream is None else stream
    stats = archive.stats
    transferred = stats["bytes_fetched"]
    total = stats["archive_size"]
    share = (transferred / total * 100) if total else 0.0
    print(
        f"[transferred {human(transferred)} of {human(total)} ({share:.2f}%) "
        f"in {stats['requests']} request(s); "
        f"cache {stats['cache_hits']} hit / {stats['cache_misses']} miss]",
        file=stream,
    )


# -- commands --------------------------------------------------------------


def cmd_info(args: argparse.Namespace) -> int:
    with open_archive_from_args(args) as archive:
        summary = archive.summary()
        print(f"archive        {summary.name}")
        print(f"size on drive  {human(summary.archive_size)} ({summary.archive_size} B)")
        print(
            f"entries        {summary.entries} ({summary.files} files, "
            f"{summary.directories} directories)"
        )
        print(f"uncompressed   {human(summary.total_size)}")
        print(f"compressed     {human(summary.total_compressed)}")
        print(f"ratio          {summary.ratio * 100:.1f}% saved")
        if args.stats:
            print_stats(archive)
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    with open_archive_from_args(args) as archive:
        entries = select(archive, args.pattern, files_only=not args.all)
        entries.sort(key=lambda entry: entry.name)
        if args.long:
            width = max((len(human(entry.size)) for entry in entries), default=0)
            for entry in entries:
                stamp = entry.modified.strftime("%Y-%m-%d %H:%M") if entry.modified else " " * 16
                print(f"{human(entry.size):>{width}}  {entry.method:<7}  {stamp}  {entry.name}")
        else:
            for entry in entries:
                print(entry.name)
        if args.stats:
            print_stats(archive)
    return 0


def cmd_cat(args: argparse.Namespace) -> int:
    with open_archive_from_args(args) as archive:
        password = args.password.encode() if args.password else None
        with archive.open(args.member, pwd=password) as handle:
            shutil.copyfileobj(handle, sys.stdout.buffer, length=1 << 20)
        sys.stdout.buffer.flush()
        if args.stats:
            print_stats(archive)
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    password = args.password.encode() if args.password else None

    with open_archive_from_args(args) as archive:
        entries = select(archive, args.pattern)
        if not entries:
            print("nothing to extract", file=sys.stderr)
            return 1

        planned = sum(entry.compressed_size for entry in entries)
        print(
            f"extracting {len(entries)} member(s), about {human(planned)} to transfer",
            file=sys.stderr,
        )
        for name, path in archive.extract_many(
            [entry.name for entry in entries],
            dest,
            overwrite=args.overwrite,
            flatten=args.flatten,
            pwd=password,
        ):
            print(f"{name} -> {path}")
        if args.stats:
            print_stats(archive)
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    from .auth import login

    path = login(args.client_secrets)
    print(f"token stored in {path}")
    return 0


# -- argument parsing ------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="drivezip",
        description=(
            "Inspect and extract large ZIP archives stored on Google Drive without "
            "downloading the whole file."
        ),
    )
    parser.add_argument("--version", action="version", version=f"drivezip {__version__}")

    auth = argparse.ArgumentParser(add_help=False)
    auth.add_argument("--access-token", help="use this OAuth access token directly")
    auth.add_argument("--service-account", help="path to a service-account key file")
    auth.add_argument("--client-secrets", help="path to an OAuth client-secrets file")
    auth.add_argument(
        "--no-interactive",
        action="store_true",
        help="fail instead of opening a browser for consent",
    )

    tuning = argparse.ArgumentParser(add_help=False)
    tuning.add_argument("target", help="Drive file id, Drive URL, or path to a local .zip")
    tuning.add_argument(
        "--block-size",
        type=int,
        default=DEFAULT_BLOCK_SIZE,
        help=f"cache block size in bytes (default {DEFAULT_BLOCK_SIZE})",
    )
    tuning.add_argument(
        "--cache-blocks",
        type=int,
        default=DEFAULT_MAX_CACHED_BLOCKS,
        help=f"how many blocks stay resident (default {DEFAULT_MAX_CACHED_BLOCKS})",
    )
    tuning.add_argument(
        "--readahead",
        type=int,
        default=DEFAULT_READAHEAD_BLOCKS,
        help=f"extra blocks pulled per miss (default {DEFAULT_READAHEAD_BLOCKS})",
    )
    tuning.add_argument(
        "--tail",
        type=int,
        default=DEFAULT_TAIL_PREFETCH,
        help=f"bytes prefetched from the end of the archive (default {DEFAULT_TAIL_PREFETCH})",
    )
    tuning.add_argument(
        "--stats", action="store_true", help="report how many bytes crossed the wire"
    )
    tuning.add_argument(
        "--public",
        action="store_true",
        help="read a link-shared Drive file over plain HTTP ranges, without credentials",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    info = sub.add_parser("info", parents=[tuning, auth], help="summarise an archive")
    info.set_defaults(func=cmd_info)

    ls = sub.add_parser("ls", parents=[tuning, auth], help="list archive members")
    ls.add_argument("pattern", nargs="*", help="optional glob(s), e.g. 'logs/*.json'")
    ls.add_argument("-l", "--long", action="store_true", help="show size, method and timestamp")
    ls.add_argument("-a", "--all", action="store_true", help="include directory entries")
    ls.set_defaults(func=cmd_ls)

    cat = sub.add_parser("cat", parents=[tuning, auth], help="stream one member to stdout")
    cat.add_argument("member", help="exact member name")
    cat.add_argument("--password", help="password for an encrypted archive")
    cat.set_defaults(func=cmd_cat)

    get = sub.add_parser("get", parents=[tuning, auth], help="extract members to disk")
    get.add_argument("pattern", nargs="*", help="member names or globs; omit for everything")
    get.add_argument("-d", "--dest", default=".", help="destination directory (default: .)")
    get.add_argument("--overwrite", action="store_true", help="replace existing files")
    get.add_argument("--flatten", action="store_true", help="drop directory prefixes")
    get.add_argument("--password", help="password for an encrypted archive")
    get.set_defaults(func=cmd_get)

    login_cmd = sub.add_parser("login", parents=[auth], help="run the OAuth flow and cache a token")
    login_cmd.set_defaults(func=cmd_login)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except DriveZipError as exc:
        print(f"drivezip: {exc}", file=sys.stderr)
        return 2
    except FileExistsError as exc:
        print(f"drivezip: {exc}", file=sys.stderr)
        return 2
    except BrokenPipeError:  # `drivezip cat ... | head` is a normal way to use this
        return 0
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
