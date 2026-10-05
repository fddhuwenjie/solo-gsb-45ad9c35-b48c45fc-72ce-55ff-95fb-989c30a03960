"""End-to-end tests for the peer-review assignment API."""
from __future__ import annotations

import itertools
import random
from fractions import Fraction

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def post(payload):
    return client.post("/assign", json=payload)


def brute_force(payload):
    """Reference solver: enumerate every complete assignment exhaustively.

    Returns None when infeasible, else (max_load_ratio, total_score, pairs)
    with pairs the lexicographically smallest optimal sorted pair list.
    """
    manuscripts = payload["manuscripts"]
    reviewers = payload["reviewers"]

    allowed = {}
    for manuscript in manuscripts:
        for reviewer in reviewers:
            if manuscript["id"] in reviewer.get("conflicts", []):
                continue
            score = sum(
                Fraction(str(reviewer.get("scores", {}).get(topic, 0)))
                for topic in manuscript["topics"]
            )
            if score > 0:
                allowed[(manuscript["id"], reviewer["id"])] = score

    per_manuscript = []
    for manuscript in manuscripts:
        candidates = sorted(
            rid for (mid, rid) in allowed if mid == manuscript["id"]
        )
        if len(candidates) < manuscript["required_reviewers"]:
            return None
        per_manuscript.append(
            list(itertools.combinations(candidates, manuscript["required_reviewers"]))
        )

    best = None
    for choice in itertools.product(*per_manuscript):
        pairs = []
        loads = {reviewer["id"]: 0 for reviewer in reviewers}
        for manuscript, combo in zip(manuscripts, choice):
            for reviewer_id in combo:
                pairs.append((manuscript["id"], reviewer_id))
                loads[reviewer_id] += 1
        if any(
            loads[reviewer["id"]] > reviewer["capacity"] for reviewer in reviewers
        ):
            continue
        max_ratio = max(
            (
                Fraction(loads[reviewer["id"]], reviewer["capacity"])
                for reviewer in reviewers
                if reviewer["capacity"] > 0
            ),
            default=Fraction(0),
        )
        total = sum(allowed[pair] for pair in pairs)
        key = (max_ratio, -total, sorted(pairs))
        if best is None or key < best[0]:
            best = (key, max_ratio, total, sorted(pairs))
    if best is None:
        return None
    _, max_ratio, total, pairs = best
    return max_ratio, total, pairs


def independent_gap_proof(payload):
    """Independently computed minimal capacity-gap proof (test oracle)."""
    manuscripts = payload["manuscripts"]
    reviewers = {r["id"]: r for r in payload["reviewers"]}

    usable = {m["id"]: set() for m in manuscripts}
    for manuscript in manuscripts:
        for reviewer in payload["reviewers"]:
            if manuscript["id"] in reviewer.get("conflicts", []):
                continue
            score = sum(
                reviewer.get("scores", {}).get(topic, 0)
                for topic in manuscript["topics"]
            )
            if score > 0:
                usable[manuscript["id"]].add(reviewer["id"])

    ids = sorted(m["id"] for m in manuscripts)
    for size in range(1, len(ids) + 1):
        for combo in itertools.combinations(ids, size):
            subset = set(combo)
            demand = sum(
                m["required_reviewers"] for m in manuscripts if m["id"] in subset
            )
            neighborhood = set().union(*(usable[m_id] for m_id in subset))
            cap_sum = 0
            for reviewer_id in neighborhood:
                serveable = sum(
                    1 for m_id in subset if reviewer_id in usable[m_id]
                )
                cap_sum += min(reviewers[reviewer_id]["capacity"], serveable)
            if demand > cap_sum:
                return {
                    "manuscripts": list(combo),
                    "reviewers": sorted(neighborhood),
                    "demand": demand,
                    "capacity": cap_sum,
                    "gap": demand - cap_sum,
                }
    return None


# ---------------------------------------------------------------------------
# Meta endpoints
# ---------------------------------------------------------------------------

