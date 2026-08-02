"""Device bridge layer — abstract Protocol + ADB + Mock implementations."""

from adroid.bridge.adb import AdbBridge, LocalBlobStore
from adroid.bridge.base import BlobStore, DeviceBridge
from adroid.bridge.mock import InMemoryBlobStore, MockBridge

__all__ = [
    "AdbBridge",
    "BlobStore",
    "DeviceBridge",
    "InMemoryBlobStore",
    "LocalBlobStore",
    "MockBridge",
]
