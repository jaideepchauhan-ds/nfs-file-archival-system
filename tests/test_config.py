import pytest

import retention_archiver as ra


def parse(text):
    return ra.parse_config(text, "/x")


def test_minimal_valid_config_defaults_to_non_recursive():
    rule = parse("sourceFileIncludePattern = \\.csv$\nretentionDays = 30\n")
    assert rule.retention_days == 30
    assert rule.recursive is False
    assert rule.exclude is None
    assert rule.include.search("a.csv")


def test_full_config_with_comments_and_section_header():
    rule = parse(
        "# note\n[retention]\nsourceFileIncludePattern=\\.log$\nsourceFileExcludePattern=^keep\nretentionDays=7\nRecursive=TRUE\n"
    )
    assert rule.recursive is True
    assert rule.exclude.search("keep_this.log")


@pytest.mark.parametrize("text", [
    "retentionDays = 30\n",                                   # include missing
    "sourceFileIncludePattern = a\n",                                   # days missing
    "sourceFileIncludePattern = a\nretentionDays = 0\n",               # zero would delete everything
    "sourceFileIncludePattern = a\nretentionDays = -5\n",
    "sourceFileIncludePattern = a\nretentionDays = soon\n",
    "sourceFileIncludePattern = a\nretentionDays = 5\nrecursive = maybe\n",
    "sourceFileIncludePattern = (\nretentionDays = 5\n",                # broken regex
    "sourceFileIncludePattern = a\nretentionDays = 5\nretentionDay = 9\n",  # typo'd key
    "sourceFileIncludePattern = a\nretentionDays = 5\nretentionDays = 6\n",  # duplicate
    "[other]\nsourceFileIncludePattern = a\nretentionDays = 5\n",
    "just some words\n",
])
def test_invalid_configs_are_rejected(text):
    with pytest.raises(ra.ConfigError):
        parse(text)


def test_next_state_inheritance_rules():
    rec = ra.parse_config("sourceFileIncludePattern=a\nretentionDays=9\nrecursive=true\n", "/r")
    non = ra.parse_config("sourceFileIncludePattern=b\nretentionDays=3\nrecursive=false\n", "/n")
    bad = ra.Blocked("broken", "/b")

    assert ra.next_state(None, None) == (None, None)
    assert ra.next_state(None, rec) == (rec, rec)
    assert ra.next_state(rec, None) == (rec, rec)
    assert ra.next_state(non, None) == (non, None)
    assert ra.next_state(non, rec) == (non, rec)    # non-recursive does not hide the ancestor from children
    assert ra.next_state(rec, rec) == (rec, rec)
    assert ra.next_state(bad, rec) == (bad, bad)
    assert ra.next_state(rec, bad) == (bad, bad)    # a blocked subtree stays blocked


def test_shipped_sample_config_parses():
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "examples", "da.cfg.sample")
    with open(path) as handle:
        rule = ra.parse_config(handle.read(), "/sample")
    assert rule.retention_days == 60 and rule.recursive is True
    assert rule.include.pattern == r"^.*\.csv$" and rule.exclude.pattern == "^keep_"


def test_inline_comments_are_not_silently_accepted():
    # "# ..." after a value becomes part of the regex; the docs say so explicitly.
    rule = parse("sourceFileIncludePattern = a # note\nretentionDays = 5\n")
    assert rule.include.pattern == "a # note"


def test_slash_wrapped_and_plain_patterns_are_equivalent_and_empty_exclude_is_allowed():
    wrapped = parse("sourceFileIncludePattern=/^.*\\.csv$/\nsourceFileExcludePattern=\nretentionDays=3\nrecursive=TRUE\n")
    plain = parse("sourceFileIncludePattern=^.*\\.csv$\nretentionDays=3\nrecursive=true\n")
    assert wrapped.include.pattern == plain.include.pattern
    assert wrapped.exclude is None and wrapped.recursive is True


def test_keys_are_case_insensitive_but_empty_include_is_rejected():
    rule = parse("SOURCEFILEINCLUDEPATTERN=/a/\nRETENTIONDAYS=2\n")
    assert rule.retention_days == 2
    with pytest.raises(ra.ConfigError):
        parse("sourceFileIncludePattern=//\nretentionDays=2\n")
