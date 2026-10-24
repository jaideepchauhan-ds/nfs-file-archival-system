# Operations runbook

## 1. Roll out safely

| Stage | Command | Goal |
|---|---|---|
| 1. Dry run | `python retention_archiver.py /data/exports --report plan.csv` | Review what would happen. Nothing changes. |
| 2. Spot-check | `--explain <path>` on surprising files | Confirm the config chain is what you intended. |
| 3. Move, not delete | `--execute --action move --archive-dir /data/_archive/<date> --max-delete N` | Reversible first run. |
| 4. Observe | Check the report and summary; wait one retention cycle | Catch missed exclusions. |
| 5. Delete | `--execute --max-delete N` | Steady state. |

Pick `--max-delete` slightly above the largest sensible run, so a misconfiguration is stopped instead of executed.

## 2. Scheduling

**cron (daily at 02:30):**
```cron
30 2 * * * /usr/bin/python3 /opt/retention/retention_archiver.py /data/exports --execute --workers 8 --max-delete 500000 --strict --report /var/log/retention/exports-$(date +\%F).csv --summary-json /var/log/retention/exports-latest.json >> /var/log/retention/exports.log 2>&1
```

**systemd timer (preferred):** a `oneshot` service running the same command and a `.timer` with `OnCalendar=*-*-* 02:30:00`, `Persistent=true`.

Run **one invocation per top-level data area** if teams need independent schedules or failure isolation.

## 3. Monitoring

- Alert on exit code `1` (errors or, with `--strict`, invalid configs) and `2` (refused, for example `--max-delete` tripped).
- Track `eligible_files`, `eligible_bytes` and `duration_seconds` from the JSON summary over time. A sudden jump means a policy or data change.
- Keep the CSV reports for the audit window (for example 90 days).
- `config_errors > 0` means a team's config is broken and its subtree is being skipped.

## 4. Rollback

- Moved files: copy them back from the archive path (the layout mirrors the source).
- Deleted files: restore from the storage snapshot or backup. This is why the first runs should use `move`.

## 5. Typical questions

| Question | Answer |
|---|---|
| A file was not deleted | `--explain <file>`; look for `not_matched`, `excluded`, `too_recent`, `no_rule` or `blocked`. |
| A config seems ignored | Check the file name (`--config-name`), key spelling, and the logged `INVALID config` line. |
| Run refused with exit 2 | Read the `refused:` log line: protected or shallow root, `--max-delete`, lock held, or archive inside root. |
| Two schedules overlap | The per-root lock makes the second run exit 2; stagger the schedules. |
| Very slow on network storage | Increase `--workers`; avoid `sourceFileIncludePattern = .*` on huge folders (it forces a `stat` per file). |

## 6. Change management

1. A data owner edits `da.cfg` in their folder.
2. Another person reviews the diff (retention days, include pattern, recursive flag).
3. Run `--explain` on a few representative files, then a dry run on that subtree.
4. Merge, and the next scheduled run applies it.

## 7. Security notes

- Run as a dedicated service account with write access only to the managed areas.
- Prefer `--action move` on shares where accidental deletion is costly.
- Treat `da.cfg` as code: restrict who can write it, since it controls deletion.
- Regular expressions come from config files; avoid patterns with catastrophic backtracking.
