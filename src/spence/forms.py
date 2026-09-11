"""EDSL authoring boundary: human-only surveys, stable versions, branch-aware validation."""

from __future__ import annotations

import json
import tempfile
from importlib import metadata
from pathlib import Path

from .errors import SpenceError
from .util import digest, literal, write_private

ALLOWED = {"free_text", "multiple_choice", "check_box", "numerical", "list", "file_upload"}


def edsl():
    try:
        import edsl

        return edsl
    except ImportError as exc:
        raise SpenceError(
            "missing_dependency", "Install Spence with its EDSL dependency from GitHub main."
        ) from exc


def source_version():
    dist = metadata.distribution("edsl")
    direct = json.loads(dist.read_text("direct_url.json") or "{}")
    return {"version": dist.version, "commit": direct.get("vcs_info", {}).get("commit_id")}


def read_survey(path):
    lib = edsl()
    path = Path(path)
    if path.suffix == ".ep":
        if not hasattr(lib.Survey, "git"):
            raise SpenceError(
                "incompatible_dependency", "EDSL main with Survey.git is required for .ep files."
            )
        return lib.Survey.git.load(path)
    return lib.Survey.from_dict(json.loads(path.read_text()))


def preset(kind, resume=False):
    lib = edsl()
    from edsl.questions import QuestionFileUpload, QuestionFreeText, QuestionMultipleChoice

    if kind == "form":
        context = "<job_post>\n{{ job_post }}\n</job_post>\n\n"
        questions = [
            QuestionFreeText(question_name="candidate_name", question_text=context + "What is your name?"),
            QuestionFreeText(
                question_name="candidate_email", question_text="What email should we use to contact you?"
            ),
            QuestionFreeText(question_name="experience", question_text="Describe your relevant experience."),
            QuestionFreeText(
                question_name="motivation", question_text="Why are you interested in this role?"
            ),
        ]
        mapping = {
            "candidate_name": "contact.name",
            "candidate_email": "contact.email",
            "experience": "profile.experience",
            "motivation": "profile.motivation",
        }
        if resume:
            questions.append(QuestionFileUpload(question_name="resume", question_text="Upload your resume."))
            mapping["resume"] = "attachments.resume"
    else:
        context = "<job_post>\n{{ job_post }}\n</job_post>\n\n<candidate_packet>\n{{ candidate_packet }}\n</candidate_packet>\n\n"
        questions = [
            QuestionFreeText(question_name=name, question_text=context + prompt)
            for name, prompt in (
                (
                    "relationship",
                    "Describe your relationship to the candidate, or say you are reviewing only these materials.",
                ),
                (
                    "strengths",
                    "What relevant strengths do you see? Cite evidence from the packet or your direct experience.",
                ),
                (
                    "concerns",
                    "What concerns or missing information matter? Distinguish observation from inference.",
                ),
            )
        ]
        questions.append(
            QuestionMultipleChoice(
                question_name="recommendation",
                question_text=context + "What next step do you recommend?",
                question_options=["advance", "discuss", "do_not_advance", "insufficient_information"],
            )
        )
        mapping = {}
    survey = lib.Survey(questions)
    return definition(
        survey, {"questions": {q.question_name: {"optional": False} for q in questions}}, mapping, kind
    )


def definition(survey, ui, mapping, kind):
    from edsl.coop.coop_humanize_schema import validate_humanize_schema

    if not isinstance(ui, dict) or not isinstance(mapping, dict):
        raise SpenceError("invalid_form", "Humanize schema and field mapping must be objects.")
    if not survey.questions:
        raise SpenceError("invalid_form", "A form needs at least one question.")
    for q in survey.questions:
        if q.question_type not in ALLOWED:
            raise SpenceError("unsupported_question", f"Human ATS forms do not support {q.question_type}.")
    try:
        validate_humanize_schema(survey, ui)
    except Exception as exc:
        raise SpenceError("invalid_form", f"Invalid humanize schema: {exc}") from exc
    names = set(survey.question_names)
    if any(key not in names or not isinstance(value, str) for key, value in mapping.items()):
        raise SpenceError(
            "invalid_mapping", "Mappings must reference survey question names and string destinations."
        )
    if len(set(mapping.values())) != len(mapping):
        raise SpenceError("invalid_mapping", "Two questions cannot map to the same destination.")
    if kind == "form" and not {"contact.name", "contact.email"} <= set(mapping.values()):
        raise SpenceError("invalid_mapping", "An application must map contact.name and contact.email.")
    for q in survey.questions:
        destination = mapping.get(q.question_name, "")
        if destination.startswith("attachments.") and q.question_type != "file_upload":
            raise SpenceError("invalid_mapping", "Attachment fields must be file_upload questions.")
        if destination in ("contact.name", "contact.email") and q.question_type != "free_text":
            raise SpenceError("invalid_mapping", "Contact name/email must be free_text questions.")
    data = {
        "survey": survey.to_dict(),
        "schema": ui,
        "mapping": mapping,
        "kind": kind,
        "edsl": source_version(),
    }
    return {**data, "hash": digest(data)}


