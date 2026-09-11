import copy
import json
import sqlite3

import pytest

from spence import forms
from spence.errors import SpenceError
from spence.provider import CoopProvider
from spence.report import application_report


def test_application_versions_stages_and_private_report(ats, application, publication, answers, tmp_path):
    first = ats.application(application["id"])
    ats.move(application["id"], "screening", "Relevant experience", expected=first["revision"])
    with pytest.raises(SpenceError, match="changed"):
        ats.move(application["id"], "offer", "Too soon", expected=first["revision"])
    ats.note(application["id"], "<script>bad()</script>")
    result = ats.sync("backend", "application")
    assert result[publication["id"]]["unchanged"] == 1
    assert len(ats.store.list("application_version")) == 1
    ats.remote.submit(
        publication["remote"]["uuid"],
        {**answers, "experience": "Updated experience"},
        response_id="response-1",
    )
    assert ats.sync("backend", "application")[publication["id"]]["updated"] == 1
    app = ats.application(application["id"])
    assert app["stage"] == "screening"
    assert first["version_id"] != app["version_id"]
    assert (
        ats.store.get("application_version", first["version_id"])["answers"]["experience"]
        == answers["experience"]
    )
    out = tmp_path / "report.html"
    application_report(ats, application["id"], out)
    assert "&lt;script&gt;" in out.read_text()
    assert "<script>" not in out.read_text()
    assert out.stat().st_mode & 0o777 == 0o600
    ats.move(application["id"], "rejected", "Role criteria mismatch")
    with pytest.raises(SpenceError, match="reopen"):
        ats.move(application["id"], "screening", "Reconsider")
    assert ats.move(application["id"], "screening", "New evidence", reopen=True)["stage"] == "screening"


def test_reviewer_workflow_packets_and_attribution(ats, application, publication, answers, tmp_path):
    batch = ats.prepare_review(application["id"], "rubric", ["alex", "sam"])
    assert set(batch["packet"]) == {"experience", "motivation"}
    assert "taylor@example.com" not in json.dumps(batch["scenario"])
    batch = ats.publish(batch["id"])
    sent = ats.send(batch["id"])
    ats.delivery_status(sent["operation_id"])
    assignments = [a for a in ats.store.list("assignment") if a["publication_id"] == batch["id"]]
    assert {a["delivery_state"] for a in assignments} == {"sent"}
    assert {a["state"] for a in assignments} == {"not_started"}
    with pytest.raises(SpenceError, match="remind"):
        ats.send(batch["id"])
    feedback = {
        "relationship": "Materials only",
        "strengths": "Relevant evidence",
        "concerns": "Need interview",
        "recommendation": "advance",
    }
    row = ats.remote.submit(batch["remote"]["uuid"], feedback, respondent_id=assignments[0]["respondent_id"])
    assert ats.sync("backend", "review")[batch["id"]]["created"] == 1
    assert ats.sync("backend", "review")[batch["id"]]["unchanged"] == 1
    changed = {**row, "respondent_id": assignments[1]["respondent_id"]}
    assert ats.import_rows(batch["id"], [changed], accept_revisions=True)["quarantined"] == 1
    assert len(ats.store.list("review_response")) == 1
    assert ats.store.get("assignment", assignments[1]["id"])["state"] == "not_started"
    ats.remote.submit(
        publication["remote"]["uuid"], {**answers, "experience": "New work"}, response_id="response-1"
    )
    ats.sync("backend", "application")
    assert ats.store.get("publication", batch["id"])["packet"] == batch["packet"]
    assert ats.application(application["id"])["version_id"] != batch["application_version_id"]
    with pytest.raises(SpenceError, match="completed"):
        ats.send(batch["id"], remind=True)
    ats.revoke(assignments[1]["id"])
    with pytest.raises(SpenceError, match="revoked"):
        ats.remote.submit(batch["remote"]["uuid"], feedback, respondent_id=assignments[1]["respondent_id"])


