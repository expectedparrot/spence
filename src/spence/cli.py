from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

from . import __version__, forms
from .errors import SpenceError
from .provider import DemoProvider, load_export
from .report import application_report
from .service import STAGES, Service
from .store import Store
from .util import digest, dumps, email, identifier, read_json, text, write_private


def parser():
    p = argparse.ArgumentParser(prog="spence", description="Track applications and human candidate reviews.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="group", required=True)
    init = sub.add_parser("init")
    init.add_argument("path")
    init.add_argument("--provider", choices=["coop", "demo"], default="coop")
    init.add_argument("--account", default="default")
    init.add_argument("--owner", default="operator")
    for group in ("role", "post", "reviewer"):
        commands = sub.add_parser(group).add_subparsers(dest="command", required=True)
        imp = commands.add_parser("import")
        imp.add_argument("path")
        imp.add_argument("--id")
        imp.add_argument("--revise", action="store_true")
        if group == "post":
            imp.add_argument("--mccall-export", action="store_true")
        commands.add_parser("list")
        show = commands.add_parser("show")
        show.add_argument("id")
    for group in ("application-form", "review-template"):
        commands = sub.add_parser(group).add_subparsers(dest="command", required=True)
        init = commands.add_parser("init")
        init.add_argument("--id", required=True)
        init.add_argument("--preset", choices=["short"], default="short")
        init.add_argument("--resume", action="store_true")
        init.add_argument("--revise", action="store_true")
        imp = commands.add_parser("import")
        imp.add_argument("--id", required=True)
        imp.add_argument("--survey", required=True)
        imp.add_argument("--schema", required=True)
        imp.add_argument("--mapping")
        imp.add_argument("--revise", action="store_true")
        for name in ("show", "validate", "export"):
            cmd = commands.add_parser(name)
            cmd.add_argument("id")
            if name == "export":
                cmd.add_argument("--output", required=True)
        commands.add_parser("list")
        if group == "application-form":
            build = commands.add_parser("build")
            build.add_argument("id")
            build.add_argument("--opening", required=True)
            build.add_argument("--post", required=True)
            build.add_argument("--output")
    opening = sub.add_parser("opening").add_subparsers(dest="command", required=True)
    create = opening.add_parser("create")
    create.add_argument("--id", required=True)
    create.add_argument("--role", required=True)
    create.add_argument("--title")
    opening.add_parser("list")
    for name in ("show", "close", "add-post", "publish", "export-post"):
        cmd = opening.add_parser(name)
        cmd.add_argument("id")
        if name == "add-post":
            cmd.add_argument("--post", required=True)
        if name == "publish":
            cmd.add_argument("--post", required=True)
            cmd.add_argument("--form", required=True)
        if name == "export-post":
            cmd.add_argument("--publication", required=True)
            cmd.add_argument("--format", choices=["markdown", "html"], default="markdown")
            cmd.add_argument("--output", required=True)
    for group in ("applications", "reviews"):
        commands = sub.add_parser(group).add_subparsers(dest="command", required=True)
        sync = commands.add_parser("sync")
        sync.add_argument("--opening", required=True)
        ingest = commands.add_parser("ingest")
        ingest.add_argument("path")
        ingest.add_argument("--publication", required=True)
        ingest.add_argument("--accept-revisions", action="store_true")
        ls = commands.add_parser("list")
        ls.add_argument("--opening")
        if group == "applications":
            ls.add_argument("--stage", choices=STAGES)
            ls.add_argument("--search")
    application = sub.add_parser("application").add_subparsers(dest="command", required=True)
    for name in ("show", "move", "note", "report", "export", "delete-request"):
        cmd = application.add_parser(name)
        cmd.add_argument("id")
        if name == "move":
            cmd.add_argument("--to", choices=STAGES, required=True)
            cmd.add_argument("--reason", required=True)
            cmd.add_argument("--expected-revision", type=int)
            cmd.add_argument("--reopen", action="store_true")
        if name == "note":
            cmd.add_argument("text")
        if name in ("report", "export"):
            cmd.add_argument("--output", required=True)
        if name == "delete-request":
            cmd.add_argument("--reason", required=True)
    review = sub.add_parser("review").add_subparsers(dest="command", required=True)
    prep = review.add_parser("prepare")
    prep.add_argument("id")
    prep.add_argument("--template", required=True)
    prep.add_argument("--reviewer", action="append", required=True)
    prep.add_argument("--field", action="append")
    prep.add_argument("--round", default="screening")
    for name in ("inspect", "publish", "send", "revoke", "links"):
        cmd = review.add_parser(name)
        cmd.add_argument("id")
        if name == "send":
            cmd.add_argument("--remind", action="store_true")
        if name == "links":
            cmd.add_argument("--output", required=True)
    publication = sub.add_parser("publication").add_subparsers(dest="command", required=True)
    publication.add_parser("list")
    for name in ("show", "publish", "configure"):
        publication.add_parser(name).add_argument("id")
    operation = sub.add_parser("operation").add_subparsers(dest="command", required=True)
    operation.add_parser("list")
    for name in ("show", "reconcile", "resolve-not-created", "delivery-status"):
        cmd = operation.add_parser(name)
        cmd.add_argument("id")
        if name == "reconcile":
            cmd.add_argument("--remote-id", required=True)
        if name == "resolve-not-created":
            cmd.add_argument(
                "--reason",
                required=True,
                help="Evidence from provider inspection that no remote object/delivery was created",
            )
    quarantine = sub.add_parser("quarantine").add_subparsers(dest="command", required=True)
    quarantine.add_parser("list")
    for name in ("show", "retry"):
        cmd = quarantine.add_parser(name)
        cmd.add_argument("id")
        if name == "retry":
            cmd.add_argument("--accept-revisions", action="store_true")
    sub.add_parser("capabilities")
    backup = sub.add_parser("backup")
    backup.add_argument("--output", required=True)
    demo = sub.add_parser("demo").add_subparsers(dest="command", required=True)
    submit = demo.add_parser("submit")
    submit.add_argument("--publication", required=True)
    submit.add_argument("--answers", required=True)
    submit.add_argument("--assignment")
    submit.add_argument("--response-id")
    return p


def summary(kind, record):
    common = ("id", "revision", "updated_at")
    fields = {
        "opening": ("title", "state"),
        "publication": ("kind", "opening_id", "state", "last_sync_at"),
        "application": ("opening_id", "candidate_id", "stage", "intake", "version_id"),
        "assignment": ("application_id", "reviewer_id", "state", "delivery_state"),
        "operation": ("action", "publication_id", "state", "remote_id", "delivery_id"),
        "quarantine": ("publication_id", "state", "error"),
    }
    return {key: record[key] for key in (*common, *fields.get(kind, ())) if key in record}


def run(args, store, service):
    g, c = args.group, getattr(args, "command", None)
    if g in ("role", "post", "reviewer"):
        if c == "list":
            return {"records": [summary(g, r) for r in store.list(g)]}
        if c == "show":
            return {"record": store.get(g, args.id)}
        if g == "post" and not args.mccall_export:
            value = {"text": text(Path(args.path).read_text(), "post text")}
        else:
            value = read_json(args.path)
        if g == "reviewer" and isinstance(value, list):
            prepared = []
            for row in value:
                if not isinstance(row, dict):
                    raise SpenceError("invalid_input", "Reviewer roster must contain objects.")
                id = identifier(row.get("id"))
                prepared.append(
                    (id, {**row, "name": text(row.get("name"), "name"), "email": email(row.get("email"))})
                )
            if len({id for id, _ in prepared}) != len(prepared):
                raise SpenceError("duplicate_id", "Duplicate reviewer IDs.")
            with store.transaction():
                for id, data in prepared:
                    old = store.get(g, id) if args.revise else None
                    store.put(g, id, data, expected=old["revision"] if old else None)
            return {"imported": len(prepared)}
        if not isinstance(value, dict):
            raise SpenceError("invalid_input", "Expected a JSON object.")
        if g == "role":
            text(value.get("title"), "role title")
        if g == "reviewer":
            text(value.get("name"), "reviewer name")
            email(value.get("email"))
        if g == "post":
            text(value.get("text"), "post text")
            if (
                args.mccall_export
                and value.get("sha256") != hashlib.sha256(value["text"].encode()).hexdigest()
            ):
                raise SpenceError("integrity_error", "McCall post hash does not match its text.")
        id = identifier(args.id or value.get("id") or Path(args.path).stem)
        value = {
            **value,
            "source": {
                "path": str(Path(args.path).resolve()),
                "sha256": digest(Path(args.path).read_bytes()),
            },
        }
        return {"record": summary(g, service.save_design(g, id, value, revise=args.revise))}
    if g in ("application-form", "review-template"):
        kind = "form" if g == "application-form" else "review_template"
        if c == "list":
            return {"records": [summary(kind, r) for r in store.list(kind)]}
        if c == "init":
            value = forms.preset(kind, args.resume)
        elif c == "import":
            value = forms.definition(
                forms.read_survey(args.survey),
                read_json(args.schema),
                read_json(args.mapping) if args.mapping else {},
                kind,
            )
        elif c == "build":
            if args.output and Path(args.output).exists():
                raise SpenceError("already_exists", "Output file already exists.")
            pub = service.prepare_application(args.opening, args.post, args.id)
            result = {"publication": summary("publication", pub), "artifact": pub["artifact"]}
            if args.output:
                result["export"] = forms.save_jobs(forms.make_jobs(pub["form"], pub["scenario"]), args.output)
            return result
        else:
            record = store.get(kind, args.id)
            if c == "validate":
                forms.definition(
                    forms.edsl().Survey.from_dict(record["survey"]), record["schema"], record["mapping"], kind
                )
                return {"valid": True, "hash": record["hash"]}
            if c == "export":
                return {"path": write_private(args.output, dumps(record))}
            return {"record": record}
        return {"record": summary(kind, service.save_design(kind, args.id, value, revise=args.revise))}
    if g == "opening":
        if c == "create":
            return {"opening": service.create_opening(args.id, args.role, args.title)}
        if c == "list":
            return {"openings": [summary("opening", r) for r in store.list("opening")]}
        if c == "show":
            return {"opening": store.get("opening", args.id)}
        if c == "add-post":
            return {"opening": summary("opening", service.add_post(args.id, args.post))}
        if c == "close":
            return {"opening": summary("opening", service.close_opening(args.id))}
        if c == "publish":
            pub = service.publish(service.prepare_application(args.id, args.post, args.form)["id"])
            return {"publication": summary("publication", pub), "apply_url": pub["remote"]["respondent_url"]}
        pub = store.get("publication", args.publication)
        if pub["opening_id"] != args.id:
            raise SpenceError("source_mismatch", "Publication belongs to another opening.")
        return service.export_post(args.publication, args.output, args.format)
    if g in ("applications", "reviews"):
        kind = "application" if g == "applications" else "review"
        if c == "sync":
            return {"publications": service.sync(args.opening, kind)}
        if c == "ingest":
            pub = store.get("publication", args.publication)
            if pub["kind"] != kind or not pub.get("remote"):
                raise SpenceError("source_mismatch", "Wrong publication type or unpublished form.")
            return service.import_rows(
                args.publication,
                load_export(args.path, pub["remote"]["uuid"]),
                accept_revisions=args.accept_revisions,
            )
        record_kind = "application" if kind == "application" else "assignment"
        records = store.list(record_kind)
        if args.opening:
            store.get("opening", args.opening)
            records = [
                r
                for r in records
                if (r.get("opening_id") or store.get("application", r["application_id"])["opening_id"])
                == args.opening
            ]
        if kind == "application":
            if args.stage:
                records = [r for r in records if r["stage"] == args.stage]
            if args.search:
                records = [
                    r
                    for r in records
                    if args.search.casefold() in dumps(store.get("candidate", r["candidate_id"])).casefold()
                ]
        return {"records": [summary(record_kind, r) for r in records]}
    if g == "application":
        if c == "show":
            return {"application": service.application(args.id)}
        if c == "move":
            return {
                "application": summary(
                    "application",
                    service.move(args.id, args.to, args.reason, args.expected_revision, args.reopen),
                )
            }
        if c == "note":
            return {"note": summary("note", service.note(args.id, args.text))}
        if c == "report":
            return application_report(service, args.id, args.output)
        if c == "export":
            return {
                "path": write_private(args.output, dumps(service.application(args.id))),
                "sensitive": True,
            }
        if c == "delete-request":
            return service.deletion_request(args.id, args.reason)
    if g == "review":
        if c == "prepare":
            return {
                "batch": summary(
                    "publication",
                    service.prepare_review(args.id, args.template, args.reviewer, args.field, args.round),
                )
            }
        if c == "publish":
            return {"batch": summary("publication", service.publish(args.id))}
        if c == "send":
            return service.send(args.id, args.remind)
        if c == "revoke":
            return {"assignment": summary("assignment", service.revoke(args.id))}
        pub = store.get("publication", args.id)
        if pub["kind"] != "review":
            raise SpenceError("invalid_state", "Expected a review batch.")
        assignments = [a for a in store.list("assignment") if a["publication_id"] == args.id]
        if c == "links":
            links = [
                {k: a.get(k) for k in ("id", "reviewer_id", "respondent_id", "url")} for a in assignments
            ]
            return {"path": write_private(args.output, dumps(links)), "sensitive": True}
        return {"batch": pub, "assignments": [summary("assignment", a) for a in assignments]}
    if g == "publication":
        if c == "list":
            return {"publications": [summary(g, r) for r in store.list(g)]}
        if c == "show":
            return {"publication": store.get(g, args.id)}
        return {"publication": summary(g, getattr(service, c)(args.id))}
    if g == "operation":
        if c == "list":
            return {"operations": [summary(g, o) for o in store.list(g)]}
        if c == "show":
            return {"operation": store.get(g, args.id)}
        if c == "reconcile":
            return {"result": service.reconcile(args.id, args.remote_id)}
        if c == "resolve-not-created":
            return {"operation": service.resolve_not_created(args.id, args.reason)}
        return service.delivery_status(args.id)
    if g == "quarantine":
        if c == "list":
            return {"records": [summary(g, q) for q in store.list(g)]}
        item = store.get(g, args.id)
        if c == "show":
            return {"record": item}
        result = service.import_rows(
            item["publication_id"], [item["row"]], accept_revisions=args.accept_revisions
        )
        if not result["quarantined"]:
            with store.transaction():
                store.put(g, args.id, {**item, "state": "resolved"}, expected=item["revision"])
        return result
    if g == "backup":
        return store.backup(args.output)
    if g == "capabilities":
        return {"provider": store.config["provider"], "capabilities": service.remote.capabilities()}
    if g == "demo":
        if not isinstance(service.remote, DemoProvider):
            raise SpenceError(
                "invalid_state", "demo submit requires a workspace initialized with --provider demo."
            )
        pub = store.get("publication", args.publication)
        assignment = store.get("assignment", args.assignment) if args.assignment else None
        if assignment and assignment["publication_id"] != args.publication:
            raise SpenceError("source_mismatch", "Assignment belongs to another publication.")
        row = service.remote.submit(
            pub["remote"]["uuid"],
            read_json(args.answers),
            respondent_id=assignment.get("respondent_id") if assignment else None,
            response_id=args.response_id,
        )
        return {"demo_only": True, "response_id": row["response_id"]}
    raise SpenceError("invalid_command", "Unknown command.")


def main(argv=None):
    store = None
    try:
        args = parser().parse_args(argv)
        if args.group == "init":
            text(args.owner, "owner")
            text(args.account, "account")
            store = Store.initialize(
                args.path, provider=args.provider, account=args.account, owner=args.owner
            )
            result = {"project": str(store.root), "provider": args.provider}
        else:
            store = Store.open()
            result = run(args, store, Service(store))
        print(json.dumps({"ok": True, **result}, indent=2, ensure_ascii=False))
        return 0
    except SpenceError as exc:
        print(json.dumps({"ok": False, "errors": [exc.to_dict()]}))
        return 1
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"ok": False, "errors": [{"code": "io_error", "message": str(exc)}]}))
        return 1
    except Exception as exc:  # noqa: BLE001 - Keep provider payloads out of terminal tracebacks.
        # Provider errors can contain credentials or full response bodies. Preserve the type, not a traceback.
        print(
            json.dumps(
                {
                    "ok": False,
                    "errors": [
                        {
                            "code": "operation_failed",
                            "message": f"Operation failed ({type(exc).__name__}); inspect operation list before retrying remote actions.",
                        }
                    ],
                }
            )
        )
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    sys.exit(main())
