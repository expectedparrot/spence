"""Transactional private records with immutable versions and explicit relationships."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from contextlib import closing, contextmanager
from pathlib import Path

from .errors import SpenceError
from .util import dumps, identifier, new_id, now, read_json, write_private

SCHEMA = """
CREATE TABLE records (
 kind TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL,
 data TEXT NOT NULL, PRIMARY KEY(kind,id)
);
CREATE TABLE versions (
 kind TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL, data TEXT NOT NULL,
 PRIMARY KEY(kind,id,revision), FOREIGN KEY(kind,id) REFERENCES records(kind,id) ON DELETE CASCADE
);
CREATE TABLE refs (
 owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL, label TEXT NOT NULL,
 target_kind TEXT NOT NULL, target_id TEXT NOT NULL,
 PRIMARY KEY(owner_kind,owner_id,label,target_kind,target_id),
 FOREIGN KEY(owner_kind,owner_id) REFERENCES records(kind,id) ON DELETE CASCADE,
 FOREIGN KEY(target_kind,target_id) REFERENCES records(kind,id)
);
CREATE TABLE source_versions (
 account TEXT NOT NULL, survey_id TEXT NOT NULL, response_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
 object_kind TEXT NOT NULL, object_id TEXT NOT NULL, imported_at TEXT NOT NULL,
 PRIMARY KEY(account,survey_id,response_id,payload_hash),
 FOREIGN KEY(object_kind,object_id) REFERENCES records(kind,id) ON DELETE CASCADE
);
CREATE TABLE events (
 id TEXT PRIMARY KEY, time TEXT NOT NULL, actor TEXT NOT NULL,
 event TEXT NOT NULL, object_kind TEXT, object_id TEXT, details TEXT NOT NULL
);
PRAGMA user_version=1;
"""


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        config = read_json(self.root / "spence.json")
        if not isinstance(config, dict) or config.get("schema_version") != 1:
            raise SpenceError("unsupported_schema", "Unsupported workspace format.")
        self.config = config
        self.private = self.root / ".spence-private"
        if self.private.is_symlink():
            raise SpenceError("unsafe_path", "Private workspace cannot be a symlink.")
        self._check_tracking()
        db_path = self.private / "ats.sqlite3"
        if not db_path.is_file() or db_path.is_symlink():
            raise SpenceError("invalid_workspace", "Missing or unsafe ATS database.")
        self.db = sqlite3.connect(db_path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA secure_delete=ON")
        if self.db.execute("PRAGMA user_version").fetchone()[0] != 1:
            self.db.close()
            raise SpenceError("unsupported_schema", "Unsupported database schema; no data was modified.")

    def _check_tracking(self):
        try:
            result = subprocess.run(
                ["git", "-C", str(self.root), "ls-files", "--", ".spence-private"],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            return
        if result.returncode == 0 and result.stdout.strip():
            raise SpenceError(
                "private_data_tracked", "Private ATS data is tracked by Git; remove it from tracking first."
            )

    @classmethod
    def initialize(cls, root, *, provider="coop", account="default", owner="operator"):
        root = Path(root).resolve()
        if (root / "spence.json").exists() or (root / ".spence-private").exists():
            raise SpenceError("already_exists", "Workspace or private directory already exists.")
        if provider not in ("coop", "demo"):
            raise SpenceError("invalid_input", "Provider must be coop or demo.")
        root.mkdir(parents=True, exist_ok=True)
        private = root / ".spence-private"
        private.mkdir(mode=0o700)
        db_path = private / "ats.sqlite3"
        fd = os.open(db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(db_path)) as db:
            db.executescript(SCHEMA)
        ignore = root / ".gitignore"
        existing = ignore.read_text() if ignore.exists() else ""
        write_private(ignore, existing.rstrip() + "\n.spence-private/\n.env\n", overwrite=True)
        write_private(
            root / "spence.json",
            dumps(
                {
                    "schema_version": 1,
                    "id": new_id("workspace"),
                    "provider": provider,
                    "account": account,
                    "owner": owner,
                }
            )
            + "\n",
        )
        return cls(root)

    @classmethod
    def open(cls, start=None):
        root = Path(start or Path.cwd()).resolve()
        for candidate in (root, *root.parents):
            if (candidate / "spence.json").is_file():
                return cls(candidate)
        raise SpenceError("project_not_found", "Run spence init PATH first.")

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def get(self, kind, id, revision=None):
        identifier(id)
        if revision is None:
            row = self.db.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, id)).fetchone()
        else:
            row = self.db.execute(
                "SELECT data FROM versions WHERE kind=? AND id=? AND revision=?", (kind, id, revision)
            ).fetchone()
        if row is None:
            raise SpenceError("not_found", f"{kind} {id} not found.")
        return json.loads(row[0])

    def list(self, kind):
        return [
            json.loads(row[0])
            for row in self.db.execute("SELECT data FROM records WHERE kind=? ORDER BY id", (kind,))
        ]

    def put(self, kind, id, value, *, expected=None, refs=()):
        if not self.db.in_transaction:
            raise RuntimeError("Writes require a transaction")
        identifier(id)
        row = self.db.execute("SELECT revision FROM records WHERE kind=? AND id=?", (kind, id)).fetchone()
        if (row is not None and expected is None) or (
            expected is not None and (row is None or row[0] != expected)
        ):
            raise SpenceError("conflict", f"{kind} {id} already exists or changed; reload before retrying.")
        revision = 1 if row is None else row[0] + 1
        record = {**value, "id": id, "revision": revision, "updated_at": now()}
        payload = dumps(record)
        self.db.execute(
            "INSERT INTO records VALUES(?,?,?,?) ON CONFLICT(kind,id) "
            "DO UPDATE SET revision=excluded.revision,data=excluded.data",
            (kind, id, revision, payload),
        )
        self.db.execute("INSERT INTO versions VALUES(?,?,?,?)", (kind, id, revision, payload))
        for label, target_kind, target_id in refs:
            self.db.execute(
                "INSERT OR IGNORE INTO refs VALUES(?,?,?,?,?)", (kind, id, label, target_kind, target_id)
            )
        return record

    def event(self, event, kind=None, id=None, **details):
        if not self.db.in_transaction:
            raise RuntimeError("Events require a transaction")
        self.db.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?,?)",
            (new_id("event"), now(), self.config["owner"], event, kind, id, dumps(details)),
        )

    def backup(self, path):
        path = Path(path)
        if path.exists():
            raise SpenceError("already_exists", "Backup destination exists.")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(path)) as target:
            self.db.backup(target)
        return {
            "path": str(path.resolve()),
            "sensitive": True,
            "scope": "database; copy private artifacts separately",
        }
