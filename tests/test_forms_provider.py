import copy
import json
from unittest.mock import create_autospec

import pytest
from edsl import (
    Agent,
    AgentList,
    Coop,
    Dataset,
    QuestionFreeText,
    QuestionMultipleChoice,
    Scenario,
    ScenarioList,
    Survey,
)
from edsl.questions import QuestionFileUpload

from spence import forms
from spence.errors import SpenceError
from spence.provider import CoopProvider, envelopes, load_export


def test_branch_aware_required_answers():
    survey = Survey(
        [
            QuestionMultipleChoice(
                question_name="screen", question_text="Continue?", question_options=["yes", "no"]
            ),
            QuestionFreeText(question_name="detail", question_text="Details"),
            QuestionFreeText(question_name="end", question_text="Finish"),
        ]
    )
    survey.add_rule("screen", "screen == 'no'", "end")
    form = forms.definition(survey, {}, {}, "review_template")
    assert forms.validate_answers(form, {"screen": "no", "end": "Done"}) == {"screen": "no", "end": "Done"}
    with pytest.raises(SpenceError, match="detail"):
        forms.validate_answers(form, {"screen": "yes", "end": "Done"})
    with pytest.raises(SpenceError, match="screen"):
        forms.validate_answers(form, {"screen": "maybe", "end": "Done"})


def test_optional_answers_and_hosted_file_references():
    survey = Survey(
        [
            QuestionFreeText(question_name="note", question_text="Optional"),
            QuestionFileUpload(question_name="resume", question_text="Resume"),
        ]
    )
    form = forms.definition(survey, {"questions": {"note": {"optional": True}}}, {}, "review_template")
    reference = [{"file_id": "file-1", "filename": "resume.pdf"}]
    assert forms.validate_answers(form, {"resume": reference})["resume"] == reference
    with pytest.raises(SpenceError, match="provider file references"):
        forms.validate_answers(form, {"resume": "/private/resume.pdf"})
    with pytest.raises(SpenceError, match="access controls"):
        forms.packet({"answers": {"resume": reference}}, form, ["resume"])


def test_ep_jobs_are_human_only_and_roundtrip(tmp_path):
    from edsl import Jobs

    form = forms.preset("form")
    jobs = forms.make_jobs(form, {"job_post": "Engineer"})
    path = tmp_path / "application.jobs.ep"
    forms.save_jobs(jobs, path)
    loaded = Jobs.git.load(path)
    assert not loaded.models
    assert not loaded.agents
    assert loaded.survey.to_dict() == jobs.survey.to_dict()
    assert loaded.scenarios[0]["job_post"] == "Engineer"
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(SpenceError):
        forms.save_jobs(jobs, path)


def test_human_response_adapters_do_not_coerce_literal_strings(tmp_path):
    raw = {
        "responses": [
            {
                "response_uuid": "r1",
                "response_json_string": json.dumps({"note": "123", "bool": "false", "list": ["x"]}),
                "agent_traits_json_string": json.dumps({"respondent_uuid": "person1"}),
            }
        ]
    }
    row = envelopes(raw, survey_id="s1")[0]
    assert row["answers"] == {"note": "123", "bool": "false", "list": ["x"]}
    assert row["respondent_id"] == "person1"
    result = {
        "data": [
            {
                "agent": {"name": "r1", "traits": {"respondent_uuid": "person1"}},
                "model": {"model": "test"},
                "answer": {"note": "human answer"},
            }
        ]
    }
    row = envelopes(result, survey_id="s1")[0]
    assert row["response_id"] == "r1"
    assert row["answers"]["note"] == "human answer"
    fallback = envelopes({"scenarios": [{"agent_name": "r1", "note": "literal"}]}, survey_id="s1")[0]
    assert fallback["respondent_id"] is None
    assert fallback["response_id"] == "r1"
    assert envelopes({"responses": [{"response_json_string": "invalid"}]}, survey_id="s1")[0]["error"]
    path = tmp_path / "responses.json"
    path.write_text(json.dumps([{"survey_id": "wrong", "response_id": "r1", "answers": {}}]))
    assert load_export(path, "s1")[0]["error"]


