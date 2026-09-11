from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .errors import SpenceError


def now():
    return datetime.now(UTC).isoformat()


def new_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
        raise SpenceError("invalid_id", "IDs must contain lowercase letters, numbers, and single hyphens.")
    return value


def text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise SpenceError("invalid_input", f"{field} must be a non-empty string.")
    return value


def email(value):
    text(value, "email")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        raise SpenceError("invalid_input", "Email address is invalid.")
    return value


def dumps(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else dumps(value).encode()).hexdigest()


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise SpenceError("invalid_input", f"Could not read JSON: {path}") from exc


def literal(value):
    if any(token in dumps(value) for token in ("{{", "{%", "{#")):
        raise SpenceError(
            "template_content",
            "Content contains template delimiters that EDSL may reinterpret.",
            "Remove the delimiters from the content supplied to the human survey.",
        )


def write_private(path, content, *, overwrite=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise SpenceError("already_exists", f"File already exists: {path}")
    raw = content if isinstance(content, bytes) else content.encode("utf-8")
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temp = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temp, path)
        else:
            os.link(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    return str(path.resolve())
