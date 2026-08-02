"""Tests for the shell_exec allowlist module."""

from __future__ import annotations

import pytest

from adroid.permissions.allowlist import (
    AllowlistChecker,
    AllowlistConfig,
    DEFAULT_ALLOWLIST_PATTERNS,
    FORBIDDEN_METACHARS,
    get_default_checker,
    reset_default_checker,
)


# ---------------------------------------------------------------------------
# Default allowlist patterns
# ---------------------------------------------------------------------------


def test_default_allowlist_has_pm_list_packages():
    """The most common shell command must be allowlisted by default."""
    checker = AllowlistChecker()
    decision = checker.check("pm list packages -3")
    assert decision.allowed, f"should be allowed: {decision.reason}"


def test_default_allowlist_has_getprop():
    checker = AllowlistChecker()
    assert checker.check("getprop ro.product.model").allowed
    assert checker.check("getprop").allowed  # no args version
    assert checker.check("getprop ro.build.version.release").allowed


def test_default_allowlist_has_logcat():
    checker = AllowlistChecker()
    assert checker.check("logcat -d -t 100").allowed
    assert checker.check("logcat -d -t 50 *:S").allowed


def test_default_allowlist_has_df():
    checker = AllowlistChecker()
    assert checker.check("df -h").allowed
    assert checker.check("df").allowed


def test_default_allowlist_has_uptime():
    checker = AllowlistChecker()
    assert checker.check("uptime").allowed


def test_default_allowlist_has_ip_addr():
    checker = AllowlistChecker()
    assert checker.check("ip addr").allowed
    assert checker.check("ip route").allowed


def test_default_allowlist_has_dumpsys():
    checker = AllowlistChecker()
    assert checker.check("dumpsys battery").allowed
    assert checker.check("dumpsys window").allowed


# ---------------------------------------------------------------------------
# Forbidden commands
# ---------------------------------------------------------------------------


def test_dangerous_command_rejected():
    """rm -rf should NOT match any allowlist pattern."""
    checker = AllowlistChecker()
    decision = checker.check("rm -rf /")
    assert not decision.allowed


def test_unknown_command_rejected():
    checker = AllowlistChecker()
    decision = checker.check("some-random-binary --evil-flag")
    assert not decision.allowed
    assert "did not match" in decision.reason


def test_empty_command_rejected():
    checker = AllowlistChecker()
    decision = checker.check("")
    assert not decision.allowed
    assert "empty" in decision.reason


def test_whitespace_only_command_rejected():
    checker = AllowlistChecker()
    decision = checker.check("   ")
    assert not decision.allowed


# ---------------------------------------------------------------------------
# Forbidden metacharacters (compound commands)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("metachar", FORBIDDEN_METACHARS)
def test_metacharacters_rejected_by_default(metachar):
    """Compound commands using shell metacharacters are rejected by default."""
    checker = AllowlistChecker()
    # Even if the prefix is allowlisted, the metachar must trigger rejection
    cmd = f"pm list packages {metachar} rm -rf /"
    decision = checker.check(cmd)
    assert not decision.allowed
    assert "metacharacter" in decision.reason or "forbidden" in decision.reason


def test_compound_command_with_pipe_rejected():
    checker = AllowlistChecker()
    decision = checker.check("pm list packages | grep foo")
    assert not decision.allowed


def test_compound_command_with_semicolon_rejected():
    checker = AllowlistChecker()
    decision = checker.check("pm list packages; rm -rf /")
    assert not decision.allowed


def test_compound_command_with_redirect_rejected():
    checker = AllowlistChecker()
    decision = checker.check("pm list packages > /tmp/out.txt")
    assert not decision.allowed


def test_compound_command_allowed_when_config_enabled():
    """If allow_compound=True, metachar check is skipped — but pattern still applies."""
    config = AllowlistConfig(
        patterns=[r"^pm list packages \| grep [a-zA-Z]+$"],
        allow_compound=True,
    )
    checker = AllowlistChecker(config)
    decision = checker.check("pm list packages | grep foo")
    assert decision.allowed


# ---------------------------------------------------------------------------
# Length limits
# ---------------------------------------------------------------------------


def test_oversized_command_rejected():
    config = AllowlistConfig(max_command_length=100)
    checker = AllowlistChecker(config)
    long_cmd = "pm list packages " + "x" * 200
    decision = checker.check(long_cmd)
    assert not decision.allowed
    assert "max length" in decision.reason


# ---------------------------------------------------------------------------
# Custom patterns
# ---------------------------------------------------------------------------


def test_custom_pattern_can_be_added():
    config = AllowlistConfig(
        patterns=[r"^my-custom-tool --flag [a-z]+$"],
    )
    checker = AllowlistChecker(config)
    assert checker.check("my-custom-tool --flag hello").allowed
    assert not checker.check("my-custom-tool --flag HELLO").allowed  # case-sensitive


def test_list_patterns_returns_config():
    patterns = ["^foo$", "^bar$"]
    config = AllowlistConfig(patterns=patterns)
    checker = AllowlistChecker(config)
    assert checker.list_patterns() == patterns


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


def test_default_checker_is_singleton():
    reset_default_checker()
    c1 = get_default_checker()
    c2 = get_default_checker()
    assert c1 is c2


def test_reset_default_checker():
    reset_default_checker()
    c1 = get_default_checker()
    reset_default_checker()
    c2 = get_default_checker()
    assert c1 is not c2


# ---------------------------------------------------------------------------
# Decision object
# ---------------------------------------------------------------------------


def test_decision_includes_matched_pattern():
    checker = AllowlistChecker()
    decision = checker.check("pm list packages -3")
    assert decision.allowed
    assert decision.matched_pattern is not None
    assert "pm list packages" in decision.matched_pattern


def test_decision_includes_command():
    checker = AllowlistChecker()
    cmd = "getprop ro.product.model"
    decision = checker.check(cmd)
    assert decision.command == cmd
