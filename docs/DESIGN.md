# Design

## Problem

Storage fills up with files that are no longer needed: staging extracts, logs, temp exports. The usual answer is a scheduled cleanup
job per directory. Over time that creates:

- many near-identical jobs with hard-coded paths, retention periods and patterns
- no single place to see what is being deleted, or why
- risky edits to shared scripts when a team wants a different retention
- jobs that silently stop covering new sub-directories

## Idea

**Move the policy next to the data and keep one executor.** Each directory may hold a small `da.cfg`. A single script walks
the tree, works out which config applies to each directory, and acts on files that match and are old enough.

| Before | After |
|---|---|
| N scheduled jobs, each with its own paths and rules | 1 scheduled job |
| Retention hidden in job code | Retention in a reviewable file beside the data |
| New sub-folder needs a new job | New sub-folder inherits automatically |
| Behaviour differs per script | One tested behaviour |

## Algorithm

```
stack = [(root, inherited=None)]
while stack:
    dir, inherited = stack.pop()
    entries = scandir(dir)                      # one syscall batch per directory
    local = load(dir/da.cfg) if present  # Rule | Blocked | None
    effective, for_children = next_state(local, inherited)
    for entry in entries:
        symlink          -> skip
        directory        -> push (entry, for_children)
        regular file     -> classify(name, effective, now)   # stat only if the name matches
```

`next_state(local, inherited)`:

| local | inherited | applies here | handed to children |
|---|---|---|---|
| any | Blocked | Blocked | Blocked |
| none | X | X | X |
| invalid | any valid | Blocked | Blocked |
| valid, recursive | any | local | local |
| valid, non-recursive | X | local | X |

`classify` order: no rule, blocked, include mismatch, exclude match, then age. Cheap string checks run before the `stat`.

Complexity is O(directories + files) with one `scandir` per directory, and memory is O(eligible files).

## Key decisions

| Decision | Reasoning |
|---|---|
| Dry run by default | A cleanup tool's worst failure is irreversible. Acting requires an explicit flag. |
| `sourceFileIncludePattern` is mandatory | Removes the "empty pattern matches everything" footgun. To match everything, write `.*` on purpose. |
| `retentionDays >= 1` | `0` would mean "delete everything", usually a mistake. |
| Strictly-older-than comparison | A file exactly at the limit is kept; predictable at the boundary. |
| Invalid config blocks the subtree | A broken config under a valid parent must not fall back to the parent's rule: that could delete files the owner meant to protect. |
| Non-recursive config does not hide ancestors from children | Matches the intuition "this folder is special, its children are normal". |
| Unknown keys are errors | Catches typos such as `retentionDay`. |
| Two-phase (plan, then act) | Enables `--max-delete`, the report, and the dry run using the same code path. |
| Re-check before acting | The tree changes while a long scan runs; size/mtime/type are verified immediately before each delete or move. |
| Never follow symlinks | A link could point outside the tree. |
| Threads for deletes | Network-filesystem deletes are latency-bound, not CPU-bound. |
| Single file, standard library only | Easy to audit, deploy to locked-down hosts, and review. |
| `mtime` as age | Cheap and available everywhere. It is documented as a limitation. |

## Governance model

- The platform team owns `retention_archiver.py` and its tests.
- Data owners own the `da.cfg` in their directories, changed through normal review.
- Rollout is staged: dry run, review the CSV, `move` to an archive, then `delete`.

## Testing strategy

45 tests build real temporary directory trees with back-dated files and a fixed clock:

- config parsing (valid, 11 invalid shapes)
- the inheritance table above
- boundary age, excludes, symlinks, many directories, custom config name
- actions: dry run, delete, parallel delete, move, no-overwrite, changed-after-scan, symlink-swapped-after-scan
- safety: protected roots, shallow roots, archive inside root, `--max-delete`, `--strict`
- explain mode output

## Limitations and possible extensions

- Remove empty directories after cleanup.
- Age by other timestamps (atime, a date in the file name).
- Streaming execution for extremely large plans.
- Per-config `action` (delete versus move) and dry-run annotations in the report.
- Metrics export (for example a Prometheus textfile) alongside the JSON summary.
