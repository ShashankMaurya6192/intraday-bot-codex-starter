"""Shared metadata and atomic last-known-good cache primitives."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from trading_bot.domain import IST


class ReferenceDataError(RuntimeError):
    """Reference data is missing, malformed, stale, or otherwise untrusted."""


@dataclass(frozen=True, slots=True)
class CacheMetadata:
    fetched_at: datetime
    record_count: int
    sha256: str
    source_url: str
    schema_version: int
    records_sha256: str
    source_record_count: int | None = None

    def __post_init__(self) -> None:
        if self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() is None:
            raise ValueError("fetched_at must be timezone-aware")
        object.__setattr__(self, "fetched_at", self.fetched_at.astimezone(IST))
        if self.record_count < 0:
            raise ValueError("record_count must be non-negative")
        if self.source_record_count is not None and self.source_record_count < 0:
            raise ValueError("source_record_count must be non-negative")
        for field_name in ("sha256", "records_sha256"):
            value = getattr(self, field_name).lower().strip()
            invalid_digest = len(value) != 64 or any(
                char not in "0123456789abcdef" for char in value
            )
            if invalid_digest:
                raise ValueError(f"{field_name} must be a SHA-256 hex digest")
            object.__setattr__(self, field_name, value)
        source = self.source_url.strip()
        if not source:
            raise ValueError("source_url must not be blank")
        object.__setattr__(self, "source_url", source)
        if self.schema_version < 1:
            raise ValueError("schema_version must be positive")

    @property
    def age_seconds(self) -> float:
        return max(0.0, (datetime.now(IST) - self.fetched_at).total_seconds())

    def to_dict(self) -> dict[str, object]:
        return {
            "fetched_at": self.fetched_at.isoformat(),
            "record_count": self.record_count,
            "sha256": self.sha256,
            "source_url": self.source_url,
            "schema_version": self.schema_version,
            "records_sha256": self.records_sha256,
            "source_record_count": (
                self.record_count
                if self.source_record_count is None
                else self.source_record_count
            ),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> CacheMetadata:
        try:
            return cls(
                fetched_at=datetime.fromisoformat(str(value["fetched_at"])),
                record_count=_strict_int(value["record_count"], "record_count"),
                sha256=str(value["sha256"]),
                source_url=str(value["source_url"]),
                schema_version=_strict_int(value["schema_version"], "schema_version"),
                records_sha256=str(value["records_sha256"]),
                source_record_count=_strict_int(
                    value.get("source_record_count", value["record_count"]),
                    "source_record_count",
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ReferenceDataError("cache metadata is malformed") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_records_bytes(records: Sequence[Mapping[str, object]]) -> bytes:
    return json.dumps(
        records,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def write_atomic_cache(
    path: Path,
    *,
    metadata: CacheMetadata,
    records: Sequence[Mapping[str, object]],
) -> None:
    """Atomically replace a cache only after all data has been validated."""
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"metadata": metadata.to_dict(), "records": list(records)},
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def read_cache_document(
    path: Path,
) -> tuple[CacheMetadata, list[Mapping[str, object]]]:
    try:
        raw = path.expanduser().resolve().read_bytes()
    except OSError as exc:
        raise ReferenceDataError(f"cache is unavailable: {path}") from exc
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReferenceDataError(f"cache is not valid JSON: {path}") from exc
    if not isinstance(document, Mapping):
        raise ReferenceDataError("cache document must be a JSON object")
    metadata_value = document.get("metadata")
    records_value = document.get("records")
    if not isinstance(metadata_value, Mapping) or not isinstance(records_value, list):
        raise ReferenceDataError("cache document is missing metadata or records")
    if not all(isinstance(row, Mapping) for row in records_value):
        raise ReferenceDataError("cache records must be JSON objects")
    metadata = CacheMetadata.from_mapping(metadata_value)
    records = list(records_value)
    if metadata.record_count != len(records):
        raise ReferenceDataError("cache record count does not match metadata")
    actual_records_hash = sha256_bytes(canonical_records_bytes(records))
    if actual_records_hash != metadata.records_sha256:
        raise ReferenceDataError("cache record hash does not match metadata")
    return metadata, records


def make_metadata(
    *,
    fetched_at: datetime,
    source_bytes: bytes,
    source_url: str,
    schema_version: int,
    records: Sequence[Mapping[str, object]],
    source_record_count: int | None = None,
) -> CacheMetadata:
    return CacheMetadata(
        fetched_at=fetched_at,
        record_count=len(records),
        sha256=sha256_bytes(source_bytes),
        source_url=source_url,
        schema_version=schema_version,
        records_sha256=sha256_bytes(canonical_records_bytes(records)),
        source_record_count=source_record_count,
    )


def require_fresh(
    metadata: CacheMetadata,
    *,
    now: datetime,
    max_age_seconds: float,
    same_ist_date: bool = False,
) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now_ist = now.astimezone(IST)
    age = (now_ist - metadata.fetched_at).total_seconds()
    if age < 0:
        raise ReferenceDataError("cache fetch timestamp is in the future")
    if same_ist_date and metadata.fetched_at.date() != now_ist.date():
        raise ReferenceDataError("cache is not from the current IST trading date")
    if age > max_age_seconds:
        raise ReferenceDataError("cache is older than the configured freshness limit")


def _strict_int(value: object, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")
    result = int(value)
    if str(result) != str(value).strip() and not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    return result
