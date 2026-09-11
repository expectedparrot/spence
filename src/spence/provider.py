"""Narrow humanize integration; demo mode is explicitly local and sends no messages."""

from __future__ import annotations

import json
from pathlib import Path

from .errors import SpenceError
from .util import digest, dumps, new_id, now, read_json, write_private


def envelopes(value, *, survey_id):
    """Accept provider raw responses or exported EDSL Results / ScenarioList.

    Caller supplies the trusted publication context. Unattributable rows stay
    visible as quarantine records instead of being matched by name or list order.
    """
    if isinstance(value, dict) and "responses" in value:
        values = value["responses"]
        mode = "raw"
    elif isinstance(value, dict) and "data" in value:
        values, mode = value["data"], "results"
    elif isinstance(value, dict) and "scenarios" in value:
        values, mode = value["scenarios"], "scenarios"
    elif isinstance(value, list):
        values, mode = value, "envelopes"
    else:
        raise SpenceError("invalid_responses", "Expected response envelopes, EDSL Results, or ScenarioList.")
    if not isinstance(values, list):
        raise SpenceError("invalid_responses", "Response payload must contain a list.")
    rows = []
    for raw in values:
        try:
            if not isinstance(raw, dict):
                raise TypeError("Response must be an object")
            if mode == "raw":
                answers = json.loads(raw["response_json_string"])
                traits = json.loads(raw.get("agent_traits_json_string") or "{}")
                row = {
                    "response_id": raw.get("response_uuid"),
                    "respondent_id": traits.get("respondent_uuid"),
                    "answers": answers,
                    "submitted_at": raw.get("submitted_at"),
                    "provider_revision": raw.get("revision"),
                }
            elif mode == "results":
                agent = raw.get("agent", {})
                row = {
                    "response_id": agent.get("name"),
                    "respondent_id": agent.get("traits", {}).get("respondent_uuid"),
                    "answers": raw.get("answer"),
                    "submitted_at": None,
                }
            elif mode == "scenarios":
                row = {
                    "response_id": raw.get("agent_name"),
                    "respondent_id": None,
                    "answers": {
                        key: item
                        for key, item in raw.items()
                        if key not in ("agent_name", "edsl_version", "edsl_class_name")
                    },
                    "submitted_at": None,
                }
            else:
                row = dict(raw)
            if row.get("survey_id", survey_id) != survey_id:
                raise ValueError("Envelope survey identity does not match selected publication")
            rows.append({**row, "survey_id": survey_id, "source": raw})
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            rows.append({"survey_id": survey_id, "source": raw, "error": str(exc)})
    return rows


class CoopProvider:
    name = "coop"

    def __init__(self, store=None, client=None):
        if client is None:
            from edsl import Coop

            client = Coop()
        self.client = client

    def capabilities(self):
        return {
            "publish": True,
            "responses": True,
            "delivery": True,
            "personal_links": True,
            "close": False,
            "revoke": False,
            "delete": False,
            "attachments_access_verified": False,
            "hosted_acceptance_verified": False,
        }

    def publish(self, jobs, publication):
        return self.client.create_human_survey(
            survey=jobs.survey,
            scenario_list=jobs.scenarios,
            scenario_list_method="single_scenario",
            agent_list=jobs.agents if jobs.agents else None,
            human_survey_name=f"Spence {publication['id']}",
            humanize_schema=publication["form"]["schema"],
            survey_visibility="private",
            scenario_list_visibility="private",
            agent_list_visibility="private",
            delivery_map={"email": {"col_name": "email"}} if jobs.agents else None,
        )

    def configure(self, publication):
        id = publication["remote"]["uuid"]
        self.client.patch_human_survey_agent_list(id, anonymous=False, allow_resubmit=False)
        return self.client.get_human_survey_respondent_links(id, strict=True).to_dicts(remove_prefix=False)

    def verify(self, publication, remote_id):
        from edsl import AgentList, ScenarioList, Survey

        remote = self.client.get_human_survey(remote_id)
        survey = self.client.pull(remote["survey_uuid"], expected_object_type="survey")
        scenarios = self.client.pull(remote["scenario_list_uuid"], expected_object_type="scenario_list")
        if not isinstance(survey, Survey) or not isinstance(scenarios, ScenarioList):
            raise SpenceError("provider_mismatch", "Provider returned unexpected object types.")
        if survey.to_dict() != publication["form"]["survey"] or [dict(s) for s in scenarios] != [
            publication["scenario"]
        ]:
            raise SpenceError(
                "provider_mismatch", "Remote form/scenario differs from the prepared publication."
            )
        if remote.get("uuid") != remote_id:
            raise SpenceError("provider_mismatch", "Provider returned a different survey identity.")
        if publication["reviewers"]:
            agents = self.client.pull(remote["agent_list_uuid"], expected_object_type="agent_list")
            expected = [(r["id"], r["email"]) for r in publication["reviewers"]]
            actual = (
                [(a.traits.get("reviewer_id"), a.traits.get("email")) for a in agents]
                if isinstance(agents, AgentList)
                else []
            )
            if actual != expected:
                raise SpenceError(
                    "provider_mismatch", "Remote reviewer roster differs from the prepared batch."
                )
        elif remote.get("agent_list_uuid"):
            raise SpenceError("provider_mismatch", "Public application unexpectedly has a recipient roster.")
        return remote

    def responses(self, publication):
        id = publication["remote"]["uuid"]
        # Use the API envelope directly: Results reconstruction can discard source timestamps and respondent IDs.
        response = self.client._send_server_request(uri=f"api/v0/human-surveys/{id}/responses", method="GET")
        self.client._resolve_server_response(response)
        payload = response.json()
        if payload.get("survey_uuid") != publication["remote"].get("survey_uuid"):
            raise SpenceError("provider_mismatch", "Response survey does not match publication.")
        return envelopes(payload, survey_id=id)

    def send(self, publication, operation_id):
        return self.client.create_human_survey_delivery(publication["remote"]["uuid"], name=operation_id)

    def delivery(self, publication, delivery_id):
        id = publication["remote"]["uuid"]
        result = self.client.get_human_survey_delivery(id, delivery_id)
        tasks = []
        page = 1
        while True:
            batch = self.client.list_human_survey_delivery_tasks(id, delivery_id, page=page, page_size=100)
            tasks.extend(batch.get("tasks", []))
            if page >= (batch.get("total_pages") or 1):
                break
            page += 1
        return {**result, "tasks": tasks}

    def close(self, publication):
        raise SpenceError(
            "unsupported_capability",
            "EDSL client has no verified hosted-intake closure operation.",
            "Opening remains closing, not closed; see docs/provider-contracts.md.",
        )

    def revoke(self, publication, respondent_id):
        raise SpenceError(
            "unsupported_capability",
            "Hosted respondent/file revocation is not verified.",
            "Assignment remains revocation_pending; local state is not proof of revoked access.",
        )

    def delete(self, publication):
        raise SpenceError("unsupported_capability", "Provider response and file deletion is not verified.")


