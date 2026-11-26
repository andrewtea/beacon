"""Replay cache: recorded Ember responses keyed by a hash of the request.

A request that was answered once is answered from disk afterwards, so reruns cost $0.
Each entry is one JSON file, `<request hash>.json`. Entries hold the request with
media replaced by a digest, so a recorded file never contains audio or an image.
"""

import fcntl
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from beacon.ember.errors import EmberError


def request_hash(body: Mapping[str, Any]) -> str:
    """SHA-256 of the request body in a canonical JSON form. Any change to the request,
    including the bytes of an inline clip or image, gives a different hash."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


# Fields whose string value is media or opaque state as a whole: inline audio
# (`input_audio.data`), inline files (`file_data`), and replayed reasoning. Fields ending
# in `_url` (`image_url`, `video_url`, `audio_url`, `file_url`) are treated the same way.
_OPAQUE_KEYS = frozenset({"data", "file_data", "encrypted_content"})
# A data URL inside a longer string. Its payload can be wrapped over several lines, so
# where it ends cannot be told from the text after it: everything from the payload to
# the end of the string is replaced.
_DATA_URL = re.compile(
    r"((?<!\w)data:(?:[\w.+-]+/[\w.+-]+(?:;[\w.+=-]+)*|(?:;[\w.+=-]+)+),)(.+)",
    re.IGNORECASE | re.DOTALL,
)
# Bare base64: one long unbroken run, or wrapped lines. Prose and identifiers hold neither.
_BASE64 = re.compile(
    r"[A-Za-z0-9+/_-]{256,}={0,2}|(?:[A-Za-z0-9+/_-]{60,}\s+){2,}[A-Za-z0-9+/_-]+={0,2}"
)


def redact(value: Any) -> Any:
    """Copy of `value` with media replaced by its length and digest, so radio audio and
    photos stay out of the ledger, the trace and the replay files. The digest still
    shows which clip or image a request carried."""
    return _redact(value, None)


def _redact(value: Any, key: str | None) -> Any:
    if isinstance(value, str):
        if key is not None and (key in _OPAQUE_KEYS or key.endswith("_url")):
            return _redact_whole(value)
        # A tool result or a tool call's arguments is JSON in a string, and can hold
        # the same media fields a request does.
        nested = _parse_json(value)
        if nested is not None:
            redacted = _redact(nested, None)
            return value if redacted == nested else json.dumps(redacted, ensure_ascii=False)
        value = _DATA_URL.sub(lambda match: match[1] + _digest(match[2]), value, count=1)
        return _BASE64.sub(lambda match: _digest(match[0]), value)
    if isinstance(value, Mapping):
        return {name: _redact(item, name) for name, item in value.items()}
    if isinstance(value, list | tuple):
        return [_redact(item, key) for item in value]
    return value


def _parse_json(value: str) -> dict[str, Any] | list[Any] | None:
    if not value.lstrip().startswith(("{", "[")):
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict | list) else None


def _redact_whole(value: str) -> str:
    if value[:5].lower() == "data:":
        header, comma, payload = value.partition(",")
        if comma:
            return f"{header},{_digest(payload)}"
    return _digest(value)


def _digest(payload: str) -> str:
    return f"<redacted {len(payload)} chars, sha256 {hashlib.sha256(payload.encode()).hexdigest()}>"


@dataclass(frozen=True)
class Recording:
    response: dict[str, Any]
    # How long the live call took. A replay reports it, so a rerun measures the same latency.
    latency_ms: int


class ReplayCache:
    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def path(self, key: str) -> Path:
        return self._directory / f"{key}.json"

    @contextmanager
    def lock(self, key: str) -> Iterator[None]:
        """Hold the lock for one request, waiting while another thread or process holds
        it. A caller that misses the cache takes the lock, looks again, and only then
        calls the API, so the same request is never paid for twice at once."""
        locks = self._directory / ".locks"
        locks.mkdir(parents=True, exist_ok=True)
        # flock belongs to the open file, so each caller opens its own. The lock goes
        # when the file closes, including when the process dies.
        with open(locks / f"{key}.lock", "ab") as file:
            fcntl.flock(file, fcntl.LOCK_EX)
            yield

    def get(self, key: str) -> Recording | None:
        try:
            text = self.path(key).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            entry = json.loads(text)
            return Recording(response=entry["response"], latency_ms=entry["latency_ms"])
        except (ValueError, KeyError, TypeError) as error:
            raise EmberError(f"The recording {self.path(key)} is unreadable: {error!r}") from error

    def put(
        self, key: str, *, request: Mapping[str, Any], response: Mapping[str, Any], latency_ms: int
    ) -> None:
        """`request` must already be redacted. It is stored for people reading the file;
        lookups use only the key."""
        entry = {
            "request_hash": key,
            "latency_ms": latency_ms,
            "request": request,
            "response": response,
        }
        self._directory.mkdir(parents=True, exist_ok=True)
        # Write beside the target and rename, so a reader never sees a partial file.
        handle, temporary = tempfile.mkstemp(dir=self._directory, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump(entry, file, indent=2, sort_keys=True)
                file.write("\n")
            os.replace(temporary, self.path(key))
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
