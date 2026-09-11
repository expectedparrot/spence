"""Hiring workflows. Remote side effects are preceded by durable operation records."""

from __future__ import annotations

import html

from . import forms
from .errors import SpenceError
from .provider import provider
from .util import digest, dumps, email, new_id, now, text, write_private

STAGES = ["submitted", "screening", "review", "interview", "offer", "hired", "rejected", "withdrawn"]
TERMINAL = {"hired", "rejected", "withdrawn"}


class Service:
    def __init__(self, store, remote=None):
        self.store = store
        self._remote = remote

    @property
    def remote(self):
        if self._remote is None:
            self._remote = provider(self.store)
        return self._remote

    def save_design(self, kind, id, value, *, revise=False):
        old = self.store.get(kind, id) if revise else None
        with self.store.transaction():
            result = self.store.put(kind, id, value, expected=old["revision"] if old else None)
            self.store.event("design_saved", kind, id, hash=digest(value))
        return result

    def create_opening(self, id, role_id, title=None):
        role = self.store.get("role", role_id)
        with self.store.transaction():
            opening = self.store.put(
                "opening",
                id,
                {
                    "title": title or role["title"],
                    "role": role,
                    "posts": [],
                    "state": "draft",
                    "stages": STAGES,
                },
                refs=[("role", "role", role_id)],
            )
            self.store.event("opening_created", "opening", id)
        return opening

    def add_post(self, opening_id, post_id):
        opening, post = self.store.get("opening", opening_id), self.store.get("post", post_id)
        if opening["state"] in ("closing", "closed"):
            raise SpenceError("closed", "Cannot add a post to a closing/closed opening.")
        posts = [p for p in opening["posts"] if p["id"] != post_id] + [post]
        with self.store.transaction():
            return self.store.put(
                "opening",
                opening_id,
                {**opening, "posts": posts},
                expected=opening["revision"],
                refs=[("post", "post", post_id)],
            )

    def prepare_application(self, opening_id, post_id, form_id):
        opening, form = self.store.get("opening", opening_id), self.store.get("form", form_id)
        if opening["state"] in ("closing", "closed"):
            raise SpenceError("closed", "Opening is closing or closed.")
        post = next((p for p in opening["posts"] if p["id"] == post_id), None)
        if post is None:
            raise SpenceError("not_found", "Attach this post to the opening first.")
        id = "pub-" + digest([opening_id, post, form])[:20]
        try:
            return self.store.get("publication", id)
        except SpenceError as exc:
            if exc.code != "not_found":
                raise
        scenario = {
            "opening_id": opening_id,
            "publication_id": id,
            "post_id": post_id,
            "job_post": post["text"],
            "opening_title": opening["title"],
        }
        value = {
            "kind": "application",
            "opening_id": opening_id,
            "post": post,
            "form": form,
            "scenario": scenario,
            "state": "prepared",
            "reviewers": [],
            "created_at": now(),
        }
        return self._prepare(id, value, [("opening", "opening", opening_id), ("form", "form", form_id)])

    def _prepare(self, id, value, refs, assignments=()):
        jobs = forms.make_jobs(value["form"], value["scenario"], value.get("reviewers", []))
        artifact = self.store.private / "artifacts" / f"{id}.jobs.ep"
        info = forms.save_jobs(jobs, artifact)
        try:
            with self.store.transaction():
                record = self.store.put(
                    "publication",
                    id,
                    {
                        **value,
                        "artifact": {
                            "path": str(artifact.relative_to(self.store.private)),
                            "sha256": info["sha256"],
                        },
                    },
                    refs=refs,
                )
                for reviewer in assignments:
                    self.store.put(
                        "assignment",
                        new_id("assignment"),
                        {
                            "publication_id": id,
                            "application_id": value["application_id"],
                            "application_version_id": value["application_version_id"],
                            "reviewer_id": reviewer["id"],
                            "state": "not_started",
                            "delivery_state": "not_sent",
                        },
                        refs=[("publication", "publication", id), ("reviewer", "reviewer", reviewer["id"])],
                    )
                self.store.event("publication_prepared", "publication", id)
            return record
        except BaseException:
            artifact.unlink(missing_ok=True)
            raise

    def _operation(self, action, publication_id):
        # One unresolved operation for a publication/action. Unknown outcomes must be reconciled before a retry.
        with self.store.transaction():
            unresolved = [
                o
                for o in self.store.list("operation")
                if o["action"] == action
                and o["publication_id"] == publication_id
                and o["state"] in ("pending", "outcome_unknown")
            ]
            if unresolved:
                raise SpenceError(
                    "outcome_unknown", f"Reconcile operation {unresolved[0]['id']} before retrying."
                )
            op = self.store.put(
                "operation",
                new_id("op"),
                {"action": action, "publication_id": publication_id, "state": "pending", "started_at": now()},
                refs=[("publication", "publication", publication_id)],
            )
            self.store.event("operation_started", "operation", op["id"], action=action)
        return op

    def _finish_operation(self, operation, state, **details):
        with self.store.transaction():
            current = self.store.get("operation", operation["id"])
            return self.store.put(
                "operation",
                current["id"],
                {**current, "state": state, **details},
                expected=current["revision"],
            )

    def publish(self, publication_id):
        pub = self.store.get("publication", publication_id)
        if pub["state"] in ("open", "closed"):
            return pub
        opening = self.store.get("opening", pub["opening_id"])
        if pub["kind"] == "application" and opening["state"] in ("closing", "closed"):
            raise SpenceError("closed", "Opening is closing or closed.")
        if pub.get("remote"):
            return self.configure(publication_id)
        if any(
            a["state"] in ("revoked", "revocation_pending")
            for a in self.store.list("assignment")
            if a["publication_id"] == publication_id
        ):
            raise SpenceError("revoked", "Prepare a fresh batch after removing a reviewer.")
        if pub.get("application_id"):
            self._require_active(pub["application_id"])
        jobs = forms.make_jobs(pub["form"], pub["scenario"], pub["reviewers"])
        operation = self._operation("publish", publication_id)
        try:
            remote = self.remote.publish(jobs, pub)
            if not isinstance(remote, dict) or not remote.get("uuid") or not remote.get("respondent_url"):
                raise ValueError("Provider publication response is missing its identity or URL")
            with self.store.transaction():
                current = self.store.get("publication", publication_id)
                self.store.put(
                    "publication",
                    publication_id,
                    {**current, "remote": remote, "state": "configuring"},
                    expected=current["revision"],
                )
            self._finish_operation(operation, "confirmed", remote_id=remote["uuid"])
        except Exception as exc:
            self._finish_operation(operation, "outcome_unknown", error_type=type(exc).__name__)
            raise SpenceError(
                "outcome_unknown", f"Publication may have succeeded; reconcile {operation['id']}."
            ) from exc
        return self.configure(publication_id)

    def configure(self, publication_id):
        pub = self.store.get("publication", publication_id)
        if pub["state"] in ("open", "closed"):
            return pub
        if pub["kind"] == "application" and self.store.get("opening", pub["opening_id"])["state"] in (
            "closing",
            "closed",
        ):
            raise SpenceError("closed", "Opening is closing or closed; configuration cannot reopen it.")
        if any(
            a["state"] in ("revoked", "revocation_pending")
            for a in self.store.list("assignment")
            if a["publication_id"] == publication_id
        ):
            raise SpenceError("revoked", "Prepare a fresh batch after removing a reviewer.")
        if not pub.get("remote"):
            raise SpenceError("invalid_state", "Publication has no confirmed provider identity.")
        if pub["kind"] == "review":
            links = self.remote.configure(pub)
            by_reviewer = {}
            for row in links:
                reviewer_id = row.get("reviewer_id")
                if reviewer_id in by_reviewer or not row.get("respondent_uuid") or not row.get("url"):
                    raise SpenceError(
                        "invalid_roster", "Duplicate or incomplete personal reviewer link mapping."
                    )
                by_reviewer[reviewer_id] = row
            if set(by_reviewer) != {r["id"] for r in pub["reviewers"]}:
                raise SpenceError(
                    "invalid_roster", "Provider reviewer roster does not match the prepared batch."
                )
            if len({r["respondent_uuid"] for r in links}) != len(links):
                raise SpenceError("invalid_roster", "Provider respondents are not unique.")
        with self.store.transaction():
            if pub["kind"] == "review":
                for assignment in self.store.list("assignment"):
                    if assignment["publication_id"] != publication_id:
                        continue
                    link = by_reviewer[assignment["reviewer_id"]]
                    self.store.put(
                        "assignment",
                        assignment["id"],
                        {**assignment, "respondent_id": link["respondent_uuid"], "url": link["url"]},
                        expected=assignment["revision"],
                    )
            current = self.store.get("publication", publication_id)
            current = self.store.put(
                "publication", publication_id, {**current, "state": "open"}, expected=current["revision"]
            )
            opening = self.store.get("opening", pub["opening_id"])
            if opening["state"] == "draft":
                self.store.put(
                    "opening", opening["id"], {**opening, "state": "open"}, expected=opening["revision"]
                )
            self.store.event("publication_opened", "publication", publication_id)
        return current

    def reconcile(self, operation_id, remote_id):
        operation = self.store.get("operation", operation_id)
        if operation["state"] not in ("pending", "outcome_unknown"):
            raise SpenceError("invalid_state", "Operation is already resolved.")
        pub = self.store.get("publication", operation["publication_id"])
        if operation["action"] == "publish":
            remote = self.remote.verify(pub, remote_id)
            with self.store.transaction():
                self.store.put(
                    "publication",
                    pub["id"],
                    {**pub, "remote": remote, "state": "configuring"},
                    expected=pub["revision"],
                )
            self._finish_operation(operation, "confirmed", remote_id=remote_id)
            if pub["kind"] == "application" and self.store.get("opening", pub["opening_id"])["state"] in (
                "closing",
                "closed",
            ):
                return self.store.get("publication", pub["id"])
            return self.configure(pub["id"])
        if operation["action"] == "send":
            delivery = self.remote.delivery(pub, remote_id)
            self._finish_operation(operation, "confirmed", delivery_id=remote_id, delivery=delivery)
            return delivery
        raise SpenceError("invalid_state", "This operation cannot be manually reconciled.")

    def resolve_not_created(self, operation_id, reason):
        """Record an operator's verified absence of a remote side effect; never infer it from a timeout."""
        text(reason, "provider verification evidence")
        with self.store.transaction():
            op = self.store.get("operation", operation_id)
            if op["state"] not in ("pending", "outcome_unknown"):
                raise SpenceError("invalid_state", "Operation is already resolved.")
            pub = self.store.get("publication", op["publication_id"])
            if op["action"] == "publish" and pub.get("remote"):
                raise SpenceError(
                    "invalid_state", "A provider publication is already recorded; reconcile it instead."
                )
            resolved = self.store.put(
                "operation",
                operation_id,
                {**op, "state": "confirmed_not_created", "reason": reason},
                expected=op["revision"],
            )
            self.store.event("remote_absence_confirmed_by_operator", "operation", operation_id, reason=reason)
        return resolved

    def export_post(self, publication_id, path, format="markdown"):
        pub = self.store.get("publication", publication_id)
        if pub["kind"] != "application" or pub["state"] != "open":
            raise SpenceError("invalid_state", "An open application publication is required.")
        url = pub["remote"]["respondent_url"]
        if not url.startswith("https://") or any(c in url for c in ("\n", "\r", '"', "<", ">")):
            raise SpenceError("invalid_url", "Provider did not return a safe HTTPS Apply URL.")
        if format == "html":
            output = f'<!doctype html><meta charset="utf-8"><title>Apply</title><pre>{html.escape(pub["post"]["text"])}</pre>'
            output += f'<a href="{html.escape(url, quote=True)}">Apply</a>'
        else:
            output = pub["post"]["text"].rstrip() + f"\n\n[Apply](<{url}>)\n"
        return {"path": write_private(path, output), "publication_id": publication_id, "apply_url": url}

    def import_rows(self, publication_id, rows, *, accept_revisions=False, trusted_sync=False):
        pub = self.store.get("publication", publication_id)
        if not pub.get("remote") or pub["state"] not in ("open", "closed", "closing"):
            raise SpenceError("invalid_state", "Publish and configure the form before importing responses.")
        counts = {"created": 0, "updated": 0, "unchanged": 0, "quarantined": 0}
        for row in rows:
            try:
                with self.store.transaction():
                    action = self._import_row(pub, row, accept_revisions or trusted_sync)
                counts[action] += 1
            except SpenceError as exc:
                key = "q-" + digest([publication_id, row])[:24]
                with self.store.transaction():
                    existing = next((q for q in self.store.list("quarantine") if q["id"] == key), None)
                    self.store.put(
                        "quarantine",
                        key,
                        {
                            "publication_id": publication_id,
                            "row": row,
                            "error": exc.to_dict(),
                            "state": "pending",
                        },
                        expected=existing["revision"] if existing else None,
                        refs=[("publication", "publication", publication_id)],
                    )
                counts["quarantined"] += 1
        with self.store.transaction():
            current = self.store.get("publication", publication_id)
            self.store.put(
                "publication",
                publication_id,
                {**current, "last_sync_at": now(), "last_sync": counts},
                expected=current["revision"],
            )
            self.store.event("responses_imported", "publication", publication_id, **counts)
        return counts

    def _import_row(self, pub, row, accept_revisions):
        if not isinstance(row, dict) or row.get("error"):
            raise SpenceError("invalid_response", "Malformed response envelope.")
        response_id = text(row.get("response_id"), "provider response ID")
        if row.get("survey_id") != pub["remote"]["uuid"]:
            raise SpenceError("source_mismatch", "Response belongs to a different publication.")
        if row.get("submitted_at") is not None:
            from datetime import datetime

            try:
                stamp = datetime.fromisoformat(row["submitted_at"])
                if stamp.tzinfo is None:
                    raise ValueError("missing timezone")
            except (ValueError, TypeError, AttributeError) as exc:
                raise SpenceError("invalid_response", "Submission timestamp must be timezone-aware.") from exc
        hash = digest(
            {key: row.get(key) for key in ("answers", "respondent_id", "submitted_at", "provider_revision")}
        )
        source = (self.store.config["account"], row["survey_id"], response_id)
        matches = self.store.db.execute(
            "SELECT * FROM source_versions WHERE account=? AND survey_id=? AND response_id=?", source
        ).fetchall()
        if any(match["payload_hash"] == hash for match in matches):
            return "unchanged"
        if matches and not accept_revisions:
            raise SpenceError(
                "revision_needs_review", "Changed response from an export needs --accept-revisions."
            )
        answers = forms.validate_answers(pub["form"], row.get("answers"))
        source_record = {
            "response_id": response_id,
            "survey_id": row["survey_id"],
            "payload_hash": hash,
            "submitted_at": row.get("submitted_at"),
            "source": row,
            "answers": answers,
            "publication_id": pub["id"],
        }
        if pub["kind"] == "application":
            contacts = {
                destination: answers.get(name) for name, destination in pub["form"]["mapping"].items()
            }
            name = text(contacts.get("contact.name"), "candidate name")
            address = email(contacts.get("contact.email"))
            old = self.store.get("application", matches[0]["object_id"]) if matches else None
            if old and old.get("deletion_pending"):
                raise SpenceError(
                    "deleted", "Application is pending deletion; it cannot be recreated by sync."
                )
            app_id = old["id"] if old else new_id("app")
            candidate_id = old["candidate_id"] if old else new_id("candidate")
            candidate = self.store.get("candidate", candidate_id) if old else None
            self.store.put(
                "candidate",
                candidate_id,
                {"name": name, "email": address, "identity_verified": False},
                expected=candidate["revision"] if candidate else None,
            )
            version = self.store.put(
                "application_version",
                new_id("submission"),
                {
                    **source_record,
                    "application_id": app_id,
                    "evidence_kind": "human_application",
                    "attachments": {k: v for k, v in contacts.items() if k.startswith("attachments.")},
                },
                refs=[("publication", "publication", pub["id"])],
            )
            opening = self.store.get("opening", pub["opening_id"])
            cutoff = opening.get("cutoff")
            intake = "received"
            if cutoff:
                intake = (
                    "late_or_unknown"
                    if not row.get("submitted_at") or stamp > datetime.fromisoformat(cutoff)
                    else "received"
                )
            app = {
                **(old or {}),
                "candidate_id": candidate_id,
                "opening_id": pub["opening_id"],
                "publication_id": pub["id"],
                "version_id": version["id"],
                "stage": old["stage"] if old else "submitted",
                "intake": intake,
                "submitted_at": row.get("submitted_at"),
                "deletion_pending": False,
            }
            obj = self.store.put(
                "application",
                app_id,
                app,
                expected=old["revision"] if old else None,
                refs=[
                    ("candidate", "candidate", candidate_id),
                    ("opening", "opening", pub["opening_id"]),
                    ("version", "application_version", version["id"]),
                ],
            )
            object_kind = "application"
        else:
            respondent = row.get("respondent_id")
            assignments = [
                a
                for a in self.store.list("assignment")
                if a["publication_id"] == pub["id"] and a.get("respondent_id") == respondent
            ]
            if not respondent or len(assignments) != 1:
                raise SpenceError(
                    "unattributed_review", "Review lacks a unique provider respondent/assignment mapping."
                )
            assignment = assignments[0]
            if assignment["state"] in ("revoked", "revocation_pending"):
                raise SpenceError("revoked", "Review assignment is revoked or awaiting revocation.")
            self._require_active(assignment["application_id"])
            old = self.store.get("review_response", matches[0]["object_id"]) if matches else None
            if old and old["assignment_id"] != assignment["id"]:
                raise SpenceError(
                    "source_mismatch", "A response revision cannot change its reviewer assignment."
                )
            if not old and assignment.get("response_id"):
                raise SpenceError(
                    "duplicate_review", "Assignment already has a response with another provider identity."
                )
            obj = self.store.put(
                "review_response",
                old["id"] if old else new_id("review"),
                {
                    **source_record,
                    "assignment_id": assignment["id"],
                    "evidence_kind": "human_review",
                    "application_version_id": pub["application_version_id"],
                    "template": pub["form"]["hash"],
                },
                expected=old["revision"] if old else None,
                refs=[("assignment", "assignment", assignment["id"])],
            )
            self.store.put(
                "assignment",
                assignment["id"],
                {**assignment, "state": "submitted", "response_id": obj["id"]},
                expected=assignment["revision"],
            )
            object_kind = "review_response"
        self.store.db.execute(
            "INSERT INTO source_versions VALUES(?,?,?,?,?,?,?)",
            (*source, hash, object_kind, obj["id"], now()),
        )
        self.store.event("human_response_imported", object_kind, obj["id"], revised=bool(matches))
        return "updated" if matches else "created"

    def sync(self, opening_id, kind):
        self.store.get("opening", opening_id)
        output = {}
        for pub in self.store.list("publication"):
            if (
                pub["opening_id"] == opening_id
                and pub["kind"] == kind
                and pub["state"] in ("open", "closing", "closed")
            ):
                self.remote.verify(pub, pub["remote"]["uuid"])
                output[pub["id"]] = self.import_rows(pub["id"], self.remote.responses(pub), trusted_sync=True)
        return output

    def application(self, id):
        app = self.store.get("application", id)
        candidate = self.store.get("candidate", app["candidate_id"])
        version = self.store.get("application_version", app["version_id"])
        duplicates = [
            c["id"]
            for c in self.store.list("candidate")
            if c["id"] != candidate["id"] and c.get("email", "").casefold() == candidate["email"].casefold()
        ]
        return {
            **app,
            "candidate": candidate,
            "submission": version,
            "possible_duplicate_candidates": duplicates,
        }

    def _require_active(self, id):
        app = self.store.get("application", id)
        if app.get("deletion_pending"):
            raise SpenceError(
                "deletion_pending", "Application is pending deletion; new processing is blocked."
            )
        return app

    def move(self, id, stage, reason, expected=None, reopen=False):
        text(reason, "reason")
        app = self._require_active(id)
        if stage not in STAGES:
            raise SpenceError("invalid_stage", "Unknown application stage.")
        if app["stage"] in TERMINAL and stage != app["stage"] and not reopen:
            raise SpenceError("terminal_stage", "Use --reopen to explicitly reopen a terminal application.")
        if expected is not None and app["revision"] != expected:
            raise SpenceError("conflict", "Application changed since it was read.")
        with self.store.transaction():
            updated = self.store.put("application", id, {**app, "stage": stage}, expected=app["revision"])
            event = {
                "from": app["stage"],
                "to": stage,
                "reason": reason,
                "evidence_version": app["version_id"],
            }
            self.store.put(
                "decision",
                new_id("decision"),
                {"application_id": id, **event},
                refs=[("application", "application", id)],
            )
            self.store.event("stage_changed", "application", id, **event)
        return updated

    def note(self, id, value):
        self._require_active(id)
        with self.store.transaction():
            return self.store.put(
                "note",
                new_id("note"),
                {"application_id": id, "text": text(value, "note")},
                refs=[("application", "application", id)],
            )

    def prepare_review(self, app_id, template_id, reviewer_ids, fields=None, round="screening"):
        self._require_active(app_id)
        app = self.application(app_id)
        if app["stage"] in TERMINAL:
            raise SpenceError("terminal_stage", "Reopen the application before preparing a new review.")
        template = self.store.get("review_template", template_id)
        form = self.store.get("publication", app["publication_id"])["form"]
        reviewer_ids = list(reviewer_ids)
        if not reviewer_ids or len(reviewer_ids) != len(set(reviewer_ids)):
            raise SpenceError("invalid_roster", "Choose one or more distinct reviewers.")
        reviewers = [self.store.get("reviewer", id) for id in reviewer_ids]
        if len({r["email"].casefold() for r in reviewers}) != len(reviewers):
            raise SpenceError("invalid_roster", "Reviewers in a batch must have distinct email addresses.")
        fields = fields or [key for key, target in form["mapping"].items() if target.startswith("profile.")]
        packet = forms.packet(app["submission"], form, fields)
        id = new_id("batch")
        scenario = {
            "publication_id": id,
            "application_id": app_id,
            "application_version_id": app["version_id"],
            "candidate_packet": dumps(packet),
            "job_post": self.store.get("publication", app["publication_id"])["post"]["text"],
        }
        value = {
            "kind": "review",
            "opening_id": app["opening_id"],
            "application_id": app_id,
            "application_version_id": app["version_id"],
            "form": template,
            "scenario": scenario,
            "packet": packet,
            "packet_hash": digest(packet),
            "fields": fields,
            "reviewers": reviewers,
            "round": text(round, "round"),
            "state": "prepared",
        }
        return self._prepare(
            id,
            value,
            [
                ("application", "application", app_id),
                ("version", "application_version", app["version_id"]),
                ("template", "review_template", template_id),
            ],
            assignments=reviewers,
        )

    def send(self, publication_id, remind=False):
        pub = self.store.get("publication", publication_id)
        if pub["kind"] != "review" or pub["state"] != "open":
            raise SpenceError("invalid_state", "Publish a review batch before sending invitations.")
        self._require_active(pub["application_id"])
        assignments = [a for a in self.store.list("assignment") if a["publication_id"] == publication_id]
        if any(a["state"] in ("revoked", "revocation_pending") for a in assignments):
            raise SpenceError(
                "revoked",
                "This batch contains revoked assignments; prepare a fresh batch for active reviewers.",
            )
        sent = [
            o
            for o in self.store.list("operation")
            if o["publication_id"] == publication_id and o["action"] == "send" and o["state"] == "confirmed"
        ]
        if sent and not remind:
            raise SpenceError("already_sent", "Invitations were sent; use --remind for an explicit resend.")
        if remind and any(a["state"] == "submitted" for a in assignments):
            raise SpenceError(
                "invalid_state",
                "Prepare a batch for outstanding reviewers; reminders must not resend completed reviews.",
            )
        operation = self._operation("send", publication_id)
        try:
            result = self.remote.send(pub, operation["id"])
            if not result.get("delivery_uuid"):
                raise ValueError("Missing delivery identity")
            self._finish_operation(operation, "confirmed", delivery_id=result["delivery_uuid"])
        except Exception as exc:
            self._finish_operation(operation, "outcome_unknown", error_type=type(exc).__name__)
            raise SpenceError(
                "outcome_unknown", f"Delivery may have started; reconcile {operation['id']} before retrying."
            ) from exc
        return {"operation_id": operation["id"], **result}

    def delivery_status(self, operation_id):
        operation = self.store.get("operation", operation_id)
        if operation["action"] != "send" or not operation.get("delivery_id"):
            raise SpenceError("invalid_state", "No confirmed delivery for this operation.")
        pub = self.store.get("publication", operation["publication_id"])
        result = self.remote.delivery(pub, operation["delivery_id"])
        with self.store.transaction():
            for assignment in self.store.list("assignment"):
                if assignment["publication_id"] != pub["id"]:
                    continue
                reviewer = next(r for r in pub["reviewers"] if r["id"] == assignment["reviewer_id"])
                tasks = [
                    t
                    for t in result.get("tasks", [])
                    if t.get("identifier", "").casefold() == reviewer["email"].casefold()
                ]
                status = tasks[-1].get("delivery_status", "unknown") if tasks else "unknown"
                self.store.put(
                    "assignment",
                    assignment["id"],
                    {**assignment, "delivery_state": status},
                    expected=assignment["revision"],
                )
        self._finish_operation(operation, "confirmed", delivery_id=operation["delivery_id"], delivery=result)
        return result

    def close_opening(self, id):
        opening = self.store.get("opening", id)
        with self.store.transaction():
            self.store.put(
                "opening",
                id,
                {**opening, "state": "closing", "cutoff": opening.get("cutoff") or now()},
                expected=opening["revision"],
            )
        publications = self.store.list("publication")
        pending = [
            o
            for o in self.store.list("operation")
            if o["action"] == "publish"
            and o["state"] in ("pending", "outcome_unknown")
            and any(p["id"] == o["publication_id"] and p["opening_id"] == id for p in publications)
        ]
        if pending:
            raise SpenceError("outcome_unknown", "Reconcile unknown publications before confirming closure.")
        for pub in publications:
            if (
                pub["opening_id"] != id
                or pub["kind"] != "application"
                or not pub.get("remote")
                or pub["state"] == "closed"
            ):
                continue
            self.remote.close(pub)
            with self.store.transaction():
                self.store.put("publication", pub["id"], {**pub, "state": "closed"}, expected=pub["revision"])
        with self.store.transaction():
            opening = self.store.get("opening", id)
            result = self.store.put(
                "opening", id, {**opening, "state": "closed"}, expected=opening["revision"]
            )
            self.store.event("opening_closed", "opening", id)
        return result

    def revoke(self, id):
        assignment = self.store.get("assignment", id)
        if assignment["state"] == "revoked":
            return assignment
        with self.store.transaction():
            assignment = self.store.put(
                "assignment",
                id,
                {**assignment, "state": "revocation_pending"},
                expected=assignment["revision"],
            )
        pub = self.store.get("publication", assignment["publication_id"])
        if not assignment.get("respondent_id") and (
            pub.get("remote")
            or any(
                o["publication_id"] == pub["id"] and o["state"] in ("pending", "outcome_unknown")
                for o in self.store.list("operation")
            )
        ):
            raise SpenceError(
                "outcome_unknown",
                "Provider access may exist; respondent mapping must be recovered before revocation.",
            )
        if assignment.get("respondent_id"):
            self.remote.revoke(pub, assignment["respondent_id"])
        with self.store.transaction():
            return self.store.put(
                "assignment",
                id,
                {**assignment, "state": "revoked", "revoked_at": now()},
                expected=assignment["revision"],
            )

    def deletion_request(self, id, reason):
        """Plan all copies without claiming that a provider supports erasure."""
        app = self.store.get("application", id)
        pubs = [p for p in self.store.list("publication") if p.get("application_id") == id]
        copies = [
            {"location": "local_database", "state": "pending"},
            {
                "location": "application_provider_response",
                "publication_id": app["publication_id"],
                "state": "pending",
            },
            {"location": "backups_and_exports", "state": "operator_inventory_required"},
        ]
        copies += [
            {"location": "review_provider_and_artifact", "publication_id": p["id"], "state": "pending"}
            for p in pubs
        ]
        with self.store.transaction():
            request = self.store.put(
                "deletion",
                new_id("deletion"),
                {
                    "application_id": id,
                    "reason": text(reason, "reason"),
                    "state": "pending",
                    "copies": copies,
                },
                refs=[("application", "application", id)],
            )
            self.store.put(
                "application",
                id,
                {**app, "deletion_pending": True, "deletion_request_id": request["id"]},
                expected=app["revision"],
            )
            self.store.event("deletion_requested", "application", id, deletion_id=request["id"])
        return {
            "request": request,
            "deleted": False,
            "next_step": "Provider erasure and backup/export inventory are required; no copy has been claimed deleted.",
        }
