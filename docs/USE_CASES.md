# Use cases

Every tree below is covered by the test suite or the demo generator, so the results are what the tool really does.
Notation: `✓` = eligible for cleanup, `✗` = kept. Ages are in days. Eligible means *strictly older* than `retentionDays`.

---

## UC1: One config governs a whole tree (recursive)

```
reports/                       da.cfg  (csv, 30 days, recursive=true)
├── q1.csv            40  ✓
├── summary.txt      400  ✗    name does not match \.csv$
└── 2026/                      (inherits)
    ├── jan.csv       35  ✓
    ├── feb.csv       20  ✗    too recent
    └── archive/deep/
        └── old.csv   50  ✓    inherited three levels down
```
**Result:** `q1.csv`, `2026/jan.csv`, `2026/archive/deep/old.csv`.
**Why it matters:** one small file replaces a cleanup job per folder, however deep the tree grows.

---

## UC2: A sub-tree overrides the parent

```
platform/                      da.cfg  (log, 60 days, recursive=true)
├── app.log           70  ✓
├── debug/                     da.cfg  (log, 5 days, recursive=true)
│   ├── trace.log      8  ✓
│   └── nested/trace2.log  6  ✓    inherits the 5-day override
└── data/                      (inherits platform: 60 days)
    ├── backup.log    65  ✓
    └── fresh.log     30  ✗
```
**Result:** `app.log`, `debug/trace.log`, `debug/nested/trace2.log`, `data/backup.log`.
**Why it matters:** noisy areas can be cleaned aggressively without touching the global policy.

---

## UC3: A non-recursive config applies to one directory only

```
service/                       da.cfg  (log, 60 days, recursive=true)
├── run.log           70  ✓
└── audit/                     da.cfg  (txt, 15 days, recursive=false)
    ├── notes.txt     20  ✓    local rule
    ├── error.log     30  ✗    local rule only matches .txt
    └── history/                (local config is not handed down, ancestor resumes)
        ├── old.txt   25  ✗    ancestor rule only matches .log
        └── system.log 80 ✓    ancestor rule: 80 > 60
```
**Result:** `run.log`, `audit/notes.txt`, `audit/history/system.log`.
**Why it matters:** a special-case folder does not hide the general policy from its own children.

---

## UC4: Sibling modules with different policies

```
platform/                      da.cfg  (any file, 120 days, recursive=true)
├── handbook.pdf     150  ✓
├── billing/                   da.cfg  (json, 14 days, recursive=true)
│   ├── invoice1.json 20  ✓
│   ├── readme.md    300  ✗    pattern is json only
│   └── archive/inv0.json 10 ✗ inherits 14-day rule; too recent
├── reporting/                 (no config: inherits 120 days)
│   ├── q.parquet    130  ✓
│   └── scratch/tmp.parquet 200 ✓
└── ingest/                    da.cfg  (xml, 5 days, recursive=false)
    ├── feed.xml       9  ✓
    └── backup/feed_old.xml 40 ✗   not inherited from ingest; platform's 120 days applies
```
**Result:** `handbook.pdf`, `billing/invoice1.json`, `reporting/q.parquet`, `reporting/scratch/tmp.parquet`, `ingest/feed.xml`.

---

## UC5: No config means no deletion

```
shared/
├── a.csv            999  ✗    no config anywhere above: never touched
└── team/                      da.cfg  (csv, 30 days, recursive=false)
    ├── b.csv         40  ✓
    └── c/d.csv      999  ✗    parent config is not recursive and no ancestor has one
```
**Result:** `team/b.csv`.
**Why it matters:** landing zones and unmanaged folders are safe by default; cleanup is opt-in per directory.

---

## UC6: Protect files with an exclude pattern

```
exports/                       da.cfg  (any, 7 days, exclude ^(keep_|LATEST), recursive=true)
├── keep_ref.dat     999  ✗    excluded
├── LATEST.dat       999  ✗    excluded
├── day1.dat          10  ✓
└── sub/day0.dat       9  ✓
```
**Result:** `day1.dat`, `sub/day0.dat`.
**Why it matters:** reference files and "latest" pointers survive even under an aggressive `.*` rule.

---

## UC7: An invalid config fails safe

```
lake/                          da.cfg  (parquet, 30 days, recursive=true)
├── ok.parquet        50  ✓
└── badzone/                   da.cfg  (typo: "retentionDay")
    ├── old.parquet  500  ✗    subtree blocked
    └── x/old.parquet 500 ✗    still blocked: not silently handed the parent rule
```
**Result:** `ok.parquet`. The run logs an error; with `--strict` it exits 1 so schedulers raise an alert.
**Why it matters:** when intent is unclear, the tool does less, not more.

---

## UC8: Reversible cleanup with `--action move`

```bash
python retention_archiver.py /data/exports --execute --action move --archive-dir /data/_archive/2026-10-24
```
Eligible files move to `/data/_archive/2026-10-24/<same relative path>`; existing archive files are never overwritten (`.dup1`, `.dup2`, ...).
To roll back, copy files back from the archive. After a retention period, delete the archive directory itself.

---

## UC9: "Why wasn't my file deleted?"

```bash
python retention_archiver.py /data/exports --explain /data/exports/2026/jan/report.csv
```
Prints each directory from the root down to the file, the local config (if any), the config that applies, and a verdict such as
`not_matched`, `excluded`, `too_recent (age 12.0 days)`, `no_rule`, `blocked` or `eligible`.

---

## UC10: Very large trees

```bash
python retention_archiver.py /data/exports --execute --workers 16 --max-delete 500000 \
    --report /var/log/retention/exports-$(date +%F).csv --summary-json /var/log/retention/exports-latest.json
```
- Iterative traversal handles very deep and very wide trees.
- Only names that match the include pattern are `stat`-ed.
- Parallel workers hide network latency.
- `--max-delete` stops a misconfiguration (for example `sourceFileIncludePattern = .*` on the wrong folder) before it deletes anything.
