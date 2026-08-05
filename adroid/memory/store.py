"""Memory module — persistent learning across sessions.

3-layer memory hierarchy (cognitive science model, research-verified):

  1. device_memory  (semantic) — facts about specific devices
     "Redmi Note 8 / MIUI 14: cmd package resolve-activity fails, use monkey -p"
     "Pixel 8 / Android 14: uiautomator dump takes 1.2s average"

  2. skill_memory (procedural) — successful action sequences per app
     "com.google.android.youtube: search icon at resource_id='com.google.android.youtube:id/search_button'"
     "com.whatsapp: first contact tap works via text='Contact Name'"

  3. failure_memory (failure patterns) — what went wrong + recovery
     "com.example.app: text='Login' fails 3x, try description='Sign in' instead"
     "Redmi Note 8: input.tap blocked until USB debugging (Security settings) enabled"

Storage: JSON files in ~/.adroid/memory/ (user-local, persists across sessions).
Each memory entry has:
  - key (natural language identifier)
  - value (structured data)
  - confidence (0.0-1.0, increases with repeated observations)
  - last_seen (timestamp)
  - observation_count (how many times this was confirmed)

The runtime auto-learns: every successful ui.tap_element call updates
skill_memory with the selector strategy that worked. Every failed call
updates failure_memory with what didn't work + what the fallback found.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Default storage path
# ---------------------------------------------------------------------------


def default_memory_dir() -> Path:
    """Return the default memory storage directory.

    Cross-platform:
        - Linux: $XDG_DATA_HOME/adroid/memory or ~/.adroid/memory
        - macOS: ~/Library/Application Support/adroid/memory
        - Windows: %APPDATA%/adroid/memory
    Falls back to ~/.adroid/memory/ if HOME is also unset (rare).
    """
    if xdg := os.environ.get("XDG_DATA_HOME"):
        return Path(xdg) / "adroid" / "memory"
    if appdata := os.environ.get("APPDATA"):  # Windows
        return Path(appdata) / "adroid" / "memory"
    if home := os.environ.get("HOME"):
        # macOS: prefer ~/Library/Application Support if it exists; else ~/.adroid
        library = Path(home) / "Library" / "Application Support"
        if library.exists():
            return library / "adroid" / "memory"
        return Path(home) / ".adroid" / "memory"
    # Last resort: home() expansion, never CWD (which is non-deterministic).
    return Path.home() / ".adroid" / "memory"


# ---------------------------------------------------------------------------
# Memory entry model
# ---------------------------------------------------------------------------


class MemoryEntry(BaseModel):
    """A single memory entry — a fact, skill, or failure pattern.

    confidence increases with repeated observations:
        confidence = min(1.0, 0.3 + 0.1 * observation_count)
    This gives 0.3 on first observation, 0.4 on second, ..., 1.0 after 7.
    """

    key: str  # natural language identifier, e.g. "device:Redmi_Note_8:launch_method"
    value: dict[str, Any]  # structured data
    confidence: float = Field(default=0.3, ge=0.0, le=1.0)
    last_seen: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    observation_count: int = Field(default=1, ge=1)
    source: str = "auto"  # "auto" | "manual"

    def observe(self) -> None:
        """Record another observation — increases confidence + count.

        Formula: confidence = min(1.0, 0.2 + 0.1 * observation_count)
        This gives: 1st=0.3, 2nd=0.4, 3rd=0.5, ..., 8th=1.0
        """
        self.observation_count += 1
        self.confidence = min(1.0, 0.2 + 0.1 * self.observation_count)
        self.last_seen = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# MemoryStore — thread-safe, file-backed
# ---------------------------------------------------------------------------


class MemoryStore:
    """Thread-safe persistent memory store.

    Three stores in one directory:
        <memory_dir>/device.json
        <memory_dir>/skill.json
        <memory_dir>/failure.json

    Each file is a JSON dict of {key: MemoryEntry.dict()}.

    Usage:
        store = MemoryStore()
        store.remember_device("Redmi_Note_8:launch_method",
                              {"method": "monkey", "reason": "MIUI blocks am start"})
        entry = store.recall_device("Redmi_Note_8:launch_method")
        if entry and entry.confidence > 0.7:
            # trust this memory
    """

    def __init__(self, memory_dir: Path | None = None) -> None:
        self._dir = memory_dir or default_memory_dir()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._device: dict[str, MemoryEntry] = self._load("device.json")
        self._skill: dict[str, MemoryEntry] = self._load("skill.json")
        self._failure: dict[str, MemoryEntry] = self._load("failure.json")

    # ------------------------------------------------------------------
    # Device memory (semantic — facts about specific devices)
    # ------------------------------------------------------------------

    def remember_device(self, key: str, value: dict[str, Any]) -> None:
        """Record or update a device fact."""
        with self._lock:
            if key in self._device:
                self._device[key].observe()
                self._device[key].value = value
            else:
                self._device[key] = MemoryEntry(key=key, value=value)
            self._save("device.json", self._device)

    def recall_device(self, key: str) -> MemoryEntry | None:
        """Retrieve a device fact. Returns None if not found."""
        with self._lock:
            return self._device.get(key)

    def list_device(self) -> list[MemoryEntry]:
        """List all device memories."""
        with self._lock:
            return list(self._device.values())

    # ------------------------------------------------------------------
    # Skill memory (procedural — successful action sequences)
    # ------------------------------------------------------------------

    def remember_skill(self, key: str, value: dict[str, Any]) -> None:
        """Record or update a successful action pattern."""
        with self._lock:
            if key in self._skill:
                self._skill[key].observe()
                self._skill[key].value = value
            else:
                self._skill[key] = MemoryEntry(key=key, value=value)
            self._save("skill.json", self._skill)

    def recall_skill(self, key: str) -> MemoryEntry | None:
        with self._lock:
            return self._skill.get(key)

    def list_skill(self) -> list[MemoryEntry]:
        with self._lock:
            return list(self._skill.values())

    # ------------------------------------------------------------------
    # Failure memory (what went wrong + recovery)
    # ------------------------------------------------------------------

    def remember_failure(self, key: str, value: dict[str, Any]) -> None:
        """Record or update a failure pattern."""
        with self._lock:
            if key in self._failure:
                self._failure[key].observe()
                self._failure[key].value = value
            else:
                self._failure[key] = MemoryEntry(key=key, value=value)
            self._save("failure.json", self._failure)

    def recall_failure(self, key: str) -> MemoryEntry | None:
        with self._lock:
            return self._failure.get(key)

    def list_failure(self) -> list[MemoryEntry]:
        with self._lock:
            return list(self._failure.values())

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def recall(self, store: str, key: str) -> MemoryEntry | None:
        """Generic recall across any store type."""
        if store == "device":
            return self.recall_device(key)
        elif store == "skill":
            return self.recall_skill(key)
        elif store == "failure":
            return self.recall_failure(key)
        return None

    def list_all(self) -> dict[str, list[MemoryEntry]]:
        """List all memories across all stores."""
        return {
            "device": self.list_device(),
            "skill": self.list_skill(),
            "failure": self.list_failure(),
        }

    def stats(self) -> dict[str, int]:
        """Return count of entries per store (thread-safe consistent snapshot)."""
        with self._lock:
            d = len(self._device)
            s = len(self._skill)
            f = len(self._failure)
        return {"device": d, "skill": s, "failure": f, "total": d + s + f}

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    def _load(self, filename: str) -> dict[str, MemoryEntry]:
        """Load a memory file. Returns empty dict if not found or corrupt.

        Per-entry isolation: a single corrupt entry drops just that entry,
        not the entire store. This is critical because _save() overwrites
        the file — dropping all entries on one bad row would silently
        destroy the entire memory store.
        """
        path = self._dir / filename
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError:
            return {}
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        result: dict[str, MemoryEntry] = {}
        for k, v in data.items():
            try:
                if isinstance(v, dict):
                    result[k] = MemoryEntry(**v)
            except Exception:
                # Skip single corrupt entry, preserve the rest.
                continue
        return result

    def _save(self, filename: str, data: dict[str, MemoryEntry]) -> None:
        """Save a memory file atomically with fsync + unique tmp name."""
        import tempfile
        path = self._dir / filename
        serializable = {k: v.model_dump(mode="json") for k, v in data.items()}
        # mkstemp guarantees a unique tmp path, surviving concurrent
        # writes from other threads/processes in the same PID.
        fd, tmp_str = tempfile.mkstemp(
            dir=str(self._dir), prefix=f".{filename}.", suffix=".tmp"
        )
        tmp = Path(tmp_str)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(serializable, indent=2, default=str, sort_keys=True))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)  # atomic on POSIX
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise


# ---------------------------------------------------------------------------
# Convenience key builders
# ---------------------------------------------------------------------------


def device_key(device_model: str, fact: str) -> str:
    """Build a device memory key. e.g. 'Redmi_Note_8:launch_method'."""
    return f"{device_model}:{fact}"


def skill_key(package: str, action: str) -> str:
    """Build a skill memory key. e.g. 'com.youtube:search_button_tap'."""
    return f"{package}:{action}"


def failure_key(package: str, selector: str) -> str:
    """Build a failure memory key. e.g. 'com.youtube:text=Login:failed_3x'."""
    return f"{package}:{selector}:failed"


__all__ = [
    "MemoryEntry",
    "MemoryStore",
    "default_memory_dir",
    "device_key",
    "skill_key",
    "failure_key",
]
