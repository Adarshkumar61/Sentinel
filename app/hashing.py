"""Deterministic evidence hashing, adapted from sentinel_block_chain."""
import hashlib
import json
from pathlib import Path


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk) 
    return "0x" + digest.hexdigest()


def sha256_event(event_id: str, event_type: str, camera: str, details: str) -> str:
    """Hash canonical event metadata if the detector did not produce an image."""
    payload = json.dumps(
        {"event_id": event_id, "event_type": event_type, "camera": camera, "details": details},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "0x" + hashlib.sha256(payload).hexdigest()