def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_openapi_and_docs_exposed():
    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    paths = spec.json()["paths"]
    assert "/assign" in paths and "/health" in paths
    assert client.get("/docs").status_code == 200


# ---------------------------------------------------------------------------
# Feasible assignments
# ---------------------------------------------------------------------------

def test_basic_assignment_matches_hand_computed_result():
    payload = {
        "manuscripts": [
            {"id": "m1", "topics": ["nlp", "ml"], "required_reviewers": 2},
            {"id": "m2", "topics": ["vision"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "scores": {"nlp": 5, "ml": 4}, "capacity": 2, "conflicts": ["m2"]},
            {"id": "r2", "scores": {"nlp": 3, "vision": 5}, "capacity": 1, "conflicts": []},
            {"id": "r3", "scores": {"ml": 4, "vision": 2}, "capacity": 1, "conflicts": []},
        ],
    }
    response = post(payload)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "feasible"
    assert body["assignments"] == [
        {"manuscript_id": "m1", "reviewer_id": "r1", "score": 9},
        {"manuscript_id": "m1", "reviewer_id": "r3", "score": 4},
        {"manuscript_id": "m2", "reviewer_id": "r2", "score": 5},
    ]
    assert body["total_score"] == 18
    assert body["max_load_ratio"] == 1.0
    assert body["loads"] == {"r1": 1, "r2": 1, "r3": 1}


def test_load_ratio_minimised_before_score():
    # Putting both manuscripts on r1 would score 20 but force ratio 1.0;
    # splitting scores 19 with ratio 0.5, which must win.
    payload = {
        "manuscripts": [
            {"id": "m1", "topics": ["t"], "required_reviewers": 1},
            {"id": "m2", "topics": ["t"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "scores": {"t": 10}, "capacity": 2, "conflicts": []},
            {"id": "r2", "scores": {"t": 9}, "capacity": 2, "conflicts": []},
        ],
    }
    body = post(payload).json()
    assert body["status"] == "feasible"
    assert body["max_load_ratio"] == 0.5
    assert body["total_score"] == 19
    assert [(a["manuscript_id"], a["reviewer_id"]) for a in body["assignments"]] == [
        ("m1", "r1"),
        ("m2", "r2"),
    ]


def test_score_maximised_within_minimal_load_ratio():
    # Both splits give ratio 0.5; the higher-scoring one must be chosen.
    payload = {
        "manuscripts": [
            {"id": "m1", "topics": ["t"], "required_reviewers": 1},
            {"id": "m2", "topics": ["t"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "scores": {"t": 10}, "capacity": 2, "conflicts": []},
            {"id": "r2", "scores": {"t": 1}, "capacity": 2, "conflicts": []},
            {"id": "r3", "scores": {"t": 9}, "capacity": 2, "conflicts": []},
        ],
    }
    body = post(payload).json()
    assert body["status"] == "feasible"
    assert body["max_load_ratio"] == 0.5
    assert body["total_score"] == 19
    assert {(a["manuscript_id"], a["reviewer_id"]) for a in body["assignments"]} == {
        ("m1", "r1"),
        ("m2", "r3"),
    }


def test_lexicographic_tie_break():
    # Identical scores and ratios: only the lexicographic order decides.
    payload = {
        "manuscripts": [
            {"id": "m1", "topics": ["t"], "required_reviewers": 1},
            {"id": "m2", "topics": ["t"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "scores": {"t": 5}, "capacity": 2, "conflicts": []},
            {"id": "r2", "scores": {"t": 5}, "capacity": 2, "conflicts": []},
        ],
    }
    body = post(payload).json()
    assert [(a["manuscript_id"], a["reviewer_id"]) for a in body["assignments"]] == [
        ("m1", "r1"),
        ("m2", "r2"),
    ]


def test_fractional_load_ratio_and_capacity_floor():
    # 4 manuscripts, two reviewers with capacity 3: optimal ratio is 2/3,
    # which floors each reviewer's effective capacity to 2.
    payload = {
        "manuscripts": [
            {"id": f"m{i}", "topics": ["t"], "required_reviewers": 1}
            for i in range(1, 5)
        ],
        "reviewers": [
            {"id": "r1", "scores": {"t": 5}, "capacity": 3, "conflicts": []},
            {"id": "r2", "scores": {"t": 5}, "capacity": 3, "conflicts": []},
        ],
    }
    body = post(payload).json()
    assert body["status"] == "feasible"
    assert body["max_load_ratio"] == pytest.approx(2 / 3)
    assert body["loads"] == {"r1": 2, "r2": 2}
    assert [(a["manuscript_id"], a["reviewer_id"]) for a in body["assignments"]] == [
        ("m1", "r1"),
        ("m2", "r1"),
        ("m3", "r2"),
        ("m4", "r2"),
    ]


def test_conflicted_and_zero_score_pairs_are_unusable():
    payload = {
        "manuscripts": [
            {"id": "m1", "topics": ["a", "b"], "required_reviewers": 2},
        ],
        "reviewers": [
            {"id": "r1", "scores": {"a": 3, "b": 2}, "capacity": 5, "conflicts": []},
            {"id": "r2", "scores": {"a": 9, "b": 9}, "capacity": 5, "conflicts": ["m1"]},
            {"id": "r3", "scores": {"a": 0, "b": 0}, "capacity": 5, "conflicts": []},
            {"id": "r4", "scores": {"b": 1}, "capacity": 5, "conflicts": []},
        ],
    }
    body = post(payload).json()
    assert body["status"] == "feasible"
    assigned = {a["reviewer_id"] for a in body["assignments"]}
    assert assigned == {"r1", "r4"}  # r2 conflicted, r3 has score 0
    scores = {a["reviewer_id"]: a["score"] for a in body["assignments"]}
    assert scores == {"r1": 5, "r4": 1}


def test_score_sums_all_topics_and_missing_topics_count_zero():
    payload = {
        "manuscripts": [
            {"id": "m1", "topics": ["a", "b", "c"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "scores": {"a": 1.5, "b": 2.25}, "capacity": 1, "conflicts": []},
        ],
    }
    body = post(payload).json()
    assert body["status"] == "feasible"
    assert body["assignments"][0]["score"] == 3.75
    assert body["total_score"] == 3.75


# ---------------------------------------------------------------------------
# Infeasible instances and capacity-gap proofs
# ---------------------------------------------------------------------------

def test_no_reviewer_reuse_forces_effective_capacity():
    # One reviewer with huge capacity still counts only once per manuscript.
    payload = {
        "manuscripts": [
            {"id": "m1", "topics": ["t"], "required_reviewers": 2},
        ],
        "reviewers": [
            {"id": "r1", "scores": {"t": 5}, "capacity": 5, "conflicts": []},
        ],
    }
    body = post(payload).json()
    assert body["status"] == "infeasible"
    assert body["proof"] == {
        "manuscripts": ["m1"],
        "reviewers": ["r1"],
        "demand": 2,
        "capacity": 1,
        "gap": 1,
    }


def test_gap_proof_over_manuscript_subset():
    payload = {
        "manuscripts": [
            {"id": "m1", "topics": ["nlp"], "required_reviewers": 2},
            {"id": "m2", "topics": ["nlp"], "required_reviewers": 2},
        ],
        "reviewers": [
            {"id": "r1", "scores": {"nlp": 5}, "capacity": 2, "conflicts": []},
            {"id": "r2", "scores": {"nlp": 4}, "capacity": 1, "conflicts": []},
            {"id": "r3", "scores": {"nlp": 3}, "capacity": 1, "conflicts": ["m1", "m2"]},
        ],
    }
    body = post(payload).json()
    assert body["status"] == "infeasible"
    assert body["proof"] == {
        "manuscripts": ["m1", "m2"],
        "reviewers": ["r1", "r2"],
        "demand": 4,
        "capacity": 3,
        "gap": 1,
    }


def test_gap_proof_prefers_fewest_manuscripts():
    # {m1} alone is infeasible; {m2, m3} is infeasible too. The singleton wins.
    payload = {
        "manuscripts": [
            {"id": "m1", "topics": ["t"], "required_reviewers": 2},
            {"id": "m2", "topics": ["u"], "required_reviewers": 2},
            {"id": "m3", "topics": ["u"], "required_reviewers": 2},
        ],
        "reviewers": [
            {"id": "r1", "scores": {"t": 5}, "capacity": 5, "conflicts": []},
            {"id": "r2", "scores": {"u": 5}, "capacity": 1, "conflicts": []},
            {"id": "r3", "scores": {"u": 5}, "capacity": 1, "conflicts": []},
        ],
    }
    body = post(payload).json()
    assert body["status"] == "infeasible"
    assert body["proof"]["manuscripts"] == ["m1"]
    assert body["proof"]["reviewers"] == ["r1"]
    assert body["proof"]["demand"] == 2
    assert body["proof"]["capacity"] == 1


def test_gap_proof_lexicographic_order_among_equal_sizes():
    # Every pair of manuscripts violates; {m1, m2} is lexicographically smallest.
    payload = {
        "manuscripts": [
            {"id": "m1", "topics": ["t"], "required_reviewers": 2},
            {"id": "m2", "topics": ["t"], "required_reviewers": 2},
            {"id": "m3", "topics": ["t"], "required_reviewers": 2},
        ],
        "reviewers": [
            {"id": "r1", "scores": {"t": 1}, "capacity": 1, "conflicts": []},
            {"id": "r2", "scores": {"t": 1}, "capacity": 1, "conflicts": ["m3"]},
            {"id": "r3", "scores": {"t": 1}, "capacity": 1, "conflicts": ["m2"]},
        ],
    }
    body = post(payload).json()
    assert body["status"] == "infeasible"
    assert body["proof"]["manuscripts"] == ["m1", "m2"]
    assert body["proof"]["reviewers"] == ["r1", "r2", "r3"]
    assert body["proof"]["demand"] == 4
    assert body["proof"]["capacity"] == 3
    assert body["proof"]["gap"] == 1


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _valid_payload():
    return {
        "manuscripts": [{"id": "m1", "topics": ["t"], "required_reviewers": 1}],
        "reviewers": [{"id": "r1", "scores": {"t": 1}, "capacity": 1, "conflicts": []}],
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(manuscripts=[]),
        lambda p: p.update(
            manuscripts=[
                {"id": f"m{i}", "topics": ["t"], "required_reviewers": 1}
                for i in range(11)
            ]
        ),
        lambda p: p.update(reviewers=[]),
        lambda p: p.update(
            reviewers=[
                {"id": f"r{i}", "scores": {"t": 1}, "capacity": 1, "conflicts": []}
                for i in range(21)
            ]
        ),
        lambda p: p["manuscripts"].append(
            {"id": "m1", "topics": ["t"], "required_reviewers": 1}
        ),
        lambda p: p["reviewers"].append(
            {"id": "r1", "scores": {"t": 1}, "capacity": 1, "conflicts": []}
        ),
        lambda p: p["manuscripts"][0].update(required_reviewers=0),
        lambda p: p["manuscripts"][0].update(topics=[]),
        lambda p: p["manuscripts"][0].update(topics=["t", "t"]),
        lambda p: p["reviewers"][0].update(capacity=-1),
        lambda p: p["reviewers"][0]["scores"].update(t=-1),
        lambda p: p["reviewers"][0].update(conflicts=["ghost"]),
    ],
)
def test_invalid_payloads_return_422(mutate):
    payload = _valid_payload()
    mutate(payload)
    assert post(payload).status_code == 422


# ---------------------------------------------------------------------------
# Randomised cross-check against the brute-force reference
# ---------------------------------------------------------------------------

def _random_payload(rng: random.Random):
    n_manuscripts = rng.randint(1, 4)
    n_reviewers = rng.randint(1, 6)
    topics = ["t1", "t2", "t3"]
    manuscripts = [
        {
            "id": f"m{i}",
            "topics": rng.sample(topics, rng.randint(1, len(topics))),
            "required_reviewers": rng.randint(1, 2),
        }
        for i in range(1, n_manuscripts + 1)
    ]
    reviewers = []
    for j in range(1, n_reviewers + 1):
        scores = {
            topic: rng.randint(0, 3)
            for topic in topics
            if rng.random() < 0.7
        }
        conflicts = [
            m["id"] for m in manuscripts if rng.random() < 0.15
        ]
        reviewers.append(
            {
                "id": f"r{j}",
                "scores": scores,
                "capacity": rng.randint(0, 3),
                "conflicts": conflicts,
            }
        )
    return {"manuscripts": manuscripts, "reviewers": reviewers}


def test_solver_matches_brute_force_on_random_instances():
    rng = random.Random(20260925)
    for _ in range(200):
        payload = _random_payload(rng)
        response = post(payload)
        assert response.status_code == 200
        body = response.json()
        reference = brute_force(payload)
        if reference is None:
            assert body["status"] == "infeasible"
            assert body["proof"] == independent_gap_proof(payload)
            continue
        max_ratio, total, pairs = reference
        assert body["status"] == "feasible"
        got_pairs = sorted(
            (a["manuscript_id"], a["reviewer_id"]) for a in body["assignments"]
        )
        assert got_pairs == pairs
        assert Fraction(str(body["total_score"])) == total
        assert Fraction(body["max_load_ratio"]).limit_denominator(10**9) == max_ratio


# ---------------------------------------------------------------------------
# Emergency reassignment (POST /reassign)
# ---------------------------------------------------------------------------

def post_reassign(payload):
    return client.post("/reassign", json=payload)


def _reassign_payload():
    """Valid base instance: r2 withdraws from a three-reviewer assignment."""
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
        "original_assignments": [
            {"manuscript_id": "m1", "reviewer_id": "r1"},
            {"manuscript_id": "m1", "reviewer_id": "r3"},
            {"manuscript_id": "m2", "reviewer_id": "r2"},
        ],
        "withdrawn_reviewer_id": "r2",
        "confirmed": [{"manuscript_id": "m1", "reviewer_id": "r1"}],
    }


def test_reassign_endpoint_exposed():
    spec = client.get("/openapi.json")
    assert "/reassign" in spec.json()["paths"]


def test_reassign_basic_hand_computed():
    response = post_reassign(_reassign_payload())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "feasible"
    # m2 must move from the withdrawn r2 to r3; the confirmed m1-r1 and the
    # remaining original m1-r3 are kept.
    assert body["assignments"] == [
        {"manuscript_id": "m1", "reviewer_id": "r1", "score": 9},
        {"manuscript_id": "m1", "reviewer_id": "r3", "score": 4},
        {"manuscript_id": "m2", "reviewer_id": "r3", "score": 2},
    ]
    assert body["revoked"] == [{"manuscript_id": "m2", "reviewer_id": "r2"}]
    assert body["added"] == [{"manuscript_id": "m2", "reviewer_id": "r3"}]
    assert body["loads"] == {"r1": 1, "r2": 0, "r3": 2}
    assert body["total_score"] == 15
    assert body["max_load_ratio"] == 1.0


def test_reassign_revokes_all_withdrawn_relations():
    payload = _reassign_payload()
    payload["confirmed"] = []
    body = post_reassign(payload).json()
    assert body["status"] == "feasible"
    assert body["revoked"] == [{"manuscript_id": "m2", "reviewer_id": "r2"}]
    assert all(a["reviewer_id"] != "r2" for a in body["assignments"])
    # Both remaining original relations are retained.
    assert body["added"] == [{"manuscript_id": "m2", "reviewer_id": "r3"}]


def test_reassign_retention_beats_score():
    # Keeping the original m2-r2 scores only 2 in total; dropping it would
    # score 101. Retention must win over the total score.
    payload = {
        "manuscripts": [
            {"id": "m1", "topics": ["a"], "required_reviewers": 1},
            {"id": "m2", "topics": ["b"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "scores": {"a": 5, "b": 5}, "capacity": 2, "conflicts": []},
            {"id": "r2", "scores": {"a": 100, "b": 1}, "capacity": 2, "conflicts": []},
            {"id": "r3", "scores": {"a": 1, "b": 1}, "capacity": 2, "conflicts": []},
        ],
        "original_assignments": [
            {"manuscript_id": "m1", "reviewer_id": "r1"},
            {"manuscript_id": "m2", "reviewer_id": "r2"},
        ],
        "withdrawn_reviewer_id": "r1",
        "confirmed": [],
    }
    body = post_reassign(payload).json()
    assert body["status"] == "feasible"
    assert [(a["manuscript_id"], a["reviewer_id"]) for a in body["assignments"]] == [
        ("m1", "r3"),
        ("m2", "r2"),
    ]
    assert body["total_score"] == 2
    assert body["revoked"] == [{"manuscript_id": "m1", "reviewer_id": "r1"}]
    assert body["added"] == [{"manuscript_id": "m1", "reviewer_id": "r3"}]


def test_reassign_load_ratio_second_priority():
    # No original relation can be retained; splitting the load (ratio 0.5)
    # must beat piling both manuscripts on the high scorer r2 (ratio 1.0).
    payload = {
        "manuscripts": [
            {"id": "m1", "topics": ["a"], "required_reviewers": 1},
            {"id": "m2", "topics": ["b"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "scores": {"a": 5, "b": 5}, "capacity": 2, "conflicts": []},
            {"id": "r2", "scores": {"a": 10, "b": 10}, "capacity": 2, "conflicts": []},
            {"id": "r3", "scores": {"a": 1, "b": 1}, "capacity": 2, "conflicts": []},
        ],
        "original_assignments": [
            {"manuscript_id": "m1", "reviewer_id": "r1"},
            {"manuscript_id": "m2", "reviewer_id": "r1"},
        ],
        "withdrawn_reviewer_id": "r1",
        "confirmed": [],
    }
    body = post_reassign(payload).json()
    assert body["status"] == "feasible"
    assert body["max_load_ratio"] == 0.5
    assert [(a["manuscript_id"], a["reviewer_id"]) for a in body["assignments"]] == [
        ("m1", "r2"),
        ("m2", "r3"),
    ]
    assert body["loads"] == {"r1": 0, "r2": 1, "r3": 1}


def test_reassign_infeasible_returns_no_partial_assignment():
    payload = {
        "manuscripts": [
            {"id": "m1", "topics": ["t"], "required_reviewers": 2},
        ],
        "reviewers": [
            {"id": "r1", "scores": {"t": 5}, "capacity": 2, "conflicts": []},
            {"id": "r2", "scores": {"t": 5}, "capacity": 1, "conflicts": []},
        ],
        "original_assignments": [
            {"manuscript_id": "m1", "reviewer_id": "r1"},
            {"manuscript_id": "m1", "reviewer_id": "r2"},
        ],
        "withdrawn_reviewer_id": "r1",
        "confirmed": [],
    }
    response = post_reassign(payload)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "infeasible"
    assert body["reason"]
    assert "assignments" not in body


def test_reassign_confirmed_zero_score_pair_is_infeasible():
    # The confirmed relation is not a usable pair (score 0), so no complete
    # reassignment can keep it.
    payload = {
        "manuscripts": [
            {"id": "m1", "topics": ["nlp"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "scores": {"nlp": 5}, "capacity": 1, "conflicts": []},
            {"id": "r2", "scores": {}, "capacity": 1, "conflicts": []},
        ],
        "original_assignments": [{"manuscript_id": "m1", "reviewer_id": "r2"}],
        "withdrawn_reviewer_id": "r1",
        "confirmed": [{"manuscript_id": "m1", "reviewer_id": "r2"}],
    }
    body = post_reassign(payload).json()
    assert body["status"] == "infeasible"
    assert "assignments" not in body


def test_reassign_confirmed_covering_all_demands():
    # The withdrawn reviewer had no relations and every original relation is
    # confirmed, so the confirmed set is returned unchanged.
    payload = _reassign_payload()
    payload["original_assignments"] = [
        {"manuscript_id": "m1", "reviewer_id": "r1"},
        {"manuscript_id": "m1", "reviewer_id": "r3"},
        {"manuscript_id": "m2", "reviewer_id": "r3"},
    ]
    payload["confirmed"] = [
        {"manuscript_id": "m1", "reviewer_id": "r1"},
        {"manuscript_id": "m1", "reviewer_id": "r3"},
        {"manuscript_id": "m2", "reviewer_id": "r3"},
    ]
    body = post_reassign(payload).json()
    assert body["status"] == "feasible"
    assert [(a["manuscript_id"], a["reviewer_id"]) for a in body["assignments"]] == [
        ("m1", "r1"),
        ("m1", "r3"),
        ("m2", "r3"),
    ]
    assert body["revoked"] == []
    assert body["added"] == []
    assert body["loads"] == {"r1": 1, "r2": 0, "r3": 2}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(withdrawn_reviewer_id="ghost"),
        lambda p: p.update(original_assignments=[]),
        lambda p: p["original_assignments"].pop(),  # m2 left without reviewers
        lambda p: p["original_assignments"].append(
            {"manuscript_id": "m1", "reviewer_id": "r2"}
        ),  # m1 gets 3 reviewers, needs 2
        lambda p: p["original_assignments"].append(
            {"manuscript_id": "m1", "reviewer_id": "r1"}
        ),  # duplicate relation
        lambda p: p["original_assignments"].__setitem__(
            2, {"manuscript_id": "m2", "reviewer_id": "r1"}
        ),  # r1 conflicts with m2
        lambda p: p.update(
            original_assignments=[
                {"manuscript_id": "m1", "reviewer_id": "r1"},
                {"manuscript_id": "m1", "reviewer_id": "r2"},
                {"manuscript_id": "m2", "reviewer_id": "r2"},
            ]
        ),  # r2 (capacity 1) overloaded
        lambda p: p["original_assignments"].append(
            {"manuscript_id": "ghost", "reviewer_id": "r1"}
        ),
        lambda p: p["original_assignments"].append(
            {"manuscript_id": "m1", "reviewer_id": "ghost"}
        ),
        lambda p: p.update(
            confirmed=[{"manuscript_id": "m1", "reviewer_id": "r2"}]
        ),  # not part of the original assignment
        lambda p: p.update(
            confirmed=[{"manuscript_id": "m2", "reviewer_id": "r2"}]
        ),  # confirmed relation of the withdrawn reviewer
        lambda p: p.update(
            confirmed=[
                {"manuscript_id": "m1", "reviewer_id": "r1"},
                {"manuscript_id": "m1", "reviewer_id": "r1"},
            ]
        ),  # duplicate confirmed relation
    ],
)
def test_reassign_invalid_payloads_return_422(mutate):
    payload = _reassign_payload()
    mutate(payload)
    response = post_reassign(payload)
    assert response.status_code == 422
    assert response.json()["detail"]


def brute_force_reassign(payload):
    """Reference emergency reassignment: enumerate every complete assignment
    over the remaining reviewers that keeps all confirmed relations.

    Returns None when infeasible, else the optimal sorted pair list under
    (max retained, min max_load_ratio, max total_score, lexicographic).
    """
    manuscripts = payload["manuscripts"]
    reviewers = payload["reviewers"]
    withdrawn = payload["withdrawn_reviewer_id"]
    active = [r for r in reviewers if r["id"] != withdrawn]
    original = {
        (relation["manuscript_id"], relation["reviewer_id"])
        for relation in payload["original_assignments"]
    }
    confirmed = {
        (relation["manuscript_id"], relation["reviewer_id"])
        for relation in payload["confirmed"]
    }

    allowed = {}
    for manuscript in manuscripts:
        for reviewer in active:
            if manuscript["id"] in reviewer.get("conflicts", []):
                continue
            score = sum(
                Fraction(str(reviewer.get("scores", {}).get(topic, 0)))
                for topic in manuscript["topics"]
            )
            if score > 0:
                allowed[(manuscript["id"], reviewer["id"])] = score

    if any(pair not in allowed for pair in confirmed):
        return None

    per_manuscript = []
    for manuscript in manuscripts:
        must = {rid for (mid, rid) in confirmed if mid == manuscript["id"]}
        rest = sorted(
            rid
            for (mid, rid) in allowed
            if mid == manuscript["id"] and rid not in must
        )
        need = manuscript["required_reviewers"] - len(must)
        if need < 0 or len(rest) < need:
            return None
        per_manuscript.append(
            [must | set(extra) for extra in itertools.combinations(rest, need)]
        )

    best = None
    for choice in itertools.product(*per_manuscript):
        pairs = []
        loads = {reviewer["id"]: 0 for reviewer in reviewers}
        for manuscript, combo in zip(manuscripts, choice):
            for reviewer_id in combo:
                pairs.append((manuscript["id"], reviewer_id))
                loads[reviewer_id] += 1
        if any(loads[r["id"]] > r["capacity"] for r in active):
            continue
        retained = len(set(pairs) & original)
        max_ratio = max(
            (
                Fraction(loads[r["id"]], r["capacity"])
                for r in active
                if r["capacity"] > 0
            ),
            default=Fraction(0),
        )
        total = sum(allowed[pair] for pair in pairs)
        key = (-retained, max_ratio, -total, sorted(pairs))
        if best is None or key < best[0]:
            best = (key, sorted(pairs))
    if best is None:
        return None
    return best[1]


def test_reassign_matches_brute_force_on_random_instances():
    rng = random.Random(20261005)
    tested = 0
    while tested < 150:
        payload = _random_payload(rng)
        reference_original = brute_force(payload)
        if reference_original is None:
            continue
        tested += 1
        original_pairs = reference_original[2]
        withdrawn = rng.choice(payload["reviewers"])["id"]
        confirmed_pairs = [
            pair
            for pair in original_pairs
            if pair[1] != withdrawn and rng.random() < 0.5
        ]
        payload["original_assignments"] = [
            {"manuscript_id": m_id, "reviewer_id": r_id}
            for m_id, r_id in original_pairs
        ]
        payload["withdrawn_reviewer_id"] = withdrawn
        payload["confirmed"] = [
            {"manuscript_id": m_id, "reviewer_id": r_id}
            for m_id, r_id in confirmed_pairs
        ]

        response = post_reassign(payload)
        assert response.status_code == 200
        body = response.json()
        reference = brute_force_reassign(payload)
        if reference is None:
            assert body["status"] == "infeasible"
            assert "assignments" not in body
            continue

        assert body["status"] == "feasible"
        got = sorted(
            (a["manuscript_id"], a["reviewer_id"]) for a in body["assignments"]
        )
        assert got == reference

        original_set = set(original_pairs)
        new_set = set(got)
        assert sorted(
            (r["manuscript_id"], r["reviewer_id"]) for r in body["revoked"]
        ) == sorted(original_set - new_set)
        assert sorted(
            (a["manuscript_id"], a["reviewer_id"]) for a in body["added"]
        ) == sorted(new_set - original_set)
        # The withdrawn reviewer loses every relation; confirmed ones stay.
        assert all(reviewer_id != withdrawn for _, reviewer_id in got)
        assert set(confirmed_pairs) <= new_set

        expected_loads = {r["id"]: 0 for r in payload["reviewers"]}
        for _, reviewer_id in got:
            expected_loads[reviewer_id] += 1
        assert body["loads"] == expected_loads
