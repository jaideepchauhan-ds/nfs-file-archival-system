import json
import os
from datetime import datetime, timezone

import pytest

import retention_archiver as ra
from tests.helpers import NOW, make_root, write_cfg, write_file

NOW_ISO = datetime.fromtimestamp(NOW, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
BASE = ["--now", NOW_ISO, "--min-depth", "1"]


def tree(root):
    write_cfg(root, "", r"\.csv$", 30, "true")
    write_file(root, "old.csv", 40)
    write_file(root, "sub/old2.csv", 50)
    write_file(root, "new.csv", 1)
    write_file(root, "notes.txt", 400)


def first_candidate(root, suffix="old.csv"):
    cands = list(ra.scan(root, ra.DEFAULT_CONFIG_NAME, NOW, ra.Counter()))
    return next(c for c in cands if c.path.endswith(suffix) and os.sep + "sub" + os.sep not in c.path)


def test_dry_run_changes_nothing_and_writes_report():
    tmp, root = make_root()
    with tmp:
        tree(root)
        report = os.path.join(os.path.dirname(root), "report.csv")
        assert ra.main([root, *BASE, "--report", report]) == 0
        assert os.path.exists(os.path.join(root, "old.csv"))
        with open(report) as handle:
            text = handle.read()
        assert "would_delete" in text and "old2.csv" in text


def test_execute_deletes_only_eligible_files():
    tmp, root = make_root()
    with tmp:
        tree(root)
        assert ra.main([root, *BASE, "--execute"]) == 0
        assert not os.path.exists(os.path.join(root, "old.csv"))
        assert not os.path.exists(os.path.join(root, "sub", "old2.csv"))
        for kept in ("new.csv", "notes.txt", "da.cfg"):
            assert os.path.exists(os.path.join(root, kept))


def test_execute_with_parallel_workers():
    tmp, root = make_root()
    with tmp:
        write_cfg(root, "", r"\.csv$", 1, "true")
        for i in range(200):
            write_file(root, f"d{i % 10}/f{i}.csv", 9)
        summary = os.path.join(os.path.dirname(root), "summary.json")
        assert ra.main([root, *BASE, "--execute", "--workers", "8", "--summary-json", summary]) == 0
        with open(summary) as handle:
            assert json.load(handle)["results"] == {"deleted": 200}


def test_move_preserves_relative_structure_outside_root():
    tmp, root = make_root()
    with tmp:
        tree(root)
        archive = os.path.join(os.path.dirname(root), "archive")
        assert ra.main([root, *BASE, "--execute", "--action", "move", "--archive-dir", archive]) == 0
        assert os.path.exists(os.path.join(archive, "old.csv"))
        assert os.path.exists(os.path.join(archive, "sub", "old2.csv"))
        assert not os.path.exists(os.path.join(root, "old.csv"))


def test_move_never_overwrites_existing_archive_files():
    tmp, root = make_root()
    with tmp:
        tree(root)
        archive = os.path.join(os.path.dirname(root), "archive")
        os.makedirs(archive)
        with open(os.path.join(archive, "old.csv"), "w") as handle:
            handle.write("precious")
        ra.main([root, *BASE, "--execute", "--action", "move", "--archive-dir", archive])
        with open(os.path.join(archive, "old.csv")) as handle:
            assert handle.read() == "precious"
        assert os.path.exists(os.path.join(archive, "old.csv.dup1"))


def test_file_modified_after_scan_is_skipped():
    tmp, root = make_root()
    with tmp:
        tree(root)
        target = first_candidate(root)
        with open(target.path, "a") as handle:
            handle.write("more data arrived")          # a writer touched it after the scan
        status, _ = ra.apply_action(target, "delete", root, None)
        assert status == "changed_since_scan"
        assert os.path.exists(target.path)


def test_file_replaced_by_symlink_after_scan_is_not_followed():
    tmp, root = make_root()
    with tmp:
        tree(root)
        target = first_candidate(root)
        precious = write_file(os.path.dirname(root), "precious.txt", 1)
        os.remove(target.path)
        os.symlink(precious, target.path)
        status, _ = ra.apply_action(target, "delete", root, None)
        assert status == "not_regular"
        assert os.path.exists(precious)


def test_max_delete_circuit_breaker_blocks_execution():
    tmp, root = make_root()
    with tmp:
        tree(root)
        assert ra.main([root, *BASE, "--execute", "--max-delete", "1"]) == 2
        assert os.path.exists(os.path.join(root, "old.csv"))   # nothing was deleted


@pytest.mark.parametrize("bad_root", ["/", "/etc", "/usr"])
def test_protected_roots_are_refused(bad_root):
    assert ra.main([bad_root, "--execute"]) == 2


def test_shallow_root_is_refused_by_min_depth():
    tmp, root = make_root()
    with tmp:
        assert ra.main([root, "--min-depth", "50"]) == 2


def test_archive_dir_inside_root_is_refused():
    tmp, root = make_root()
    with tmp:
        tree(root)
        inside = os.path.join(root, "archive")
        assert ra.main([root, *BASE, "--execute", "--action", "move", "--archive-dir", inside]) == 2


def test_move_requires_archive_dir_and_delete_rejects_it():
    tmp, root = make_root()
    with tmp:
        assert ra.main([root, *BASE, "--action", "move"]) == 2
        assert ra.main([root, *BASE, "--archive-dir", "/x/y"]) == 2


def test_strict_mode_fails_when_a_config_is_invalid():
    tmp, root = make_root()
    with tmp:
        write_cfg(root, "", r"\.csv$", 30, "true")
        os.makedirs(os.path.join(root, "bad"))
        with open(os.path.join(root, "bad", "da.cfg"), "w") as handle:
            handle.write("nonsense")
        assert ra.main([root, *BASE]) == 0
        assert ra.main([root, *BASE, "--strict"]) == 1


def test_explain_reports_inheritance_chain_and_file_verdict(capsys):
    tmp, root = make_root()
    with tmp:
        write_cfg(root, "", r"\.csv$", 30, "true")
        write_file(root, "a/b/old.csv", 40)
        target = os.path.join(root, "a", "b", "old.csv")
        assert ra.main([root, *BASE, "--explain", target]) == 0
        out = capsys.readouterr().out
        assert "no local config" in out
        assert "VERDICT for old.csv: eligible" in out
