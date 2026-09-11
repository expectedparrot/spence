import pytest

from spence import forms
from spence.service import Service
from spence.store import Store


@pytest.fixture
def ats(tmp_path):
    store = Store.initialize(tmp_path / "workspace", provider="demo", owner="test")
    service = Service(store)
    service.save_design("role", "engineer", {"title": "Backend Engineer"})
    service.save_design("post", "advert", {"text": "# Engineer\nBuild useful things."})
    service.save_design("form", "short", forms.preset("form"))
    service.save_design("review_template", "rubric", forms.preset("review_template"))
    for id in ("alex", "sam"):
        service.save_design("reviewer", id, {"name": id.title(), "email": f"{id}@example.com"})
    service.create_opening("backend", "engineer")
    service.add_post("backend", "advert")
    yield service
    store.close()


@pytest.fixture
def answers():
    return {
        "candidate_name": "Taylor",
        "candidate_email": "taylor@example.com",
        "experience": "Built Python services",
        "motivation": "Interested in the role",
    }


@pytest.fixture
def publication(ats):
    return ats.publish(ats.prepare_application("backend", "advert", "short")["id"])


@pytest.fixture
def application(ats, publication, answers):
    ats.remote.submit(publication["remote"]["uuid"], answers, response_id="response-1")
    ats.sync("backend", "application")
    return ats.store.list("application")[0]
