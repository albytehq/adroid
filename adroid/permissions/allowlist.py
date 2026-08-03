"""Shell command allowlist for the SHELL_EXEC capability.

The runtime refuses to execute any shell command that does not match at
least one allowlist pattern. This is the "unrestricted shell execution"
promise from the spec (section 2.2): "shell_exec is a permission scope
that must be explicitly approved per session, and the runtime refuses to
execute commands outside an allowlist defined by policy."

v0.1.0 ships with a conservative default allowlist focused on read-only
inspection commands. Operators can extend the allowlist via a YAML/JSON
config file (--allowlist-path on `adroid start`).

Allowlist semantics:
    - Patterns are POSIX extended regex matched against the FULL command
      string (not a substring match).
    - Patterns are case-sensitive.
    - Shell metacharacters (``;``, ``|``, ``&``, ``$()``, ``&&``, ``||``)
      are FORBIDDEN by default — even if the prefix matches an allowlist
      pattern. The runtime cannot safely parse shell composition, so it
      refuses compound commands.
    - Operators who need compound commands can disable the metachar
      filter via a config flag (``allow_compound: true``), but this is
      explicitly NOT recommended for production.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Default allowlist — conservative, read-only inspection commands
# ---------------------------------------------------------------------------


DEFAULT_ALLOWLIST_PATTERNS: list[str] = [
    # Package management (read-only)
    r"^pm list packages( -[0-9a-zA-Z]+)*$",
    r"^pm list permissions( -[0-9a-zA-Z]+)*$",
    r"^pm list features$",
    r"^pm list instrumentation$",
    r"^pm path [a-zA-Z0-9._]+$",

    # Device properties (read-only)
    r"^getprop( [a-zA-Z0-9._]+)*$",
    r"^dumpsys [a-zA-Z0-9_]+( [a-zA-Z0-9._-]+)*$",
    r"^dumpsys battery$",
    r"^dumpsys window$",
    r"^dumpsys activity$",
    r"^dumpsys notification$",

    # Logcat (read-only)
    r"^logcat -d( -[a-zA-Z]+ [0-9a-zA-Z*:._]+)*( \*:S)?$",
    r"^logcat -d -t [0-9]+$",
    r"^logcat -d -t [0-9]+ \*[A-Z]:[A-ZVIWEF]$",

    # Filesystem inspection (read-only)
    r"^df( -h)?( [a-zA-Z0-9/_-]+)*$",
    r"^ls( -[a-zA-Z]+)?( [a-zA-Z0-9/_\-.]+)*$",
    r"^stat [a-zA-Z0-9/_\-.]+$",
    r"^file [a-zA-Z0-9/_\-.]+$",

    # Network inspection (read-only)
    r"^ip addr$",
    r"^ip route$",
    r"^ip -6 addr$",
    r"^ifconfig( [a-zA-Z0-9]+)?$",
    r"^netstat( -[a-zA-Z]+)*$",
    r"^ss( -[a-zA-Z]+)*$",
    r"^cat /proc/[a-zA-Z0-9_]+(/[a-zA-Z0-9_]+)*$",

    # Settings (read-only)
    r"^settings get [a-zA-Z0-9_. ]+$",
    r"^settings list [a-zA-Z]+$",

    # Activity manager (read-only)
    r"^am list [a-zA-Z]+$",

    # App launch via monkey (MIUI-friendly, count must be 1)
    # monkey -p <package> 1 — launches the package's main activity
    r"^monkey -p [a-zA-Z0-9._]+ 1$",
    r"^monkey -p [a-zA-Z0-9._]+ -c [a-zA-Z.]+ 1$",

    # Storage inspection
    r"^mount$",
    r"^free( -[a-zA-Z]+)*$",
    r"^uptime$",

    # Date/time
    r"^date$",
    r"^uptime$",
]


# Forbidden shell metacharacters — compound commands are unsafe
FORBIDDEN_METACHARS: list[str] = [";", "|", "&", "`", "$(", "${", "&&", "||", ">"]


# ---------------------------------------------------------------------------
# Allowlist engine
# ---------------------------------------------------------------------------


@dataclass
class AllowlistConfig:
    """Configuration for the shell_exec allowlist.

    Attributes:
        patterns: List of regex patterns. Command must match at least one.
        allow_compound: If True, allow shell metacharacters (DANGEROUS).
        max_command_length: Reject commands longer than this (default 4KB).
    """

    patterns: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWLIST_PATTERNS))
    allow_compound: bool = False
    max_command_length: int = 4096

    @classmethod
    def from_file(cls, path: Path) -> "AllowlistConfig":
        """Load config from JSON or YAML file.

        File format:
            {
              "patterns": ["^pm list packages$", ...],
              "allow_compound": false,
              "max_command_length": 4096
            }
        """
        import json
        text = Path(path).read_text()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Try YAML
            try:
                import yaml
                data = yaml.safe_load(text)
            except ImportError as exc:
                raise ValueError(
                    f"Cannot parse {path}: not valid JSON, and PyYAML not installed"
                ) from exc

        return cls(
            patterns=data.get("patterns", list(DEFAULT_ALLOWLIST_PATTERNS)),
            allow_compound=data.get("allow_compound", False),
            max_command_length=data.get("max_command_length", 4096),
        )


@dataclass
class AllowlistDecision:
    """Result of checking a command against the allowlist."""

    allowed: bool
    matched_pattern: str | None = None
    reason: str | None = None
    command: str | None = None


class AllowlistChecker:
    """Check whether a shell command is safe to execute."""

    def __init__(self, config: AllowlistConfig | None = None) -> None:
        self._config = config or AllowlistConfig()
        # Pre-compile patterns for performance
        self._compiled: list[re.Pattern[str]] = [
            re.compile(p) for p in self._config.patterns
        ]

    @property
    def config(self) -> AllowlistConfig:
        return self._config

    def check(self, command: str) -> AllowlistDecision:
        """Check if ``command`` is allowed.

        Returns an AllowlistDecision with:
            - allowed=True if the command passed all checks
            - allowed=False + reason if rejected
        """
        # Strip leading/trailing whitespace
        cmd = command.strip()

        # Length check
        if len(cmd) > self._config.max_command_length:
            return AllowlistDecision(
                allowed=False,
                reason=f"command exceeds max length ({self._config.max_command_length} bytes)",
                command=command,
            )

        # Empty command
        if not cmd:
            return AllowlistDecision(
                allowed=False,
                reason="empty command",
                command=command,
            )

        # Metachar check (unless allow_compound=True)
        if not self._config.allow_compound:
            for mc in FORBIDDEN_METACHARS:
                if mc in cmd:
                    return AllowlistDecision(
                        allowed=False,
                        reason=f"forbidden shell metacharacter: {mc!r} (compound commands not allowed; set allow_compound=True in config to override)",
                        command=command,
                    )

        # Pattern match
        for pattern, compiled in zip(self._config.patterns, self._compiled, strict=True):
            if compiled.fullmatch(cmd):
                return AllowlistDecision(
                    allowed=True,
                    matched_pattern=pattern,
                    command=command,
                )

        return AllowlistDecision(
            allowed=False,
            reason="command did not match any allowlist pattern",
            command=command,
        )

    def list_patterns(self) -> list[str]:
        """Return the list of allowlist patterns (for /api/tools documentation)."""
        return list(self._config.patterns)


# ---------------------------------------------------------------------------
# Module-level singleton (used by default in runtime if no config provided)
# ---------------------------------------------------------------------------


_default_checker: AllowlistChecker | None = None


def get_default_checker() -> AllowlistChecker:
    global _default_checker
    if _default_checker is None:
        _default_checker = AllowlistChecker()
    return _default_checker


def reset_default_checker() -> None:
    """Reset the singleton — used in tests."""
    global _default_checker
    _default_checker = None
