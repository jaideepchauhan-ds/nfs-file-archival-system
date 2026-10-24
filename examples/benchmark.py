#!/usr/bin/env python3
"""Rough scan benchmark on YOUR filesystem. Use a scratch directory, not production.

    python examples/benchmark.py /scratch/bench 2000 25     # 2000 directories x 25 files
"""
import os
import shutil
import subprocess
import sys
import time

root = sys.argv[1] if len(sys.argv) > 1 else "bench_tree"
dirs = int(sys.argv[2]) if len(sys.argv) > 2 else 1000
per_dir = int(sys.argv[3]) if len(sys.argv) > 3 else 20

if os.path.isdir(root):
    shutil.rmtree(root)
os.makedirs(root)
with open(os.path.join(root, "da.cfg"), "w") as handle:
    handle.write("sourceFileIncludePattern=/\\.dat$/\nretentionDays=30\nrecursive=TRUE\n")

old = time.time() - 90 * 86400
for d in range(dirs):
    leaf = os.path.join(root, f"g{d % 50}", f"s{d}")
    os.makedirs(leaf)
    for f in range(per_dir):
        name = f"f{f}.dat" if f % 2 == 0 else f"f{f}.txt"   # half match the pattern
        path = os.path.join(leaf, name)
        open(path, "w").close()
        os.utime(path, (old, old))
total = dirs * per_dir
print(f"built {dirs} directories / {total} files")

script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "retention_archiver.py")
for label, extra in (("dry run", []), ("execute, 1 worker", ["--execute"]),):
    started = time.monotonic()
    subprocess.run([sys.executable, script, root, "--min-depth", "1", *extra],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"{label:<20} {time.monotonic() - started:6.2f}s")
shutil.rmtree(root)