def make_jobs(form, scenario, reviewers=()):
    lib = edsl()
    literal(scenario)
    literal([{"name": r["name"], "email": r["email"]} for r in reviewers])
    survey = lib.Survey.from_dict(form["survey"])
    jobs = lib.Jobs(survey=survey).by(lib.ScenarioList([lib.Scenario(scenario)]))
    if reviewers:
        agents = lib.AgentList(
            [lib.Agent(name=r["id"], traits={"reviewer_id": r["id"], "email": r["email"]}) for r in reviewers]
        )
        jobs = jobs.by(agents)
    if jobs.models:
        raise SpenceError("invalid_form", "Human jobs must not contain models.")
    return jobs


def save_jobs(jobs, path):
    path = Path(path)
    if path.exists():
        raise SpenceError("already_exists", f"Artifact exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".ep":
        if not hasattr(type(jobs), "git"):
            raise SpenceError("incompatible_dependency", "EDSL main with Jobs.git is required.")
        with tempfile.TemporaryDirectory(prefix=".spence-build-", dir=path.parent) as temporary:
            private_path = Path(temporary) / path.name
            jobs.git.save(private_path, message="Build Spence human survey")
            write_private(path, private_path.read_bytes())
    elif path.suffix == ".json":
        write_private(path, json.dumps(jobs.to_dict(), indent=2))
    else:
        raise SpenceError("invalid_input", "Jobs output must end in .ep or .json.")
    return {"path": str(path.resolve()), "sha256": digest(path.read_bytes())}


def validate_answers(form, answers):
    if not isinstance(answers, dict):
        raise SpenceError("invalid_answers", "Answers must be an object.")
    survey = edsl().Survey.from_dict(form["survey"])
    prior = {}
    index = 0
    visited = set()
    while isinstance(index, int) and index < len(survey.questions):
        if index < 0 or index in visited:
            raise SpenceError("invalid_form", "Survey path contains a cycle.")
        visited.add(index)
        question = survey.questions[index]
        name = question.question_name
        try:
            skip = survey.rule_collection.skip_question_before_running(index, prior)
        except Exception as exc:
            raise SpenceError("invalid_branch", f"Cannot evaluate branch before {name}.") from exc
        if skip:
            index += 1
            continue
        answer = answers.get(name)
        optional = form["schema"].get("questions", {}).get(name, {}).get("optional", False)
        if answer is None or answer == "":
            if not optional:
                raise SpenceError("incomplete_submission", f"Missing required answer: {name}")
        elif question.question_type == "file_upload":
            if (
                not isinstance(answer, list)
                or not answer
                or not all(
                    isinstance(item, dict) and (item.get("gcs_path") or item.get("file_id"))
                    for item in answer
                )
            ):
                raise SpenceError(
                    "invalid_attachment", f"{name} must contain provider file references, not local paths."
                )
        else:
            try:
                question._validate_answer({"answer": answer})
            except Exception as exc:
                raise SpenceError("invalid_answer", f"Invalid answer for {name}: {exc}") from exc
        prior[name] = answer
        try:
            index = survey.rule_collection.next_question(index, prior).next_q
        except Exception as exc:
            raise SpenceError("invalid_branch", f"Cannot evaluate branch after {name}.") from exc
    return {name: answers.get(name) for name in prior}


def packet(application, form, fields):
    available = {q["question_name"] for q in form["survey"]["questions"]}
    if not fields or any(field not in available for field in fields):
        raise SpenceError(
            "invalid_packet", "Select at least one known application field for the review packet."
        )
    file_fields = {
        q["question_name"] for q in form["survey"]["questions"] if q["question_type"] == "file_upload"
    }
    if file_fields & set(fields):
        raise SpenceError(
            "unsupported_capability",
            "Sharing hosted attachments with reviewers requires verified file access controls.",
            "Select text fields; inspect resume references privately with application show.",
        )
    result = {field: application["answers"].get(field) for field in fields}
    literal(result)
    return result
