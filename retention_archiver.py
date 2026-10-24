#!/usr/bin/env python3
"""retention_archiver - config-driven file retention for very large directory trees.

One script replaces many per-directory cleanup jobs. Each directory may hold a small
``da.cfg`` file that says which files may be removed (or moved to an archive) and when.
Directories without a config inherit from an ancestor that opted in with ``recursive = true``.

Config file ``da.cfg`` (key=value; full-line comments start with '#' or ';'; optional [retention]
header; inline comments are NOT supported because a regex may legitimately contain '#'):

    [retention]
    sourceFileIncludePattern=/\\.csv$/
    sourceFileExcludePattern=/^keep_/
    retentionDays=60
    recursive=TRUE

  sourceFileIncludePattern  regex on the file NAME, REQUIRED: only matching names are ever touched
  sourceFileExcludePattern  regex on the file name, optional/empty: matching names are always protected
  retentionDays             integer >= 1, REQUIRED: eligible when strictly older than this many days
  recursive                 TRUE|FALSE (case-insensitive, default FALSE): sub-directories inherit this config

Regex values may be wrapped in slashes (/regex/); the slashes are optional and stripped.

Inheritance rules (see docs/DESIGN.md):
  1. A directory's own config always applies to its own files.
  2. A config with recursive=true is inherited by every sub-directory without its own config.
  3. A config with recursive=false applies only to its own directory; deeper directories keep
     inheriting from the nearest ancestor that was recursive (if any).
  4. No applicable config -> files are left alone. An INVALID config blocks its whole subtree.

Safety: dry-run by default, explicit --execute, protected/shallow roots refused, per-file recheck
before acting, never follows symlinks, config files are never touched, optional --max-delete
circuit breaker, single-run lock, CSV audit report.

Exit codes: 0 success | 1 completed with errors | 2 usage / safety / configuration refusal.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import shutil
import stat as stat_mod
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterator, Optional, Union

try:  # POSIX only; the lock is skipped elsewhere
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

log = logging.getLogger("retention_archiver")

__version__ = "1.0.0"

DEFAULT_CONFIG_NAME = "da.cfg"
MAX_CONFIG_BYTES = 64 * 1024
SECONDS_PER_DAY = 86400.0
KNOWN_KEYS = {  # lower-case spelling -> canonical spelling (keys are matched case-insensitively)
    "sourcefileincludepattern": "sourceFileIncludePattern",
    "sourcefileexcludepattern": "sourceFileExcludePattern",
    "retentiondays": "retentionDays",
    "recursive": "recursive",
}
PROTECTED_ROOTS = {
    "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib64", "/opt",
    "/proc", "/root", "/run", "/sbin", "/sys", "/tmp", "/usr", "/var",
}


class ConfigError(Exception):
    """A da.cfg file is invalid."""


class SafetyError(Exception):
    """The run was refused by a safety guard."""


# --------------------------------------------------------------------------------------
# Configuration model
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Rule:
    include: "re.Pattern[str]"
    exclude: "Optional[re.Pattern[str]]"
    retention_days: int
    recursive: bool
    source_dir: str

    def describe(self) -> str:
        exclude = self.exclude.pattern if self.exclude else "-"
        return (f"rule from '{self.source_dir}' (include={self.include.pattern!r}, exclude={exclude!r}, "
                f"retentionDays={self.retention_days}, recursive={str(self.recursive).lower()})")


@dataclass(frozen=True)
class Blocked:
    """An invalid config. Everything below it is left alone rather than guessed at."""
    reason: str
    source_dir: str

    def describe(self) -> str:
        return f"INVALID config in '{self.source_dir}' ({self.reason}); subtree blocked"


State = Union[Rule, Blocked, None]


@lru_cache(maxsize=4096)
def _compile(pattern: str, label: str) -> "re.Pattern[str]":
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ConfigError(f"{label}: invalid regex {pattern!r}: {exc}") from exc


def _unwrap_regex(value: str) -> str:
    """Patterns may be written as /regex/ (slashes are optional and stripped)."""
    if len(value) >= 2 and value.startswith("/") and value.endswith("/"):
        return value[1:-1]
    return value


def parse_config(text: str, source_dir: str) -> Rule:
    values: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            if line.lower() != "[retention]":
                raise ConfigError(f"line {lineno}: unknown section {line}")
            continue
        if "=" not in line:
            raise ConfigError(f"line {lineno}: expected key=value")
        key, _, value = line.partition("=")
        canonical = KNOWN_KEYS.get(key.strip().lower())
        if canonical is None:
            raise ConfigError(f"line {lineno}: unknown key '{key.strip()}' (typo?)")
        if canonical in values:
            raise ConfigError(f"line {lineno}: duplicate key '{canonical}'")
        values[canonical] = value.strip()

    include_text = _unwrap_regex(values.get("sourceFileIncludePattern", ""))
    missing = []
    if not include_text:
        missing.append("sourceFileIncludePattern")
    if not values.get("retentionDays"):
        missing.append("retentionDays")
    if missing:
        raise ConfigError(f"missing required key(s): {', '.join(missing)}")
    try:
        days = int(values["retentionDays"])
    except ValueError:
        raise ConfigError(f"retentionDays must be an integer, got {values['retentionDays']!r}") from None
    if days < 1:
        raise ConfigError("retentionDays must be >= 1")
    recursive = values.get("recursive", "FALSE").lower()
    if recursive not in ("true", "false"):
        raise ConfigError(f"recursive must be TRUE or FALSE, got {values['recursive']!r}")

    exclude_text = _unwrap_regex(values.get("sourceFileExcludePattern", ""))
    exclude = _compile(exclude_text, "sourceFileExcludePattern") if exclude_text else None
    return Rule(_compile(include_text, "sourceFileIncludePattern"), exclude, days,
                recursive == "true", source_dir)


def load_rule(cfg_path: str, source_dir: str) -> Union[Rule, Blocked]:
    try:
        if os.stat(cfg_path).st_size > MAX_CONFIG_BYTES:
            raise ConfigError(f"config larger than {MAX_CONFIG_BYTES} bytes")
        with open(cfg_path, "r", encoding="utf-8") as handle:
            return parse_config(handle.read(), source_dir)
    except (ConfigError, OSError, UnicodeDecodeError) as exc:
        return Blocked(str(exc), source_dir)


def next_state(local: State, inherited: State) -> "tuple[State, State]":
    """Return (config applied to this directory's files, config handed down to sub-directories)."""
    if isinstance(inherited, Blocked):
        return inherited, inherited
    if local is None:
        return inherited, inherited
    if isinstance(local, Blocked):
        return local, local
    return local, (local if local.recursive else inherited)


def classify(name: str, rule: State, now: float, get_mtime: Callable[[], float]) -> "tuple[str, Optional[float]]":
    """Decide what to do with one file. The mtime is fetched lazily so that files that cannot match
    never cost a stat() call, which matters on network filesystems."""
    if rule is None:
        return "no_rule", None
    if isinstance(rule, Blocked):
        return "blocked", None
    if not rule.include.search(name):
        return "not_matched", None
    if rule.exclude is not None and rule.exclude.search(name):
        return "excluded", None
    age_days = (now - get_mtime()) / SECONDS_PER_DAY
    return ("eligible" if age_days > rule.retention_days else "too_recent"), age_days


# --------------------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Candidate:
    path: str
    size: int
    mtime: float
    age_days: float
    retention_days: int
    rule_dir: str


def _is_regular(entry: os.DirEntry) -> bool:
    try:
        return entry.is_file(follow_symlinks=False)
    except OSError:
        return False


def scan(root: str, cfg_name: str, now: float, stats: Counter) -> Iterator[Candidate]:
    """Single pass, iterative (no recursion limit), one scandir() per directory."""
    stack: "list[tuple[str, State]]" = [(root, None)]
    while stack:
        directory, inherited = stack.pop()
        try:
            with os.scandir(directory) as it:
                entries = list(it)
        except OSError as exc:
            stats["dirs_unreadable"] += 1
            log.error("cannot read directory %s: %s", directory, exc)
            continue
        stats["dirs_scanned"] += 1

        local: State = None
        for entry in entries:
            if entry.name == cfg_name and _is_regular(entry):
                local = load_rule(entry.path, directory)
                if isinstance(local, Blocked):
                    stats["config_errors"] += 1
                    log.error("%s", local.describe())
                else:
                    stats["configs_loaded"] += 1
                break
        effective, child = next_state(local, inherited)

        for entry in entries:
            try:
                if entry.is_symlink():
                    stats["symlinks_skipped"] += 1
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append((entry.path, child))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    stats["special_files_skipped"] += 1
                    continue
                stats["files_seen"] += 1
                if entry.name == cfg_name:
                    continue  # the config is never a candidate
                holder: list = []

                def get_mtime(entry=entry, holder=holder) -> float:
                    st = entry.stat(follow_symlinks=False)
                    holder.append(st)
                    return st.st_mtime

                verdict, age = classify(entry.name, effective, now, get_mtime)
            except OSError as exc:
                stats["stat_errors"] += 1
                log.error("cannot inspect %s: %s", entry.path, exc)
                continue
            stats[f"files_{verdict}"] += 1
            if verdict == "eligible":
                st = holder[0]
                yield Candidate(entry.path, st.st_size, st.st_mtime, age, effective.retention_days,
                                effective.source_dir)


# --------------------------------------------------------------------------------------
# Safety guards
# --------------------------------------------------------------------------------------
def validate_root(root: str, min_depth: int) -> str:
    real = os.path.realpath(root)
    if not os.path.isdir(real):
        raise SafetyError(f"root is not a directory: {root}")
    if real in PROTECTED_ROOTS:
        raise SafetyError(f"refusing protected root: {real}")
    depth = len(Path(real).parts) - 1
    if depth < min_depth:
        raise SafetyError(f"root {real} is only {depth} level(s) deep; --min-depth is {min_depth}")
    return real


def validate_archive_dir(action: str, archive_dir: Optional[str], root: str) -> Optional[str]:
    if action != "move":
        if archive_dir:
            raise SafetyError("--archive-dir is only valid with --action move")
        return None
    if not archive_dir:
        raise SafetyError("--action move requires --archive-dir")
    real = os.path.realpath(archive_dir)
    if real == root or real.startswith(root + os.sep):
        raise SafetyError("--archive-dir must be outside the scanned root (it would be re-scanned)")
    return real


def acquire_lock(root: str):
    """Prevent two executing runs on the same root. Returns the open handle (keep it alive)."""
    if fcntl is None:
        return None
    digest = hashlib.sha1(root.encode()).hexdigest()[:12]
    handle = open(os.path.join(tempfile.gettempdir(), f"retention_archiver_{digest}.lock"), "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise SafetyError(f"another run is already executing on {root}") from None
    return handle


# --------------------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------------------
def _unique_dest(archive_dir: str, rel_path: str) -> str:
    dest = os.path.join(archive_dir, rel_path)
    candidate, counter = dest, 0
    while os.path.exists(candidate):
        counter += 1
        candidate = f"{dest}.dup{counter}"
    return candidate


def apply_action(cand: Candidate, action: str, root: str, archive_dir: Optional[str]) -> "tuple[str, str]":
    """Re-verify the file right before acting; skip it if anything changed since the scan."""
    try:
        st = os.lstat(cand.path)
    except FileNotFoundError:
        return "vanished", ""
    except OSError as exc:
        return "error", str(exc)
    if not stat_mod.S_ISREG(st.st_mode):
        return "not_regular", ""
    if st.st_mtime != cand.mtime or st.st_size != cand.size:
        return "changed_since_scan", ""
    try:
        if action == "delete":
            os.remove(cand.path)
            return "deleted", ""
        dest = _unique_dest(archive_dir, os.path.relpath(cand.path, root))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(cand.path, dest)
        return "moved", dest
    except OSError as exc:
        return "error", str(exc)


def execute(cands: "list[Candidate]", action: str, root: str, archive_dir: Optional[str],
            workers: int) -> "list[tuple[str, str]]":
    """Deleting is I/O bound (NFS round trips), so threads give a near-linear speed-up."""
    if workers <= 1:
        return [apply_action(c, action, root, archive_dir) for c in cands]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda c: apply_action(c, action, root, archive_dir), cands))