def test_invalid_rows_quarantine_and_export_revision_control(ats, publication, answers):
    row = ats.remote.submit(publication["remote"]["uuid"], answers, response_id="response-1")
    assert ats.import_rows(publication["id"], [row])["created"] == 1
    changed = {**row, "answers": {**answers, "experience": "New"}}
    assert ats.import_rows(publication["id"], [changed])["quarantined"] == 1
    assert ats.import_rows(publication["id"], [changed], accept_revisions=True)["updated"] == 1
    bad = [
        None,
        {**row, "response_id": None},
        {**row, "survey_id": "wrong"},
        {**row, "response_id": "incomplete", "answers": {"candidate_name": "No email"}},
        {**row, "response_id": "invalid-email", "answers": {**answers, "candidate_email": "nope"}},
        {**row, "submitted_at": "2026-01-01"},
    ]
    assert ats.import_rows(publication["id"], bad)["quarantined"] == len(bad)
    assert len(ats.store.list("application")) == 1
    assert len(ats.store.list("candidate")) == 1  # failed imports rolled back
    assert ats.import_rows(publication["id"], [row])["unchanged"] == 1
    assert (
        ats.application(ats.store.list("application")[0]["id"])["submission"]["answers"]["experience"]
        == "New"
    )


def test_duplicate_email_is_flagged_not_merged(ats, publication, answers):
    for id in ("one", "two"):
        ats.remote.submit(publication["remote"]["uuid"], answers, response_id=id)
    ats.sync("backend", "application")
    apps = ats.store.list("application")
    assert len(apps) == 2
    assert ats.application(apps[0]["id"])["possible_duplicate_candidates"] == [apps[1]["candidate_id"]]


def test_publish_timeout_durable_and_reconciled(ats, monkeypatch):
    pub = ats.prepare_application("backend", "advert", "short")
    original = ats.remote.publish
    remote = {}

    def timeout(*args):
        remote.update(original(*args))
        raise TimeoutError("after server accepted")

    monkeypatch.setattr(ats.remote, "publish", timeout)
    with pytest.raises(SpenceError, match="may have succeeded"):
        ats.publish(pub["id"])
    with pytest.raises(SpenceError, match="Reconcile"):
        ats.publish(pub["id"])
    assert len(ats.remote.read()["surveys"]) == 1
    operation = ats.store.list("operation")[0]
    assert operation["state"] == "outcome_unknown"
    assert ats.reconcile(operation["id"], remote["uuid"])["state"] == "open"
    assert ats.publish(pub["id"])["remote"]["uuid"] == remote["uuid"]


def test_send_timeout_durable_and_reconciled(ats, application, monkeypatch):
    batch = ats.publish(ats.prepare_review(application["id"], "rubric", ["alex"])["id"])
    original, remote = ats.remote.send, {}

    def timeout(*args):
        remote.update(original(*args))
        raise TimeoutError("accepted")

    monkeypatch.setattr(ats.remote, "send", timeout)
    with pytest.raises(SpenceError, match="may have started"):
        ats.send(batch["id"])
    with pytest.raises(SpenceError, match="Reconcile"):
        ats.send(batch["id"])
    op = next(o for o in ats.store.list("operation") if o["action"] == "send")
    ats.reconcile(op["id"], remote["delivery_uuid"])
    assert len(ats.remote.read()["deliveries"]) == 1
    with pytest.raises(SpenceError, match="remind"):
        ats.send(batch["id"])


def test_close_capability_failure_never_claims_closed(ats, publication):
    ats._remote = CoopProvider(client=object())
    with pytest.raises(SpenceError, match="closure"):
        ats.close_opening("backend")
    assert ats.store.get("opening", "backend")["state"] == "closing"
    assert ats.store.get("publication", publication["id"])["state"] == "open"
    with pytest.raises(SpenceError, match="closing"):
        ats.prepare_application("backend", "advert", "short")


def test_close_demo_and_late_import(ats, publication, answers):
    row = ats.remote.submit(publication["remote"]["uuid"], answers)
    assert ats.close_opening("backend")["state"] == "closed"
    assert ats.configure(publication["id"])["state"] == "closed"
    with pytest.raises(SpenceError, match="closed"):
        ats.remote.submit(publication["remote"]["uuid"], answers)
    late = {**row, "submitted_at": "2099-01-01T00:00:00+00:00"}
    ats.import_rows(publication["id"], [late])
    assert ats.store.list("application")[0]["intake"] == "late_or_unknown"


