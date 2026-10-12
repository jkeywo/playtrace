"""One deterministic identity encoding for every Play Trace content digest."""

from __future__ import annotations

import base64
import hashlib
import json
import math
from collections.abc import Mapping
from datetime import UTC, date, datetime, time
from enum import Enum
from pathlib import PurePath
from typing import Any
from uuid import UUID

from pydantic import BaseModel


CANONICAL_IDENTITY_SCHEMA = "playtrace.identity"
CANONICAL_IDENTITY_VERSION = 1


class CanonicalEncodingError(ValueError):
    """A value cannot safely participate in a durable identity."""


def canonical_digest(value: Any, *, domain: str) -> str:
    """Return the SHA-256 digest of a versioned, domain-separated envelope."""

    return hashlib.sha256(canonical_bytes(value, domain=domain)).hexdigest()


def canonical_bytes(value: Any, *, domain: str) -> bytes:
    """Encode a value as the canonical Play Trace identity envelope."""

    if not isinstance(domain, str) or not domain.strip():
        raise CanonicalEncodingError("identity domain must be a nonblank string")
    _reject_lone_surrogates(domain)
    envelope = {
        "schema": CANONICAL_IDENTITY_SCHEMA,
        "version": CANONICAL_IDENTITY_VERSION,
        "domain": domain,
        "value": _normalize(value, active=set()),
    }
    return json.dumps(
        envelope,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", errors="strict")


def canonical_json(value: Any, *, domain: str) -> str:
    """Return the UTF-8 canonical envelope as text, primarily for diagnostics."""

    return canonical_bytes(value, domain=domain).decode("utf-8")


def _normalize(value: Any, *, active: set[int]) -> Any:
    if isinstance(value, BaseModel):
        identity = id(value)
        if identity in active:
            raise CanonicalEncodingError("cyclic models cannot be canonicalized")
        active.add(identity)
        try:
            return {
                "t": "model",
                "c": _class_name(value),
                "v": _normalize(value.model_dump(mode="python"), active=active),
            }
        finally:
            active.remove(identity)
    if isinstance(value, Enum):
        return {
            "t": "enum",
            "c": _class_name(value),
            "v": _normalize(value.value, active=active),
        }
    if value is None:
        return {"t": "none"}
    if type(value) is bool:
        return {"t": "bool", "v": value}
    if type(value) is int:
        return {"t": "int", "v": str(value)}
    if type(value) is float:
        if not math.isfinite(value):
            raise CanonicalEncodingError("non-finite floats cannot be canonicalized")
        # Python's hexadecimal form is exact and independent of JSON number
        # formatting implementations.  Normalise negative zero because Python
        # equality and the rest of Play Trace treat it as zero.
        encoded = "0x0.0p+0" if value == 0 else value.hex()
        return {"t": "float", "v": encoded}
    if type(value) is str:
        _reject_lone_surrogates(value)
        return {"t": "str", "v": value}
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise CanonicalEncodingError("naive datetimes cannot be canonicalized")
        return {
            "t": "datetime",
            "v": value.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }
    if isinstance(value, date):
        return {"t": "date", "v": value.isoformat()}
    if isinstance(value, time):
        # A UTC offset on a date-free time is not a portable instant: timezone
        # rules can depend on the missing date.  Callers that need temporal
        # identity must supply an aware datetime, which is normalized above.
        raise CanonicalEncodingError(
            "standalone times cannot be canonicalized; use an aware datetime"
        )
    if isinstance(value, UUID):
        return {"t": "uuid", "v": str(value)}
    if isinstance(value, PurePath):
        return {"t": "path", "v": value.as_posix()}
    if type(value) is bytes:
        return {"t": "bytes", "v": base64.b64encode(value).decode("ascii")}
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise CanonicalEncodingError("cyclic mappings cannot be canonicalized")
        if any(type(key) is not str for key in value):
            raise CanonicalEncodingError("canonical mappings require string keys")
        active.add(identity)
        try:
            result: list[list[Any]] = []
            for key in sorted(value):
                _reject_lone_surrogates(key)
                result.append(
                    [
                        _normalize(key, active=active),
                        _normalize(value[key], active=active),
                    ]
                )
            return {"t": "map", "v": result}
        finally:
            active.remove(identity)
    if type(value) in (set, frozenset):
        identity = id(value)
        if identity in active:
            raise CanonicalEncodingError("cyclic sets cannot be canonicalized")
        active.add(identity)
        try:
            normalized = [_normalize(item, active=active) for item in value]
            normalized.sort(key=_normalized_sort_key)
            collection_type = "set" if type(value) is set else "frozenset"
            return {"t": collection_type, "v": normalized}
        finally:
            active.remove(identity)
    if type(value) in (list, tuple):
        identity = id(value)
        if identity in active:
            raise CanonicalEncodingError("cyclic sequences cannot be canonicalized")
        active.add(identity)
        try:
            collection_type = "list" if type(value) is list else "tuple"
            return {
                "t": collection_type,
                "v": [_normalize(item, active=active) for item in value],
            }
        finally:
            active.remove(identity)
    raise CanonicalEncodingError(
        f"unsupported canonical identity type: {type(value).__module__}.{type(value).__qualname__}"
    )


def _normalized_sort_key(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", errors="strict")


def _class_name(value: Any) -> str:
    value_type = type(value)
    return f"{value_type.__module__}.{value_type.__qualname__}"


def _reject_lone_surrogates(value: str) -> None:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise CanonicalEncodingError("lone Unicode surrogates cannot be canonicalized")
