"""Build throw-away directory trees with back-dated files for the tests."""
import os
import tempfile
from collections import Counter

import retention_archiver as ra

NOW = 1_800_000_000.0  # fixed "now" so tests never depend on the wall clock
DAY = 86400.0


def make_root():
    tmp = tempfile.TemporaryDirectory()
    root = os.path.join(os.path.realpath(tmp.name), "data", "root")
    os.makedirs(root)
    return tmp, root


def write_file(root, rel, age_days=0, content="x"):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(content)
    stamp = NOW - age_days * DAY
    os.utime(path, (stamp, stamp))
    return path


def write_cfg(root, rel_dir, include, days, recursive=None, exclude=None, name="da.cfg"):
    lines = ["[retention]", f"sourceFileIncludePattern=/{include}/", f"retentionDays={days}"]
    if exclude is not None:
        lines.append(f"sourceFileExcludePattern=/{exclude}/")
    if recursive is not None:
        lines.append(f"recursive={str(recursive).upper()}")
    path = os.path.join(root, rel_dir, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write("\n".join(lines) + "\n")


def eligible(root):
    """Relative paths the scanner marks as eligible."""
    stats = Counter()
    found = ra.scan(root, ra.DEFAULT_CONFIG_NAME, NOW, stats)
    return sorted(os.path.relpath(c.path, root) for c in found), stats