class DemoProvider:
    name = "demo"

    def __init__(self, store):
        self.path = store.private / "demo-provider.json"

    def read(self):
        return read_json(self.path) if self.path.exists() else {"surveys": {}, "deliveries": {}}

    def save(self, value):
        write_private(self.path, dumps(value), overwrite=True)

    def capabilities(self):
        return {
            "publish": True,
            "responses": True,
            "delivery": True,
            "personal_links": True,
            "close": True,
            "revoke": True,
            "delete": True,
            "attachments_access_verified": False,
            "hosted_acceptance_verified": False,
            "demo_only": True,
        }

    def publish(self, jobs, publication):
        data = self.read()
        id = new_id("survey")
        roster = [
            {
                "reviewer_id": r["id"],
                "respondent_uuid": new_id("respondent"),
                "url": f"https://demo.invalid/review/{new_id('token')}",
                "response_status": "not_started",
            }
            for r in publication.get("reviewers", [])
        ]
        remote = {
            "uuid": id,
            "survey_uuid": new_id("form"),
            "respondent_url": f"https://demo.invalid/apply/{id}",
        }
        data["surveys"][id] = {
            "remote": remote,
            "definition": digest(publication["scenario"]),
            "form": publication["form"]["hash"],
            "roster": roster,
            "responses": [],
            "closed": False,
        }
        self.save(data)
        return remote

    def configure(self, publication):
        return self.read()["surveys"][publication["remote"]["uuid"]]["roster"]

    def verify(self, publication, remote_id):
        remote = self.read()["surveys"][remote_id]
        if (
            remote["definition"] != digest(publication["scenario"])
            or remote["form"] != publication["form"]["hash"]
        ):
            raise SpenceError("provider_mismatch", "Remote demo publication differs.")
        return remote["remote"]

    def responses(self, publication):
        return self.read()["surveys"][publication["remote"]["uuid"]]["responses"]

    def submit(self, remote_id, answers, *, respondent_id=None, response_id=None, submitted_at=None):
        data = self.read()
        survey = data["surveys"][remote_id]
        if survey["closed"]:
            raise SpenceError("closed", "Demo survey is closed.")
        if survey["roster"]:
            person = next((r for r in survey["roster"] if r["respondent_uuid"] == respondent_id), None)
            if person is None or person.get("revoked"):
                raise SpenceError("access_denied", "Unknown or revoked demo reviewer.")
        row = {
            "survey_id": remote_id,
            "response_id": response_id or new_id("response"),
            "respondent_id": respondent_id,
            "answers": answers,
            "submitted_at": submitted_at or now(),
        }
        survey["responses"] = [r for r in survey["responses"] if r["response_id"] != row["response_id"]] + [
            row
        ]
        self.save(data)
        return row

    def send(self, publication, operation_id):
        data = self.read()
        id = new_id("delivery")
        data["deliveries"][id] = {
            "delivery_uuid": id,
            "status": "completed",
            "demo_only": True,
            "tasks": [
                {"identifier": r["email"], "delivery_status": "sent"} for r in publication["reviewers"]
            ],
        }
        self.save(data)
        return {"delivery_uuid": id}

    def delivery(self, publication, delivery_id):
        return self.read()["deliveries"][delivery_id]

    def close(self, publication):
        data = self.read()
        data["surveys"][publication["remote"]["uuid"]]["closed"] = True
        self.save(data)

    def revoke(self, publication, respondent_id):
        data = self.read()
        roster = data["surveys"][publication["remote"]["uuid"]]["roster"]
        for person in roster:
            if person["respondent_uuid"] == respondent_id:
                person["revoked"] = True
        self.save(data)

    def delete(self, publication):
        data = self.read()
        data["surveys"].pop(publication["remote"]["uuid"], None)
        self.save(data)


def provider(store):
    return DemoProvider(store) if store.config["provider"] == "demo" else CoopProvider(store)


def load_export(path, survey_id):
    path = Path(path)
    if path.suffix == ".ep":
        from edsl import Results

        value = Results.git.load(path).to_dict()
    else:
        value = read_json(path)
    return envelopes(value, survey_id=survey_id)
