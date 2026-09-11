import hashlib
import json
import os
import subprocess

import pytest

from spence.cli import main
from spence.errors import SpenceError
from spence.store import Store
from spence.util import write_private


def invoke(capsys, *args, success=True):
    code = main(list(map(str, args)))
    data = json.loads(capsys.readouterr().out)
    assert code == (0 if success else 1), data
    assert data["ok"] is success
    return data


def test_cli_post_application_export_and_errors(tmp_path, monkeypatch, capsys):
    root = tmp_path / "workspace"
    invoke(capsys, "init", root, "--provider", "demo")
    monkeypatch.chdir(root)
    role = root / "role.json"
    role.write_text(json.dumps({"id": "engineer", "title": "Engineer"}))
    invoke(capsys, "role", "import", role)
    post = root / "post.json"
    body = "# Engineer <script>bad</script>"
    post.write_text(
        json.dumps({"id": "advert", "text": body, "sha256": hashlib.sha256(body.encode()).hexdigest()})
    )
    invoke(capsys, "post", "import", post, "--mccall-export")
    post.write_text(json.dumps({"id": "bad", "text": body, "sha256": "wrong"}))
    assert (
        invoke(capsys, "post", "import", post, "--mccall-export", success=False)["errors"][0]["code"]
        == "integrity_error"
    )
    invoke(capsys, "application-form", "init", "--id", "short")
    invoke(capsys, "application-form", "validate", "short")
    invoke(capsys, "opening", "create", "--id", "backend", "--role", "engineer")
    invoke(capsys, "opening", "add-post", "backend", "--post", "advert")
    pub = invoke(capsys, "opening", "publish", "backend", "--post", "advert", "--form", "short")[
        "publication"
    ]["id"]
    output = root / "advert.html"
    invoke(
        capsys,
        "opening",
        "export-post",
        "backend",
        "--publication",
        pub,
        "--format",
        "html",
        "--output",
        output,
    )
    assert "&lt;script&gt;" in output.read_text()
    assert "https://demo.invalid/apply/" in output.read_text()
    invoke(
        capsys, "opening", "export-post", "backend", "--publication", pub, "--output", output, success=False
    )
    assert invoke(capsys, "capabilities")["capabilities"]["demo_only"]
    invoke(capsys, "opening", "show", "missing", success=False)


def test_roster_import_rolls_back_on_conflict(ats, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(ats.store.root)
    roster = tmp_path / "reviewers.json"
    roster.write_text(
        json.dumps(
            [
                {"id": "new", "name": "New", "email": "new@example.com"},
                {"id": "alex", "name": "Alex", "email": "alex@example.com"},
            ]
        )
    )
    invoke(capsys, "reviewer", "import", roster, success=False)
    assert {r["id"] for r in ats.store.list("reviewer")} == {"alex", "sam"}


def test_store_permissions_schema_and_tracking(tmp_path):
    root = tmp_path / "workspace"
    store = Store.initialize(root, provider="demo")
    assert store.private.stat().st_mode & 0o777 == 0o700
    assert (store.private / "ats.sqlite3").stat().st_mode & 0o777 == 0o600
    store.close()
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "add", "-f", ".spence-private/ats.sqlite3"], check=True, capture_output=True
    )
    with pytest.raises(SpenceError, match="tracked"):
        Store(root)
    with pytest.raises(SpenceError, match="already exists"):
        Store.initialize(root)


def test_atomic_private_write_refuses_overwrite_and_symlink(tmp_path):
    path = tmp_path / "private.json"
    write_private(path, "original")
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(SpenceError):
        write_private(link, "changed")
    assert path.read_text() == "original"
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(SpenceError):
        write_private(path, "changed")


def test_database_transaction_rolls_back_and_revisions_are_immutable(ats):
    old = ats.store.get("role", "engineer")
    with pytest.raises(RuntimeError), ats.store.transaction():
        ats.store.put("role", "engineer", {"title": "Changed"}, expected=old["revision"])
        raise RuntimeError("rollback")
    assert ats.store.get("role", "engineer") == old
    with ats.store.transaction():
        ats.store.put("role", "engineer", {"title": "Changed"}, expected=old["revision"])
    assert ats.store.get("role", "engineer", old["revision"]) == old
    assert ats.store.db.execute("pragma foreign_key_check").fetchall() == []


def test_demo_script_uses_installed_cli(tmp_path):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "examples" / "demo.py"
    spec = importlib.util.spec_from_file_location("spence_demo", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Subprocesses need the same source tree when testing an uninstalled checkout.
    import spence

    old = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = str(Path(spence.__file__).resolve().parents[1])
    try:
        result = module.demonstrate(tmp_path / "demo")
    finally:
        if old is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = old
    assert result["demo_only"]
    assert Path(result["report"]).is_file()
