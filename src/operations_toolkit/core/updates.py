from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class UpdatePolicy:
    owner: str
    repository: str


@dataclass(frozen=True, slots=True)
class UpdateManifest:
    version: str
    download_url: str
    sha256: str
    notes: str = ""


def validate_manifest(payload: dict[str, object], policy: UpdatePolicy) -> UpdateManifest:
    url = str(payload.get("download_url", ""))
    parsed = urlparse(url)
    expected_prefix = f"/{policy.owner}/{policy.repository}/releases/download/"
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or not parsed.path.startswith(expected_prefix)
    ):
        raise ValueError("download must come from the approved GitHub repository over HTTPS")
    digest = str(payload.get("sha256", ""))
    if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        raise ValueError("manifest SHA-256 is invalid")
    version = str(payload.get("version", ""))
    if not version:
        raise ValueError("manifest version is required")
    return UpdateManifest(version, url, digest.lower(), str(payload.get("notes", "")))


def verify_download(path: Path, expected_sha256: str) -> Path:
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual.lower() != expected_sha256.lower():
        raise ValueError("download checksum does not match release metadata")
    return path
