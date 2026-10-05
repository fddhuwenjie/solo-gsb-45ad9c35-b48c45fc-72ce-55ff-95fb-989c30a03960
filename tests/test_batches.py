"""Tests for persisted, versioned review batches."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import batches, storage
from app.main import app
from app.models import BatchConfirmationRequest, BatchReassignmentRequest

client = TestClient(app)


def _input_payload():
    return {
        "manuscripts": [
            {"id": "m1", "topics": ["nlp", "ml"], "required_reviewers": 2},
            {"id": "m2", "topics": ["vision"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "scores": {"nlp": 5, "ml": 4}, "capacity": 2, "conflicts": ["m2"]},
            {"id": "r2", "scores": {"nlp": 3, "vision": 5}, "capacity": 1, "conflicts": []},
            {"id": "r3", "scores": {"ml": 4, "vision": 2}, "capacity": 2, "conflicts": []},
        ],
    }


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path):
    storage.reset(str(tmp_path / "batches.db"))
    yield
    storage.reset(str(tmp_path / "batches-clean.db"))


def _create(batch_id="b1", payload=None):
    body = dict(payload or _input_payload())
    body["batch_id"] = batch_id
    response = client.post("/batches", json=body)
    return response


# ---------------------------------------------------------------------------
# Creation and queries
# ---------------------------------------------------------------------------


def test_create_batch_solves_then_persists():
    response = _create()
    assert response.status_code == 201
    body = response.json()
    assert body["batch_id"] == "b1"
    assert body["version"] == 1
    assert body["confirmed"] == []
    assert body["input"] == _input_payload()
    assert body["assignment"]["status"] == "feasible"
    assert [
        (a["manuscript_id"], a["reviewer_id"]) for a in body["assignment"]["assignments"]
    ] == [("m1", "r1"), ("m1", "r3"), ("m2", "r2")]


def test_get_returns_current_input_assignment_confirmations_version():
    _create()
    got = client.get("/batches/b1").json()
    assert got["batch_id"] == "b1"
    assert got["version"] == 1
    assert got["input"]["reviewers"][2]["id"] == "r3"
    assert got["assignment"]["total_score"] == 18
    assert got["confirmed"] == []


def test_get_unknown_batch_is_404():
    assert client.get("/batches/ghost").status_code == 404


def test_duplicate_batch_id_does_not_overwrite():
    _create("b1")
    other = _input_payload()
    other["manuscripts"][0]["id"] = "changed"
    response = _create("b1", other)
    assert response.status_code == 409
    stored = client.get("/batches/b1").json()
    assert stored["input"]["manuscripts"][0]["id"] == "m1"


def test_infeasible_input_stores_nothing_and_allows_retry():
    infeasible = {
        "manuscripts": [{"id": "m1", "topics": ["t"], "required_reviewers": 2}],
        "reviewers": [
            {"id": "r1", "scores": {"t": 5}, "capacity": 5, "conflicts": []},
        ],
    }
    response = client.post("/batches", json={"batch_id": "b9", **infeasible})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["result"]["status"] == "infeasible"
    assert detail["result"]["proof"]["gap"] == 1
    assert client.get("/batches/b9").status_code == 404

    fixed = dict(infeasible)
    fixed["reviewers"] = infeasible["reviewers"] + [
        {"id": "r2", "scores": {"t": 4}, "capacity": 1, "conflicts": []}
    ]
    assert client.post("/batches", json={"batch_id": "b9", **fixed}).status_code == 201


def test_batch_assignment_equals_assign_endpoint():
    _create()
    direct = client.post("/assign", json=_input_payload()).json()
    stored = client.get("/batches/b1").json()["assignment"]
    assert stored == direct


# ---------------------------------------------------------------------------
# Confirmations
# ---------------------------------------------------------------------------


def test_confirm_registers_relation_and_bumps_version():
    _create()
    response = client.post(
        "/batches/b1/confirmations",
        json={"version": 1, "relations": [{"manuscript_id": "m1", "reviewer_id": "r1"}]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 2
    assert body["confirmed"] == [{"manuscript_id": "m1", "reviewer_id": "r1"}]


def test_confirm_multiple_new_relations_in_one_request():
    _create()
    response = client.post(
        "/batches/b1/confirmations",
        json={
            "version": 1,
            "relations": [
                {"manuscript_id": "m1", "reviewer_id": "r1"},
                {"manuscript_id": "m2", "reviewer_id": "r2"},
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["confirmed"] == [
        {"manuscript_id": "m1", "reviewer_id": "r1"},
        {"manuscript_id": "m2", "reviewer_id": "r2"},
    ]
    assert response.json()["version"] == 2


def test_confirm_stale_version_conflicts_and_reports_current():
    _create()
    client.post(
        "/batches/b1/confirmations",
        json={"version": 1, "relations": [{"manuscript_id": "m1", "reviewer_id": "r1"}]},
    )
    response = client.post(
        "/batches/b1/confirmations",
        json={"version": 1, "relations": [{"manuscript_id": "m2", "reviewer_id": "r2"}]},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "version_conflict",
        "message": "版本已过期，当前版本为 2",
        "current_version": 2,
    }
    # The stale request registered nothing.
    assert client.get("/batches/b1").json()["confirmed"] == [
        {"manuscript_id": "m1", "reviewer_id": "r1"}
    ]


def test_confirm_relation_outside_current_assignment_is_422():
    _create()
    # r1 conflicts with m2, so (m2, r1) is not in the assignment.
    response = client.post(
        "/batches/b1/confirmations",
        json={"version": 1, "relations": [{"manuscript_id": "m2", "reviewer_id": "r1"}]},
    )
    assert response.status_code == 422
    assert client.get("/batches/b1").json()["version"] == 1


def test_confirm_already_confirmed_is_422():
    _create()
    body = {"version": 1, "relations": [{"manuscript_id": "m1", "reviewer_id": "r1"}]}
    assert client.post("/batches/b1/confirmations", json=body).status_code == 200
    response = client.post(
        "/batches/b1/confirmations",
        json={"version": 2, "relations": [{"manuscript_id": "m1", "reviewer_id": "r1"}]},
    )
    assert response.status_code == 422


def test_confirm_duplicate_relations_in_request_is_422():
    _create()
    response = client.post(
        "/batches/b1/confirmations",
        json={
            "version": 1,
            "relations": [
                {"manuscript_id": "m1", "reviewer_id": "r1"},
                {"manuscript_id": "m1", "reviewer_id": "r1"},
            ],
        },
    )
    assert response.status_code == 422


def test_confirm_on_unknown_batch_is_404():
    response = client.post(
        "/batches/ghost/confirmations",
        json={"version": 1, "relations": [{"manuscript_id": "m1", "reviewer_id": "r1"}]},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Publishing a withdrawal as the next version
# ---------------------------------------------------------------------------


def test_publish_feasible_reassignment_reuses_solver_and_publishes_next_version():
    _create()
    client.post(
        "/batches/b1/confirmations",
        json={"version": 1, "relations": [{"manuscript_id": "m1", "reviewer_id": "r1"}]},
    )
    response = client.post(
        "/batches/b1/reassignments",
        json={"version": 2, "withdrawn_reviewer_id": "r2"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "feasible"
    assert body["version"] == 3
    assert body["batch_id"] == "b1"
    assert [(a["manuscript_id"], a["reviewer_id"]) for a in body["assignments"]] == [
        ("m1", "r1"),
        ("m1", "r3"),
        ("m2", "r3"),
    ]
    assert body["revoked"] == [{"manuscript_id": "m2", "reviewer_id": "r2"}]
    assert body["added"] == [{"manuscript_id": "m2", "reviewer_id": "r3"}]

    state = client.get("/batches/b1").json()
    assert state["version"] == 3
    assert [(a["manuscript_id"], a["reviewer_id"]) for a in state["assignment"]["assignments"]] == [
        ("m1", "r1"),
        ("m1", "r3"),
        ("m2", "r3"),
    ]
    # The other confirmation is preserved.
    assert state["confirmed"] == [{"manuscript_id": "m1", "reviewer_id": "r1"}]


def test_publish_matches_standalone_reassign_endpoint():
    _create()
    direct = client.post(
        "/reassign",
        json={
            **_input_payload(),
            "original_assignments": [
                {"manuscript_id": "m1", "reviewer_id": "r1"},
                {"manuscript_id": "m1", "reviewer_id": "r3"},
                {"manuscript_id": "m2", "reviewer_id": "r2"},
            ],
            "withdrawn_reviewer_id": "r2",
            "confirmed": [{"manuscript_id": "m1", "reviewer_id": "r1"}],
        },
    ).json()
    client.post(
        "/batches/b1/confirmations",
        json={"version": 1, "relations": [{"manuscript_id": "m1", "reviewer_id": "r1"}]},
    )
    published = client.post(
        "/batches/b1/reassignments",
        json={"version": 2, "withdrawn_reviewer_id": "r2"},
    ).json()
    for key in ("assignments", "revoked", "added", "total_score", "max_load_ratio", "loads"):
        assert published[key] == direct[key]


def test_publish_infeasible_leaves_batch_confirmations_version_unchanged():
    payload = _input_payload()
    payload["reviewers"][2]["capacity"] = 1  # r3 cannot cover both the m1 slot and m2
    _create(payload=payload)
    client.post(
        "/batches/b1/confirmations",
        json={"version": 1, "relations": [{"manuscript_id": "m1", "reviewer_id": "r1"}]},
    )
    response = client.post(
        "/batches/b1/reassignments",
        json={"version": 2, "withdrawn_reviewer_id": "r2"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "infeasible"
    assert body["reason"]
    assert body["version"] == 2

    state = client.get("/batches/b1").json()
    assert state["version"] == 2
    assert [(a["manuscript_id"], a["reviewer_id"]) for a in state["assignment"]["assignments"]] == [
        ("m1", "r1"),
        ("m1", "r3"),
        ("m2", "r2"),
    ]
    assert state["confirmed"] == [{"manuscript_id": "m1", "reviewer_id": "r1"}]

    # No version was consumed: retrying on the same version is still allowed.
    again = client.post(
        "/batches/b1/reassignments",
        json={"version": 2, "withdrawn_reviewer_id": "r2"},
    )
    assert again.status_code == 200
    assert again.json()["status"] == "infeasible"


def test_publish_stale_version_conflicts_and_does_not_change_state():
    _create()
    # Advance to version 2 via a confirmation.
    client.post(
        "/batches/b1/confirmations",
        json={"version": 1, "relations": [{"manuscript_id": "m1", "reviewer_id": "r1"}]},
    )
    response = client.post(
        "/batches/b1/reassignments",
        json={"version": 1, "withdrawn_reviewer_id": "r2"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["current_version"] == 2
    state = client.get("/batches/b1").json()
    assert state["version"] == 2
    # r2's original relation is still in place.
    assert ("m2", "r2") in {
        (a["manuscript_id"], a["reviewer_id"]) for a in state["assignment"]["assignments"]
    }


def test_publish_unknown_withdrawer_is_422():
    _create()
    response = client.post(
        "/batches/b1/reassignments",
        json={"version": 1, "withdrawn_reviewer_id": "ghost"},
    )
    assert response.status_code == 422
    assert client.get("/batches/b1").json()["version"] == 1


def test_publish_on_unknown_batch_is_404():
    response = client.post(
        "/batches/ghost/reassignments",
        json={"version": 1, "withdrawn_reviewer_id": "r2"},
    )
    assert response.status_code == 404


def test_chained_publish_carries_surviving_confirmations_and_drops_revoked():
    _create()
    client.post(
        "/batches/b1/confirmations",
        json={"version": 1, "relations": [{"manuscript_id": "m1", "reviewer_id": "r1"}]},
    )
    first = client.post(
        "/batches/b1/reassignments",
        json={"version": 2, "withdrawn_reviewer_id": "r2"},
    ).json()
    assert first["version"] == 3

    # m1-r1 survived and stays confirmed after a second withdrawal.
    second = client.post(
        "/batches/b1/reassignments",
        json={"version": 3, "withdrawn_reviewer_id": "r3"},
    ).json()
    assert second["status"] == "infeasible"
    state = client.get("/batches/b1").json()
    assert state["version"] == 3
    assert state["confirmed"] == [{"manuscript_id": "m1", "reviewer_id": "r1"}]


# ---------------------------------------------------------------------------
# Optimistic concurrency: only one mutating request per version succeeds
# ---------------------------------------------------------------------------


def test_concurrent_confirms_on_same_version_only_one_succeeds(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    db = str(tmp_path / "conc.db")
    storage.reset(db)
    _create()

    def confirm(_):
        try:
            return batches.register_confirmation(
                "b1",
                BatchConfirmationRequest(
                    version=1,
                    relations=[{"manuscript_id": "m1", "reviewer_id": "r1"}],
                ),
            )
        except batches.VersionConflict as conflict:
            return conflict

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(confirm, range(8)))

    # Exactly one success, seven version conflicts reporting current version 2.
    successes = [r for r in results if isinstance(r, dict)]
    conflicts = [r for r in results if isinstance(r, batches.VersionConflict)]
    assert len(successes) == 1
    assert successes[0]["version"] == 2
    assert len(conflicts) == 7
    assert all(conflict.current_version == 2 for conflict in conflicts)
    state = client.get("/batches/b1").json()
    assert state["version"] == 2
    assert state["confirmed"] == [{"manuscript_id": "m1", "reviewer_id": "r1"}]


def test_concurrent_publishes_on_same_version_only_one_succeeds():
    from concurrent.futures import ThreadPoolExecutor

    _create()

    def publish(_):
        try:
            return batches.publish_reassignment(
                "b1",
                BatchReassignmentRequest(version=1, withdrawn_reviewer_id="r2"),
            )
        except batches.VersionConflict as conflict:
            return conflict

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(publish, range(8)))

    feasible = [r for r in results if isinstance(r, dict) and r["status"] == "feasible"]
    conflicts = [r for r in results if isinstance(r, batches.VersionConflict)]
    assert len(feasible) == 1
    assert feasible[0]["version"] == 2
    assert len(conflicts) == 7
    assert all(conflict.current_version == 2 for conflict in conflicts)
    state = client.get("/batches/b1").json()
    assert state["version"] == 2


# ---------------------------------------------------------------------------
# Persistence across a simulated container restart
# ---------------------------------------------------------------------------


def test_state_survives_storage_reconnect(tmp_path):
    db = str(tmp_path / "persist.db")
    storage.reset(db)
    _create()
    client.post(
        "/batches/b1/confirmations",
        json={"version": 1, "relations": [{"manuscript_id": "m1", "reviewer_id": "r1"}]},
    )
    client.post(
        "/batches/b1/reassignments",
        json={"version": 2, "withdrawn_reviewer_id": "r2"},
    )

    # Simulate a new process: drop the connection and reopen the same file.
    storage.reset(db)

    state = client.get("/batches/b1").json()
    assert state["version"] == 3
    assert state["input"] == _input_payload()
    assert [(a["manuscript_id"], a["reviewer_id"]) for a in state["assignment"]["assignments"]] == [
        ("m1", "r1"),
        ("m1", "r3"),
        ("m2", "r3"),
    ]
    assert state["confirmed"] == [{"manuscript_id": "m1", "reviewer_id": "r1"}]


def test_database_file_is_sqlite(tmp_path):
    db = tmp_path / "sub" / "peer.db"
    storage.reset(str(db))
    _create()
    header = db.read_bytes()[:16]
    assert header.startswith(b"SQLite format 3")


def test_openapi_documents_batch_endpoints_and_legacy_ones():
    spec = client.get("/openapi.json").json()["paths"]
    for path in (
        "/assign",
        "/reassign",
        "/health",
        "/batches",
        "/batches/{batch_id}",
        "/batches/{batch_id}/confirmations",
        "/batches/{batch_id}/reassignments",
    ):
        assert path in spec, path
