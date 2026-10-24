import os

import retention_archiver as ra
from tests.helpers import NOW, eligible, make_root, write_cfg, write_file


def test_recursive_config_reaches_deep_directories():
    tmp, root = make_root()
    with tmp:
        write_cfg(root, "", r"\.csv$", 30, "true")
        write_file(root, "a.csv", 40)
        write_file(root, "a.txt", 400)                  # name does not match
        write_file(root, "x/y/z/deep.csv", 31)          # inherited three levels down
        write_file(root, "x/recent.csv", 29)            # too recent
        found, _ = eligible(root)
        assert found == ["a.csv", os.path.join("x", "y", "z", "deep.csv")]


def test_local_config_overrides_parent_and_is_inherited_by_its_children():
    tmp, root = make_root()
    with tmp:
        write_cfg(root, "", r"\.log$", 60, "true")
        write_cfg(root, "short", r"\.log$", 5, "true")
        write_file(root, "top.log", 70)
        write_file(root, "short/a.log", 6)
        write_file(root, "short/sub/b.log", 6)          # inherits the 5-day override
        write_file(root, "other/c.log", 6)              # still 60 days, too recent
        found, _ = eligible(root)
        assert found == sorted([os.path.join("short", "a.log"), os.path.join("short", "sub", "b.log"), "top.log"])


def test_non_recursive_config_applies_only_locally_and_ancestor_resumes_below():
    tmp, root = make_root()
    with tmp:
        write_cfg(root, "", r"\.log$", 60, "true")
        write_cfg(root, "logs", r"\.txt$", 15, "false")
        write_file(root, "logs/debug.txt", 20)          # local rule: txt > 15 days
        write_file(root, "logs/error.log", 90)          # local rule does not match .log
        write_file(root, "logs/archive/old.txt", 25)    # ancestor rule only matches .log
        write_file(root, "logs/archive/system.log", 80) # ancestor rule applies again
        found, _ = eligible(root)
        assert found == sorted([os.path.join("logs", "archive", "system.log"), os.path.join("logs", "debug.txt")])


def test_no_config_and_no_recursive_ancestor_leaves_everything_alone():
    tmp, root = make_root()
    with tmp:
        write_cfg(root, "", r"\.dat$", 10, "false")
        write_file(root, "master.dat", 50)              # local config applies
        write_file(root, "child/data.dat", 500)         # parent not recursive -> untouched
        found, stats = eligible(root)
        assert found == ["master.dat"]
        assert stats["files_no_rule"] == 1


def test_exclude_pattern_always_protects():
    tmp, root = make_root()
    with tmp:
        write_cfg(root, "", r"\.csv$", 10, "true", exclude=r"^keep_")
        write_file(root, "keep_me.csv", 999)
        write_file(root, "drop_me.csv", 999)
        found, stats = eligible(root)
        assert found == ["drop_me.csv"]
        assert stats["files_excluded"] == 1


def test_age_boundary_is_strictly_older_than_retention():
    tmp, root = make_root()
    with tmp:
        write_cfg(root, "", r"\.csv$", 30, "true")
        write_file(root, "exact.csv", 30)
        write_file(root, "older.csv", 30.01)
        found, _ = eligible(root)
        assert found == ["older.csv"]


def test_invalid_config_blocks_its_whole_subtree_even_under_a_valid_parent():
    tmp, root = make_root()
    with tmp:
        write_cfg(root, "", r"\.csv$", 10, "true")
        os.makedirs(os.path.join(root, "bad"))
        with open(os.path.join(root, "bad", "da.cfg"), "w") as handle:
            handle.write("sourceFileIncludePattern = (\nretentionDays = 1\n")
        write_file(root, "bad/old.csv", 999)
        write_file(root, "bad/deeper/old.csv", 999)
        write_file(root, "fine/old.csv", 999)
        found, stats = eligible(root)
        assert found == [os.path.join("fine", "old.csv")]
        assert stats["config_errors"] == 1
        assert stats["files_blocked"] == 2


def test_config_file_itself_is_never_a_candidate():
    tmp, root = make_root()
    with tmp:
        write_cfg(root, "", r".*", 1, "true")
        cfg = os.path.join(root, "da.cfg")
        os.utime(cfg, (NOW - 999 * 86400, NOW - 999 * 86400))
        write_file(root, "a.bin", 50)
        found, _ = eligible(root)
        assert found == ["a.bin"]


def test_symlinks_are_never_followed_or_touched():
    tmp, root = make_root()
    with tmp:
        write_cfg(root, "", r".*", 1, "true")
        outside = os.path.join(os.path.dirname(root), "outside")
        os.makedirs(outside)
        write_file(outside, "victim.txt", 999)
        os.symlink(outside, os.path.join(root, "linked_dir"))
        os.symlink(os.path.join(outside, "victim.txt"), os.path.join(root, "linked_file.txt"))
        found, stats = eligible(root)
        assert found == []
        assert stats["symlinks_skipped"] == 2


def test_custom_config_name_is_supported():
    tmp, root = make_root()
    with tmp:
        write_cfg(root, "", r"\.csv$", 5, "true", name=".cleanup")
        write_file(root, "a.csv", 50)
        stats = ra.Counter()
        found = list(ra.scan(root, ".cleanup", NOW, stats))
        assert len(found) == 1


def test_many_directories_are_handled_iteratively():
    tmp, root = make_root()
    with tmp:
        write_cfg(root, "", r"\.csv$", 1, "true")
        for i in range(300):
            write_file(root, os.path.join(f"d{i // 20}", f"e{i}", "f.csv"), 5)
        found, stats = eligible(root)
        assert len(found) == 300
        assert stats["dirs_scanned"] > 300
