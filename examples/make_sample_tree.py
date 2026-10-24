#!/usr/bin/env python3
"""Create a synthetic demo tree (back-dated file times) to try retention_archiver safely.

    python examples/make_sample_tree.py                # creates ./sandbox/warehouse
    python retention_archiver.py sandbox/warehouse --report plan.csv
"""
import os
import shutil
import sys
import time

DAY = 86400
ROOT = sys.argv[1] if len(sys.argv) > 1 else os.path.join("sandbox", "warehouse")

CONFIGS = {
    "sales": "[retention]\nsourceFileIncludePattern=/\\.csv$/\nretentionDays=30\nrecursive=TRUE\n",
    "app": "[retention]\nsourceFileIncludePattern=/\\.log$/\nretentionDays=60\nrecursive=TRUE\n",
    "app/audit": "[retention]\nsourceFileIncludePattern=/\\.txt$/\nretentionDays=15\nrecursive=FALSE\n",
    "staging": ("[retention]\nsourceFileIncludePattern=/.*/\nsourceFileExcludePattern=/^keep_/\n"
                "retentionDays=7\nrecursive=TRUE\n"),
    "broken": "[retention]\nsourceFileIncludePattern=/(/\nretentionDays=1\n",   # deliberately invalid regex
}

# relative path -> age in days
FILES = {
    "sales/2026/q1/jan.csv": 45, "sales/2026/q1/feb.csv": 35, "sales/2026/q1/mar.csv": 10,
    "sales/2026/q1/notes.txt": 400, "sales/regional/emea/emea.csv": 31,
    "app/service.log": 70, "app/data/backup.log": 65, "app/audit/trace.txt": 20,
    "app/audit/error.log": 30, "app/audit/old/session.txt": 25, "app/audit/old/system.log": 80,
    "staging/tmp1.dat": 10, "staging/keep_lookup.dat": 500, "staging/batch/run1.dat": 8,
    "landing/raw.csv": 900,        # no config anywhere above -> never touched
    "broken/old.csv": 900,         # invalid config -> whole subtree blocked
}

if os.path.isdir(ROOT):
    shutil.rmtree(ROOT)
now = time.time()
for rel, text in CONFIGS.items():
    path = os.path.join(ROOT, rel, "da.cfg")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(text)
for rel, age in FILES.items():
    path = os.path.join(ROOT, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write("demo data\n")
    stamp = now - age * DAY
    os.utime(path, (stamp, stamp))
print(f"created {len(FILES)} files and {len(CONFIGS)} configs under {ROOT}")