def test_revocation_and_deletion_are_honest(ats, application):
    batch = ats.publish(ats.prepare_review(application["id"], "rubric", ["alex"])["id"])
    assignment = ats.store.list("assignment")[0]
    ats._remote = CoopProvider(client=object())
    with pytest.raises(SpenceError, match="revocation"):
        ats.revoke(assignment["id"])
    assert ats.store.get("assignment", assignment["id"])["state"] == "revocation_pending"
    result = ats.deletion_request(application["id"], "Candidate requested removal")
    assert result["deleted"] is False
    assert ats.store.get("application", application["id"])["deletion_pending"] is True
    with pytest.raises(SpenceError, match="pending deletion"):
        ats.prepare_review(application["id"], "rubric", ["sam"])
    with pytest.raises(SpenceError, match="pending deletion"):
        ats.send(batch["id"])


def test_unpublished_revoked_batch_cannot_publish(ats, application):
    batch = ats.prepare_review(application["id"], "rubric", ["alex"])
    ats.revoke(ats.store.list("assignment")[0]["id"])
    with pytest.raises(SpenceError, match="fresh batch"):
        ats.publish(batch["id"])


def test_forms_snapshot_and_template_injection(ats, application):
    batch = ats.prepare_review(application["id"], "rubric", ["alex"])
    revised = copy.deepcopy(ats.store.get("review_template", "rubric"))
    revised["survey"]["questions"][0]["question_text"] = "Revised rubric"
    ats.save_design("review_template", "rubric", revised, revise=True)
    assert ats.store.get("publication", batch["id"])["form"]["survey"] != revised["survey"]
    with pytest.raises(SpenceError, match="template"):
        forms.make_jobs(ats.store.get("form", "short"), {"job_post": "{{ secret }}"})


def test_sqlite_backup_is_readable_and_private(ats, application, tmp_path):
    path = tmp_path / "backup.sqlite3"
    ats.store.backup(path)
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from records where kind='application'").fetchone()[0] == 1
        assert db.execute("pragma integrity_check").fetchone()[0] == "ok"
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(SpenceError):
        ats.store.backup(path)


def test_unknown_publication_blocks_closure_until_reconciled(ats, monkeypatch):
    pub = ats.prepare_application("backend", "advert", "short")
    remote, original = {}, ats.remote.publish

    def timeout(*args):
        remote.update(original(*args))
        raise TimeoutError("accepted")

    monkeypatch.setattr(ats.remote, "publish", timeout)
    with pytest.raises(SpenceError):
        ats.publish(pub["id"])
    with pytest.raises(SpenceError, match="Reconcile"):
        ats.close_opening("backend")
    assert ats.store.get("opening", "backend")["state"] == "closing"
    operation = ats.store.list("operation")[0]
    ats.reconcile(operation["id"], remote["uuid"])
    assert ats.close_opening("backend")["state"] == "closed"
    assert ats.remote.read()["surveys"][remote["uuid"]]["closed"]


def test_operator_verified_absence_allows_retry(ats, monkeypatch):
    pub = ats.prepare_application("backend", "advert", "short")
    original = ats.remote.publish

    def timeout(*args):
        raise TimeoutError("not created")

    monkeypatch.setattr(ats.remote, "publish", timeout)
    with pytest.raises(SpenceError):
        ats.publish(pub["id"])
    op = ats.store.list("operation")[0]
    with pytest.raises(SpenceError):
        ats.resolve_not_created(op["id"], "")
    ats.resolve_not_created(op["id"], "Provider audit confirmed no created survey.")
    monkeypatch.setattr(ats.remote, "publish", original)
    assert ats.publish(pub["id"])["state"] == "open"
    assert len(ats.remote.read()["surveys"]) == 1


def test_review_preparation_is_atomic(ats, application, monkeypatch):
    original = ats.store.put

    def fail_assignment(kind, *args, **kwargs):
        if kind == "assignment":
            raise RuntimeError("storage failure")
        return original(kind, *args, **kwargs)

    monkeypatch.setattr(ats.store, "put", fail_assignment)
    with pytest.raises(RuntimeError, match="storage failure"):
        ats.prepare_review(application["id"], "rubric", ["alex"])
    assert len(ats.store.list("publication")) == 1
    assert not ats.store.list("assignment")
    assert len(list((ats.store.private / "artifacts").glob("*.ep"))) == 1


def test_reviews_continue_after_intake_closes(ats, application):
    ats.close_opening("backend")
    batch = ats.prepare_review(application["id"], "rubric", ["alex"])
    assert ats.publish(batch["id"])["state"] == "open"
    assert ats.send(batch["id"])["delivery_uuid"]
    assert ats.store.get("opening", "backend")["state"] == "closed"
