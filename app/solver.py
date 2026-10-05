"""Peer-review assignment solver built on NetworkX flow algorithms.

Rules implemented here:

* The match score of a (manuscript, reviewer) pair is the sum of the
  reviewer's scores over the manuscript's topics.
* A pair is usable only when the reviewer is not in conflict with the
  manuscript and the pair score is strictly positive.
* A reviewer may be assigned to the same manuscript at most once and may
  review at most ``capacity`` manuscripts in total.
* A complete assignment first minimises the maximum reviewer load ratio
  (assigned / capacity), then maximises the total match score, and breaks
  remaining ties by choosing the lexicographically smallest sorted list of
  ``(manuscript_id, reviewer_id)`` pairs.
* When no complete assignment exists, a Hall-style capacity-gap proof is
  produced: the subset of manuscripts (fewest first, then lexicographic by
  id) whose demand exceeds the effective capacity of its usable reviewers.
* ``reassign`` handles a reviewer withdrawal: it revokes every original
  relation of the withdrawn reviewer, keeps all confirmed relations, and
  among complete reassignments over the remaining reviewers first retains
  as many of the other original relations as possible, then minimises the
  maximum load ratio, then maximises the total match score, and finally
  takes the lexicographically smallest pair list.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from itertools import combinations
from math import lcm

import networkx as nx

_SOURCE = ("__source__",)
_SINK = ("__sink__",)


@dataclass(frozen=True)
class Manuscript:
    id: str
    topics: tuple[str, ...]
    required: int


@dataclass(frozen=True)
class Reviewer:
    id: str
    scores: dict[str, Fraction]
    capacity: int
    conflicts: frozenset[str]


def _m_node(manuscript_id: str) -> tuple[str, str]:
    return ("m", manuscript_id)


def _r_node(reviewer_id: str) -> tuple[str, str]:
    return ("r", reviewer_id)


def _floor(value: Fraction) -> int:
    return value.numerator // value.denominator


def _as_number(value: Fraction):
    """Render an exact fraction as int when integral, else float."""
    if value.denominator == 1:
        return value.numerator
    return float(value)


def allowed_pairs(
    manuscripts: list[Manuscript], reviewers: list[Reviewer]
) -> dict[tuple[str, str], Fraction]:
    """Usable (manuscript_id, reviewer_id) pairs with their match score.

    Conflicted pairs and pairs whose total topic score is 0 are excluded.
    """
    pairs: dict[tuple[str, str], Fraction] = {}
    for manuscript in manuscripts:
        for reviewer in reviewers:
            if manuscript.id in reviewer.conflicts:
                continue
            score = sum(
                (reviewer.scores.get(topic, Fraction(0)) for topic in manuscript.topics),
                Fraction(0),
            )
            if score > 0:
                pairs[(manuscript.id, reviewer.id)] = score
    return pairs


def _build_flow_network(
    manuscripts: list[Manuscript],
    reviewers: list[Reviewer],
    pairs: dict[tuple[str, str], Fraction],
    capacities: dict[str, int],
) -> nx.DiGraph:
    """Bipartite b-matching network: source -> manuscripts -> reviewers -> sink."""
    graph = nx.DiGraph()
    for manuscript in manuscripts:
        graph.add_edge(_SOURCE, _m_node(manuscript.id), capacity=manuscript.required)
    for manuscript_id, reviewer_id in pairs:
        # Capacity 1: a reviewer may serve the same manuscript at most once.
        graph.add_edge(_m_node(manuscript_id), _r_node(reviewer_id), capacity=1)
    for reviewer in reviewers:
        graph.add_edge(_r_node(reviewer.id), _SINK, capacity=capacities[reviewer.id])
    return graph


def _max_flow_value(
    manuscripts: list[Manuscript],
    reviewers: list[Reviewer],
    pairs: dict[tuple[str, str], Fraction],
    capacities: dict[str, int],
) -> int:
    graph = _build_flow_network(manuscripts, reviewers, pairs, capacities)
    value, _ = nx.maximum_flow(graph, _SOURCE, _SINK)
    return value


def _minimal_max_load_ratio(
    manuscripts: list[Manuscript],
    reviewers: list[Reviewer],
    pairs: dict[tuple[str, str], Fraction],
    demand: int,
) -> Fraction:
    """Smallest t such that capping every reviewer at floor(t * capacity)
    still admits a complete assignment. The caller guarantees the instance
    is feasible at full capacity."""
    candidates: set[Fraction] = {Fraction(0)}
    for reviewer in reviewers:
        if reviewer.capacity <= 0:
            continue
        # No reviewer can take more manuscripts than exist.
        for load in range(1, min(reviewer.capacity, len(manuscripts)) + 1):
            candidates.add(Fraction(load, reviewer.capacity))
    ordered = sorted(candidates)

    def feasible(ratio: Fraction) -> bool:
        caps = {r.id: _floor(ratio * r.capacity) for r in reviewers}
        return _max_flow_value(manuscripts, reviewers, pairs, caps) >= demand

    # ordered[-1] is feasible: it allows every reviewer at least
    # min(capacity, #manuscripts) assignments, which full capacity never exceeds.
    lo, hi = 0, len(ordered) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if feasible(ordered[mid]):
            hi = mid
        else:
            lo = mid + 1
    return ordered[lo]


def _solve_optimal(
    manuscripts: list[Manuscript],
    reviewers: list[Reviewer],
    pairs: dict[tuple[str, str], Fraction],
    demand: int,
    ratio: Fraction,
) -> tuple[list[tuple[str, str]], dict[str, int]]:
    """Complete assignment under the load-ratio cap that first maximises the
    total score, then is lexicographically smallest.

    Implemented as a single min-cost flow: each pair edge costs
    ``-(scaled_score * 2^M) - 2^(M-1-rank)`` where ``rank`` is the pair's
    position in lexicographic order. The score term dominates any sum of
    lexicographic weights. Reading the assignment as a bit vector over the
    ordered pairs, the lexicographically smallest sorted pair list has the
    *largest* exponential weight sum (its most significant differing bit is
    1), so minimising the negated sum yields it.
    """
    caps = {r.id: _floor(ratio * r.capacity) for r in reviewers}

    # Scale possibly fractional scores to integers for exact arithmetic.
    denom = 1
    for score in pairs.values():
        denom = lcm(denom, score.denominator)
    int_score = {
        pair: score.numerator * (denom // score.denominator)
        for pair, score in pairs.items()
    }

    ordered_pairs = sorted(pairs)  # lexicographic by (manuscript_id, reviewer_id)
    n_pairs = len(ordered_pairs)
    score_weight = 1 << n_pairs  # dominates the sum of all lexicographic weights

    reviewers_in_pairs = {reviewer_id for _, reviewer_id in ordered_pairs}

    graph = nx.DiGraph()
    graph.add_node(_SOURCE, demand=-demand)
    graph.add_node(_SINK, demand=demand)
    for manuscript in manuscripts:
        graph.add_edge(
            _SOURCE, _m_node(manuscript.id), capacity=manuscript.required, weight=0
        )
    for rank, (manuscript_id, reviewer_id) in enumerate(ordered_pairs):
        lex_weight = 1 << (n_pairs - 1 - rank)
        weight = -(int_score[(manuscript_id, reviewer_id)] * score_weight) - lex_weight
        graph.add_edge(
            _m_node(manuscript_id), _r_node(reviewer_id), capacity=1, weight=weight
        )
    for reviewer in reviewers:
        if reviewer.id in reviewers_in_pairs:
            graph.add_edge(
                _r_node(reviewer.id), _SINK, capacity=caps[reviewer.id], weight=0
            )

    _, flow = nx.network_simplex(graph)

    assignments = [
        (manuscript_id, reviewer_id)
        for manuscript_id, reviewer_id in ordered_pairs
        if flow.get(_m_node(manuscript_id), {}).get(_r_node(reviewer_id), 0)
    ]
    loads = {
        reviewer.id: flow.get(_r_node(reviewer.id), {}).get(_SINK, 0)
        for reviewer in reviewers
    }
    return assignments, loads


def find_capacity_gap_proof(
    manuscripts: list[Manuscript],
    reviewers: list[Reviewer],
    pairs: dict[tuple[str, str], Fraction],
) -> dict:
    """Smallest Hall-style certificate that no complete assignment exists.

    For a subset S of manuscripts, the usable reviewers N(S) are those that
    may serve at least one member of S. Because a reviewer may serve each
    manuscript at most once, its effective capacity towards S is
    ``min(capacity, number of S-manuscripts it may serve)``. When the demand
    of S exceeds the summed effective capacity, no complete assignment can
    exist (max-flow/min-cut). Returns the violating subset with the fewest
    manuscripts, breaking ties by lexicographic manuscript-id order.
    """
    required = {m.id: m.required for m in manuscripts}
    capacity = {r.id: r.capacity for r in reviewers}
    usable: dict[str, set[str]] = {m.id: set() for m in manuscripts}
    for manuscript_id, reviewer_id in pairs:
        usable[manuscript_id].add(reviewer_id)

    ids = sorted(required)
    for size in range(1, len(ids) + 1):
        for combo in combinations(ids, size):
            subset = set(combo)
            demand = sum(required[m_id] for m_id in subset)
            neighborhood: set[str] = set().union(*(usable[m_id] for m_id in subset))
            cap_sum = 0
            for reviewer_id in neighborhood:
                serveable = sum(1 for m_id in subset if reviewer_id in usable[m_id])
                cap_sum += min(capacity[reviewer_id], serveable)
            if demand > cap_sum:
                return {
                    "manuscripts": list(combo),
                    "reviewers": sorted(neighborhood),
                    "demand": demand,
                    "capacity": cap_sum,
                    "gap": demand - cap_sum,
                }
    raise RuntimeError("instance is feasible; no capacity-gap proof exists")


def assign(manuscripts: list[Manuscript], reviewers: list[Reviewer]) -> dict:
    """Solve one assignment request and return the response payload."""
    pairs = allowed_pairs(manuscripts, reviewers)
    demand = sum(m.required for m in manuscripts)

    full_caps = {r.id: r.capacity for r in reviewers}
    if _max_flow_value(manuscripts, reviewers, pairs, full_caps) < demand:
        return {
            "status": "infeasible",
            "proof": find_capacity_gap_proof(manuscripts, reviewers, pairs),
        }

    ratio = _minimal_max_load_ratio(manuscripts, reviewers, pairs, demand)
    assignments, loads = _solve_optimal(manuscripts, reviewers, pairs, demand, ratio)

    total_score = sum((pairs[pair] for pair in assignments), Fraction(0))
    max_load_ratio = max(
        (Fraction(loads[r.id], r.capacity) for r in reviewers if r.capacity > 0),
        default=Fraction(0),
    )
    return {
        "status": "feasible",
        "assignments": [
            {
                "manuscript_id": manuscript_id,
                "reviewer_id": reviewer_id,
                "score": _as_number(pairs[(manuscript_id, reviewer_id)]),
            }
            for manuscript_id, reviewer_id in assignments
        ],
        "total_score": _as_number(total_score),
        "max_load_ratio": float(max_load_ratio),
        "loads": loads,
    }


def _residual_min_cost_flow(
    manuscripts: list[Manuscript],
    reviewers: list[Reviewer],
    pairs: dict[tuple[str, str], Fraction],
    capacities: dict[str, int],
    weight_of: Callable[[tuple[str, str]], int],
) -> tuple[int, dict]:
    """Min-cost flow saturating the (residual) demand exactly.

    The caller guarantees feasibility (checked via max flow beforehand), so
    ``network_simplex`` always finds a flow satisfying all node demands.
    """
    demand = sum(m.required for m in manuscripts)
    graph = nx.DiGraph()
    graph.add_node(_SOURCE, demand=-demand)
    graph.add_node(_SINK, demand=demand)
    for manuscript in manuscripts:
        graph.add_edge(
            _SOURCE, _m_node(manuscript.id), capacity=manuscript.required, weight=0
        )
    for pair in pairs:
        graph.add_edge(
            _m_node(pair[0]), _r_node(pair[1]), capacity=1, weight=weight_of(pair)
        )
    reviewers_in_pairs = {reviewer_id for _, reviewer_id in pairs}
    for reviewer in reviewers:
        if reviewer.id in reviewers_in_pairs:
            graph.add_edge(
                _r_node(reviewer.id), _SINK, capacity=capacities[reviewer.id], weight=0
            )
    return nx.network_simplex(graph)


def reassign(
    manuscripts: list[Manuscript],
    reviewers: list[Reviewer],
    original: list[tuple[str, str]],
    withdrawn_id: str,
    confirmed: list[tuple[str, str]],
) -> dict:
    """Emergency reassignment after reviewer ``withdrawn_id`` withdraws.

    Every original relation of the withdrawn reviewer is revoked; all
    confirmed relations are kept. Among complete reassignments over the
    remaining reviewers, the optimisation order is: maximise the number of
    retained original relations, minimise the maximum load ratio, maximise
    the total match score, then take the lexicographically smallest sorted
    ``(manuscript_id, reviewer_id)`` list. Returns ``status: infeasible``
    (with a reason, and no partial assignment) when none exists.

    Confirmed relations are fixed up front: they reduce the residual
    manuscript requirements and reviewer capacities, so the residual problem
    is solved on the remaining slots and merged back afterwards. Because the
    confirmed part is constant across all solutions, optimising the residual
    problem optimises the full one.
    """
    original_set = set(original)
    confirmed_set = set(confirmed)

    active = [reviewer for reviewer in reviewers if reviewer.id != withdrawn_id]
    pairs = allowed_pairs(manuscripts, active)

    if any(pair not in pairs for pair in confirmed_set):
        return {
            "status": "infeasible",
            "reason": "已确认关系中存在匹配分为 0 的不可用组合，无法保留",
        }

    confirmed_load = {reviewer.id: 0 for reviewer in active}
    confirmed_count = {manuscript.id: 0 for manuscript in manuscripts}
    for manuscript_id, reviewer_id in confirmed_set:
        confirmed_load[reviewer_id] += 1
        confirmed_count[manuscript_id] += 1

    residual_required = {
        m.id: m.required - confirmed_count[m.id] for m in manuscripts
    }
    residual_manuscripts = [
        Manuscript(m.id, m.topics, residual_required[m.id])
        for m in manuscripts
        if residual_required[m.id] > 0
    ]
    residual_pairs = {
        pair: score
        for pair, score in pairs.items()
        if pair not in confirmed_set and residual_required[pair[0]] > 0
    }
    # Original relations that may still be kept: not the withdrawer's (no
    # longer usable) and not already confirmed (fixed up front).
    retained_candidates = set(residual_pairs) & original_set
    residual_demand = sum(residual_required.values())

    residual_assignment: list[tuple[str, str]] = []
    residual_loads = {reviewer.id: 0 for reviewer in active}

    if residual_demand > 0:
        if not residual_pairs:
            return {
                "status": "infeasible",
                "reason": "退出者撤销后，剩余评审人无法满足全部稿件需求",
            }

        def retention_weight(pair: tuple[str, str]) -> int:
            return -1 if pair in retained_candidates else 0

        full_caps = {r.id: r.capacity - confirmed_load[r.id] for r in active}
        if (
            _max_flow_value(residual_manuscripts, active, residual_pairs, full_caps)
            < residual_demand
        ):
            return {
                "status": "infeasible",
                "reason": "退出者撤销后，剩余评审人无法满足全部稿件需求",
            }

        # Objective 1: maximise the number of retained original relations.
        cost, _ = _residual_min_cost_flow(
            residual_manuscripts, active, residual_pairs, full_caps, retention_weight
        )
        max_retention = -cost

        # Objective 2: minimise the maximum load ratio (over total loads,
        # confirmed included) subject to keeping the maximal retention.
        candidates: set[Fraction] = {Fraction(0)}
        for reviewer in active:
            if reviewer.capacity <= 0:
                continue
            # No reviewer can take more manuscripts than exist.
            for load in range(1, min(reviewer.capacity, len(manuscripts)) + 1):
                candidates.add(Fraction(load, reviewer.capacity))
        ordered = sorted(candidates)

        def residual_caps(ratio: Fraction) -> dict[str, int] | None:
            caps = {}
            for reviewer in active:
                cap = _floor(ratio * reviewer.capacity) - confirmed_load[reviewer.id]
                if cap < 0:
                    return None
                caps[reviewer.id] = cap
            return caps

        def feasible(ratio: Fraction) -> bool:
            caps = residual_caps(ratio)
            if caps is None:
                return False
            if (
                _max_flow_value(residual_manuscripts, active, residual_pairs, caps)
                < residual_demand
            ):
                return False
            cost, _ = _residual_min_cost_flow(
                residual_manuscripts, active, residual_pairs, caps, retention_weight
            )
            return -cost >= max_retention

        # ordered[-1] is feasible: it allows every reviewer at least
        # min(capacity, #manuscripts) total assignments, which full capacity
        # never exceeds, and full capacity admits retention max_retention.
        lo, hi = 0, len(ordered) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if feasible(ordered[mid]):
                hi = mid
            else:
                lo = mid + 1
        ratio = ordered[lo]

        # Objectives 3+4: maximise the total score, then take the
        # lexicographically smallest pair list. One min-cost flow with
        # weights -(retention * R) - (score * 2^M) - 2^(M-1-rank): R
        # dominates any sum of score and lexicographic weights, and the
        # score term dominates any sum of lexicographic weights.
        denom = 1
        for score in residual_pairs.values():
            denom = lcm(denom, score.denominator)
        int_score = {
            pair: score.numerator * (denom // score.denominator)
            for pair, score in residual_pairs.items()
        }
        ordered_pairs = sorted(residual_pairs)
        n_pairs = len(ordered_pairs)
        score_weight = 1 << n_pairs
        retention_unit = (sum(int_score.values()) + 1) * score_weight
        rank = {pair: position for position, pair in enumerate(ordered_pairs)}

        def combined_weight(pair: tuple[str, str]) -> int:
            weight = -(int_score[pair] * score_weight) - (1 << (n_pairs - 1 - rank[pair]))
            if pair in retained_candidates:
                weight -= retention_unit
            return weight

        caps = residual_caps(ratio)
        assert caps is not None  # ratio is feasible by construction
        _, flow = _residual_min_cost_flow(
            residual_manuscripts, active, residual_pairs, caps, combined_weight
        )
        residual_assignment = [
            pair
            for pair in ordered_pairs
            if flow.get(_m_node(pair[0]), {}).get(_r_node(pair[1]), 0)
        ]
        residual_loads = {
            reviewer.id: flow.get(_r_node(reviewer.id), {}).get(_SINK, 0)
            for reviewer in active
        }

    new_pairs = sorted(confirmed_set | set(residual_assignment))
    new_set = set(new_pairs)
    loads = {reviewer.id: 0 for reviewer in reviewers}
    for reviewer in active:
        loads[reviewer.id] = confirmed_load[reviewer.id] + residual_loads[reviewer.id]
    total_score = sum((pairs[pair] for pair in new_set), Fraction(0))
    max_load_ratio = max(
        (
            Fraction(loads[reviewer.id], reviewer.capacity)
            for reviewer in reviewers
            if reviewer.capacity > 0
        ),
        default=Fraction(0),
    )
    return {
        "status": "feasible",
        "assignments": [
            {
                "manuscript_id": manuscript_id,
                "reviewer_id": reviewer_id,
                "score": _as_number(pairs[(manuscript_id, reviewer_id)]),
            }
            for manuscript_id, reviewer_id in new_pairs
        ],
        "revoked": [
            {"manuscript_id": manuscript_id, "reviewer_id": reviewer_id}
            for manuscript_id, reviewer_id in sorted(original_set - new_set)
        ],
        "added": [
            {"manuscript_id": manuscript_id, "reviewer_id": reviewer_id}
            for manuscript_id, reviewer_id in sorted(new_set - original_set)
        ],
        "total_score": _as_number(total_score),
        "max_load_ratio": float(max_load_ratio),
        "loads": loads,
    }