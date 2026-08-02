"""Device bridge layer — abstract Protocol + ADB + Mock + Termux implementations."""

from adroid.bridge.adb import AdbBridge, LocalBlobStore
from adroid.bridge.base import BlobStore, DeviceBridge
from adroid.bridge.mock import InMemoryBlobStore, MockBridge
from adroid.bridge.termux import TermuxBridge

__all__ = [
    "AdbBridge",
    "BlobStore",
    "DeviceBridge",
    "InMemoryBlobStore",
    "LocalBlobStore",
    "MockBridge",
    "TermuxBridge",
]