def test_real_coop_client_signatures_and_identity_checks(ats, application):
    batch = ats.prepare_review(application["id"], "rubric", ["alex"])
    client = create_autospec(Coop, instance=True)
    adapter = CoopProvider(client=client)
    remote = {
        "uuid": "human-1",
        "survey_uuid": "survey-1",
        "agent_list_uuid": "agents-1",
        "scenario_list_uuid": "scenarios-1",
        "respondent_url": "https://example.com/apply",
    }
    client.create_human_survey.return_value = remote
    jobs = forms.make_jobs(batch["form"], batch["scenario"], batch["reviewers"])
    assert adapter.publish(jobs, batch) == remote
    kwargs = client.create_human_survey.call_args.kwargs
    assert kwargs["survey_visibility"] == "private"
    assert kwargs["scenario_list_visibility"] == "private"
    assert kwargs["agent_list_visibility"] == "private"
    assert kwargs["scenario_list_method"] == "single_scenario"
    assert kwargs["delivery_map"] == {"email": {"col_name": "email"}}
    batch = {**batch, "remote": remote}
    client.get_human_survey_respondent_links.return_value = Dataset(
        [{"reviewer_id": ["alex"]}, {"respondent_uuid": ["person-1"]}, {"url": ["https://example.com/token"]}]
    )
    assert adapter.configure(batch)[0]["reviewer_id"] == "alex"
    client.patch_human_survey_agent_list.assert_called_once_with(
        "human-1", anonymous=False, allow_resubmit=False
    )
    client.get_human_survey.return_value = remote
    objects = {
        "survey-1": Survey.from_dict(batch["form"]["survey"]),
        "scenarios-1": ScenarioList([Scenario(batch["scenario"])]),
        "agents-1": AgentList(
            [Agent(name="alex", traits={"reviewer_id": "alex", "email": "alex@example.com"})]
        ),
    }
    client.pull.side_effect = lambda uuid, **kwargs: objects[uuid]
    assert adapter.verify(batch, "human-1") == remote
    objects["agents-1"] = AgentList([Agent(traits={"reviewer_id": "alex", "email": "intruder@example.com"})])
    with pytest.raises(SpenceError, match="roster"):
        adapter.verify(batch, "human-1")
    client._send_server_request.return_value.json.return_value = {"survey_uuid": "wrong", "responses": []}
    with pytest.raises(SpenceError, match="does not match"):
        adapter.responses(batch)
    client._send_server_request.return_value.json.return_value = {"survey_uuid": "survey-1", "responses": []}
    assert adapter.responses(batch) == []
    client.create_human_survey_delivery.return_value = {"delivery_uuid": "delivery-1"}
    adapter.send(batch, "op-1")
    client.create_human_survey_delivery.assert_called_once_with("human-1", name="op-1")
    client.get_human_survey_delivery.return_value = {"status": "completed"}
    client.list_human_survey_delivery_tasks.side_effect = [
        {"tasks": [{"identifier": "alex@example.com"}], "total_pages": 2},
        {"tasks": [], "total_pages": 2},
    ]
    assert len(adapter.delivery(batch, "delivery-1")["tasks"]) == 1
    assert client.list_human_survey_delivery_tasks.call_count == 2


def test_unattributed_and_wrong_roster_reviews_quarantine(ats, application, monkeypatch):
    batch = ats.prepare_review(application["id"], "rubric", ["alex"])
    configure = ats.remote.configure
    monkeypatch.setattr(
        ats.remote, "configure", lambda pub: [{"reviewer_id": "wrong", "respondent_uuid": "p", "url": "url"}]
    )
    with pytest.raises(SpenceError, match="roster"):
        ats.publish(batch["id"])
    assert ats.store.get("publication", batch["id"])["state"] == "configuring"
    assert len(ats.remote.read()["surveys"]) == 2
    monkeypatch.setattr(ats.remote, "configure", configure)
    batch = ats.publish(batch["id"])
    feedback = {
        "relationship": "Materials",
        "strengths": "Good",
        "concerns": "None",
        "recommendation": "advance",
    }
    row = {"response_id": "r1", "survey_id": batch["remote"]["uuid"], "answers": feedback}
    assert ats.import_rows(batch["id"], [row])["quarantined"] == 1
    assert not ats.store.list("review_response")
    assignment = ats.store.list("assignment")[0]
    row["respondent_id"] = assignment["respondent_id"]
    assert ats.import_rows(batch["id"], [row])["created"] == 1
    assert ats.import_rows(batch["id"], [{**row, "response_id": "different-id"}])["quarantined"] == 1


def test_invalid_schema_and_missing_contact_mapping():
    survey = Survey([QuestionFreeText(question_name="name", question_text="Name")])
    with pytest.raises(SpenceError, match="contact"):
        forms.definition(survey, {}, {}, "form")
    with pytest.raises(SpenceError, match="schema"):
        forms.definition(survey, {"questions": {"unknown": {"optional": False}}}, {}, "review_template")
    original = forms.preset("form")
    changed = copy.deepcopy(original)
    changed["mapping"]["motivation"] = "contact.email"
    with pytest.raises(SpenceError, match="same destination"):
        forms.definition(Survey.from_dict(changed["survey"]), changed["schema"], changed["mapping"], "form")
