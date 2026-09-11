from __future__ import annotations

import html
import json

from .util import write_private


def application_report(service, id, path):
    app = service.application(id)
    store = service.store
    assignments = [a for a in store.list("assignment") if a["application_id"] == id]
    lines = [
        '<!doctype html><html lang="en"><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        (
            "<title>Candidate review</title><style>body{font:16px system-ui;max-width:900px;margin:40px auto;padding:0 20px;line-height:1.5}"
            "section{border-top:1px solid #ccc;margin-top:24px}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>"
        ),
        f"<h1>{html.escape(app['candidate']['name'])}</h1>",
        f"<p>Application {html.escape(id)} · {html.escape(app['stage'])} · Intake: {html.escape(app['intake'])}</p>",
        "<p>Human submissions and attributed reviewer opinions. No automatic hiring score.</p>",
        f"<p>{sum(a['state'] == 'submitted' for a in assignments)} of {len(assignments)} requested reviews submitted.</p>",
        "<h2>Application answers</h2>",
        f"<pre>{html.escape(json.dumps(app['submission']['answers'], indent=2, ensure_ascii=False))}</pre>",
    ]
    for assignment in assignments:
        publication = store.get("publication", assignment["publication_id"])
        reviewer = next(r for r in publication["reviewers"] if r["id"] == assignment["reviewer_id"])
        lines.extend(
            [
                "<section>",
                f"<h2>{html.escape(reviewer['name'])}</h2>",
                f"<p>{html.escape(assignment['state'])} · Delivery: {html.escape(assignment['delivery_state'])}</p>",
                f"<p>Round: {html.escape(publication['round'])} · Rubric: {html.escape(publication['form']['hash'])}</p>",
            ]
        )
        if assignment["application_version_id"] != app["version_id"]:
            lines.append("<p><strong>This reviewer assessed an earlier application version.</strong></p>")
        if assignment.get("response_id"):
            response = store.get("review_response", assignment["response_id"])
            lines.append(
                f"<pre>{html.escape(json.dumps(response['answers'], indent=2, ensure_ascii=False))}</pre>"
            )
        lines.append("</section>")
    decisions = [d for d in store.list("decision") if d["application_id"] == id]
    notes = [n for n in store.list("note") if n["application_id"] == id]
    lines.extend(
        [
            "<h2>Decisions and internal notes</h2>",
            f"<pre>{html.escape(json.dumps({'decisions': decisions, 'notes': notes}, indent=2))}</pre>",
            "</html>",
        ]
    )
    return {"path": write_private(path, "\n".join(lines)), "sensitive": True, "application_id": id}