# --------------------------------------------------------------------------------------
# Reporting and explain mode
# --------------------------------------------------------------------------------------
def human_bytes(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024
    return f"{num:.1f} TB"


def write_report(path: str, cands: "list[Candidate]", outcomes: "list[tuple[str, str]]", planned: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp_utc", "status", "path", "size_bytes", "age_days",
                         "retention_days", "rule_dir", "detail"])
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for i, cand in enumerate(cands):
            status, detail = outcomes[i] if outcomes else (planned, "")
            writer.writerow([stamp, status, cand.path, cand.size, f"{cand.age_days:.2f}",
                             cand.retention_days, cand.rule_dir, detail])


def explain(root: str, target: str, cfg_name: str, now: float) -> "list[str]":
    """Show exactly which config applies to a directory or file, and why."""
    target_real = os.path.realpath(target)
    if target_real != root and not target_real.startswith(root + os.sep):
        raise SafetyError("--explain target must be inside the root")
    is_file = os.path.isfile(target_real)
    directory = os.path.dirname(target_real) if is_file else target_real
    rel = os.path.relpath(directory, root)
    chain = [root]
    if rel != ".":
        for part in rel.split(os.sep):
            chain.append(os.path.join(chain[-1], part))

    lines, inherited, effective = [], None, None
    for current in chain:
        cfg_path = os.path.join(current, cfg_name)
        local: State = None
        if os.path.isfile(cfg_path) and not os.path.islink(cfg_path):
            local = load_rule(cfg_path, current)
        effective, child = next_state(local, inherited)
        label = os.path.relpath(current, root)
        own = "no local config" if local is None else f"local config: {local.describe()}"
        lines.append(f"[{label}] {own}")
        lines.append(f"      -> applies here: {effective.describe() if effective else 'nothing (files untouched)'}")
        inherited = child
    if is_file:
        name = os.path.basename(target_real)
        verdict, age = classify(name, effective, now, lambda: os.lstat(target_real).st_mtime)
        suffix = f" (age {age:.1f} days)" if age is not None else ""
        lines.append(f"VERDICT for {name}: {verdict}{suffix}")
    return lines


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def parse_args(argv: "Optional[list[str]]" = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="retention_archiver", description=__doc__.split("\n\n")[0])
    p.add_argument("root", help="directory tree to process")
    p.add_argument("--config-name", default=DEFAULT_CONFIG_NAME,
                   help=f"config file name (default {DEFAULT_CONFIG_NAME})")
    p.add_argument("--execute", action="store_true", help="actually delete/move (default is a dry run)")
    p.add_argument("--action", choices=["delete", "move"], default="delete")
    p.add_argument("--archive-dir", help="destination for --action move (must be outside ROOT)")
    p.add_argument("--max-delete", type=int, default=0,
                   help="abort if more files than this are eligible (0 = no limit)")
    p.add_argument("--workers", type=int, default=1, help="parallel workers for delete/move")
    p.add_argument("--min-depth", type=int, default=2, help="refuse roots shallower than this many path levels")
    p.add_argument("--report", help="write a CSV audit report here")
    p.add_argument("--summary-json", help="write the run summary as JSON here")
    p.add_argument("--explain", metavar="PATH", help="explain which config applies to PATH, then exit")
    p.add_argument("--now", help="override 'now' (ISO date-time, UTC) for reproducible runs and tests")
    p.add_argument("--strict", action="store_true", help="exit 1 if any config was invalid or directory unreadable")
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--version", action="version", version=__version__)
    return p.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    started = time.monotonic()
    root = validate_root(args.root, args.min_depth)
    archive_dir = validate_archive_dir(args.action, args.archive_dir, root)
    if args.workers < 1:
        raise SafetyError("--workers must be >= 1")
    if args.now:
        moment = datetime.fromisoformat(args.now)
        now = (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).timestamp()
    else:
        now = time.time()

    if args.explain:
        print("\n".join(explain(root, args.explain, args.config_name, now)))
        return 0

    lock = acquire_lock(root) if args.execute else None  # noqa: F841 - held until the run ends
    mode = "EXECUTE" if args.execute else "DRY RUN"
    log.info("starting %s on %s (action=%s)", mode, root, args.action)

    stats: Counter = Counter()
    cands = list(scan(root, args.config_name, now, stats))
    eligible_bytes = sum(c.size for c in cands)
    log.info("scan complete: %d eligible files (%s)", len(cands), human_bytes(eligible_bytes))

    if args.max_delete and len(cands) > args.max_delete:
        message = f"{len(cands)} eligible files exceed --max-delete {args.max_delete}"
        if args.execute:
            raise SafetyError(f"refusing to proceed: {message}")
        log.warning("%s (a real run would be refused)", message)

    outcomes: "list[tuple[str, str]]" = []
    if args.execute and cands:
        outcomes = execute(cands, args.action, root, archive_dir, args.workers)
    results = Counter(status for status, _ in outcomes)

    planned = "would_delete" if args.action == "delete" else "would_move"
    if args.report:
        write_report(args.report, cands, outcomes, planned)

    summary = {
        "mode": mode, "root": root, "action": args.action, "eligible_files": len(cands),
        "eligible_bytes": eligible_bytes, "stats": dict(stats), "results": dict(results),
        "duration_seconds": round(time.monotonic() - started, 2),
    }
    if args.summary_json:
        with open(args.summary_json, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2)

    print(f"\n=== retention_archiver summary ({mode}) ===")
    for key in sorted(stats):
        print(f"  {key:<24} {stats[key]}")
    print(f"  {'eligible_files':<24} {len(cands)}  ({human_bytes(eligible_bytes)})")
    for key in sorted(results):
        print(f"  {'result_' + key:<24} {results[key]}")
    print(f"  {'duration_seconds':<24} {summary['duration_seconds']}")

    failed = results.get("error", 0) > 0
    degraded = args.strict and (stats["config_errors"] or stats["dirs_unreadable"])
    return 1 if (failed or degraded) else 0


def main(argv: "Optional[list[str]]" = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(message)s")
    try:
        return run(args)
    except (SafetyError, ValueError) as exc:
        log.error("refused: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
