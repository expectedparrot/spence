"""Run the complete local ATS workflow with fictitious data and no network calls.

Install Spence, then: python examples/demo.py /tmp/spence-demo
The destination must be new. Demo Apply/reviewer URLs deliberately use .invalid.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def demonstrate(destination):
    root = Path(destination).resolve()
    if root.exists():
        raise SystemExit(f"Choose a new directory; {root} already exists.")

    def cli(*arguments, cwd=None):
        completed = subprocess.run(
            [sys.executable, "-m", "spence.cli", *map(str, arguments)],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            raise RuntimeError(completed.stdout + completed.stderr)
        return json.loads(completed.stdout)

    cli("init", root, "--provider", "demo", "--owner", "demo-operator")
    inputs = root / ".spence-private" / "fixtures"
    inputs.mkdir()
    fixtures = {
        "role.json": {
            "id": "engineer",
            "title": "Backend Engineer",
            "criteria": ["Python", "Service design"],
        },
        "reviewers.json": [
            {"id": "alex", "name": "Alex Reviewer", "email": "alex@example.com"},
            {"id": "sam", "name": "Sam Reviewer", "email": "sam@example.com"},
        ],
        "application.json": {
            "candidate_name": "Taylor Example",
            "candidate_email": "taylor@example.com",
            "experience": "Built Python APIs and operated production services.",
            "motivation": "I enjoy building useful research tools.",
        },
        "review.json": {
            "relationship": "Reviewing the submitted materials only.",
            "strengths": "Relevant Python and operations experience.",
            "concerns": "Discuss testing and collaboration in an interview.",
            "recommendation": "advance",
        },
    }
    for name, value in fixtures.items():
        (inputs / name).write_text(json.dumps(value))
    (inputs / "post.md").write_text(
        "# Backend Engineer\n\nHelp build tools for researchers. Python experience required.\n"
    )

    def run(*args):
        return cli(*args, cwd=root)

    run("role", "import", inputs / "role.json")
    run("post", "import", inputs / "post.md", "--id", "advert")
    run("reviewer", "import", inputs / "reviewers.json")
    run("application-form", "init", "--id", "short")
    run("review-template", "init", "--id", "rubric")
    run("opening", "create", "--id", "backend", "--role", "engineer")
    run("opening", "add-post", "backend", "--post", "advert")
    published = run("opening", "publish", "backend", "--post", "advert", "--form", "short")
    publication = published["publication"]["id"]
    run(
        "opening",
        "export-post",
        "backend",
        "--publication",
        publication,
        "--output",
        root / "post-with-apply.md",
    )
    run("demo", "submit", "--publication", publication, "--answers", inputs / "application.json")
    run("applications", "sync", "--opening", "backend")
    application = run("applications", "list", "--opening", "backend")["records"][0]["id"]
    run(
        "application",
        "move",
        application,
        "--to",
        "screening",
        "--reason",
        "Relevant experience merits a review.",
    )
    batch = run(
        "review", "prepare", application, "--template", "rubric", "--reviewer", "alex", "--reviewer", "sam"
    )["batch"]["id"]
    run("review", "publish", batch)
    sent = run("review", "send", batch)
    run("operation", "delivery-status", sent["operation_id"])
    assignment = run("reviews", "list", "--opening", "backend")["records"][0]["id"]
    run(
        "demo",
        "submit",
        "--publication",
        batch,
        "--assignment",
        assignment,
        "--answers",
        inputs / "review.json",
    )
    run("reviews", "sync", "--opening", "backend")
    repeated = run("reviews", "sync", "--opening", "backend")
    assert repeated["publications"][batch]["unchanged"] == 1
    run("application", "note", application, "One review received; second reviewer is still outstanding.")
    report = root / ".spence-private" / "candidate-report.html"
    run("application", "report", application, "--output", report)
    run("backup", "--output", root / ".spence-private" / "backup.sqlite3")
    return {
        "demo_only": True,
        "workspace": str(root),
        "application_id": application,
        "review_batch_id": batch,
        "report": str(report),
        "apply_url": published["apply_url"],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination")
    print(json.dumps(demonstrate(parser.parse_args().destination), indent=2))
