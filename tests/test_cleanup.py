import pytest

from lib.cleanup import _matches, cleanup


def test_prefix_matching_is_case_insensitive_prefix_only():
    assert _matches("zztest-acme-corp", "zztest")
    assert _matches("ZZTEST-LAB", "zztest")
    assert not _matches("prod-zztest", "zztest")
    assert not _matches("", "zztest")


def test_empty_prefix_refused():
    # startswith("") matches EVERYTHING — cleanup must refuse outright
    with pytest.raises(ValueError):
        cleanup("")
    with pytest.raises(ValueError):
        cleanup("   ")
