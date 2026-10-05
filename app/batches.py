"""Persistent, versioned review batches.

A batch stores the original solver input (manuscripts and reviewers), the
complete assignment of its current version, and the relations that have been
confirmed on that version. Creation reuses :func:`app.solver.assign`;
publishing a withdrawal reuses :func:`app.solver.reassign` with the batch's
own original relations and confirmation records.

Optimistic concurrency: every mutation carries the version the caller read.
Confirms and publishes execute inside one serialised ``BEGIN IMMEDIATE``
transaction with the version checked inside it, so at most one concurrent
request per version can succeed; stale callers get :class:`VersionConflict`
together with the current version. When a reassignment is infeasible the
transaction is rolled back and batch, confirmations and version stay
unchanged.
"""
from __future__ import annotations

import json
import sqlite3
from fractions import Fraction

from .models import (
    BatchConfirmationRequest,
    BatchCreateRequest,
    BatchReassignmentRequest,
)
from .solver import Manuscript, Reviewer, assign, reassign
from .storage import Rollback, read_transaction, write_transaction


class BatchError(Exception):
    """Base class for service-layer errors (mapped to HTTP 4xx)."""


class BatchNotFound(BatchError):
    def __init__(self, batch_id: str):
        super().__init__(f"批次不存在: {batch_id}")
        self.batch_id = batch_id


class BatchAlreadyExists(BatchError):
    def __init__(self, batch_id: str):
        super().__init__(f"批次 ID 已存在，拒绝覆盖: {batch_id}")
        self.batch_id = batch_id


class AssignmentInfeasible(BatchError):
    """Creation input admits no complete assignment; nothing is stored."""

    def __init__(self, result: dict):
        super().__init__("输入不存在完整分派，批次未创建")
        self.result = result


class InvalidBatchRequest(BatchError):
    """The mutation is semantically invalid for the batch (HTTP 422)."""


class VersionConflict(BatchError):
    def __init__(self, current_version: int):
        super().__init__(f"版本已过期，当前版本为 {current_version}")
        self.current_version = current_version


def to_domain(input_payload: dict):
    manuscripts = [
        Manuscript(
            id=manuscript["id"],
            topics=tuple(manuscript["topics"]),
            required=manuscript["required_reviewers"],
        )
        for manuscript in input_payload["manuscripts"]
    ]
    reviewers = [
        Reviewer(
            id=reviewer["id"],
            scores={
                topic: Fraction(str(score))
                for topic, score in reviewer.get("scores", {}).items()
            },
            capacity=reviewer["capacity"],
            conflicts=frozenset(reviewer.get("conflicts", [])),
        )
        for reviewer in input_payload["reviewers"]
    ]
    return manuscripts, reviewers


def _fetch(conn: sqlite3.Connection, batch_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, version, input, assignment FROM batches WHERE id = ?",
        (batch_id,),
    ).fetchone()


def _confirmed(conn, batch_id: str) -> list[tuple[str, str]]:
    return [
        (row["manuscript_id"], row["reviewer_id"])
        for row in conn.execute(
            "SELECT manuscript_id, reviewer_id FROM confirmations "
            "WHERE batch_id = ? ORDER BY manuscript_id, reviewer_id",
            (batch_id,),
        ).fetchall()
    ]


def _state(row, confirmed: list[tuple[str, str]]) -> dict:
    return {
        "batch_id": row["id"],
        "version": row["version"],
        "input": json.loads(row["input"]),
        "assignment": json.loads(row["assignment"]),
        "confirmed": [
            {"manuscript_id": manuscript_id, "reviewer_id": reviewer_id}
            for manuscript_id, reviewer_id in confirmed
        ],
    }


def get_state(batch_id: str) -> dict:
    with read_transaction() as conn:
        row = _fetch(conn, batch_id)
        if row is None:
            raise BatchNotFound(batch_id)
        return _state(row, _confirmed(conn, batch_id))


def create_batch(payload: BatchCreateRequest) -> dict:
    """Solve with ``/assign`` semantics first; only persist a feasible result."""
    input_payload = payload.model_dump(exclude={"batch_id"})
    manuscripts, reviewers = to_domain(input_payload)
    result = assign(manuscripts, reviewers)
    if result["status"] != "feasible":
        raise AssignmentInfeasible(result)

    with write_transaction() as conn:
        existing = conn.execute(
            "SELECT 1 FROM batches WHERE id = ?", (payload.batch_id,)
        ).fetchone()
        if existing is not None:
            # Rolls the transaction back; the caller's input never overwrites
            # an existing batch.
            raise BatchAlreadyExists(payload.batch_id)
        conn.execute(
            "INSERT INTO batches (id, version, input, assignment) "
            "VALUES (?, 1, ?, ?)",
            (
                payload.batch_id,
                json.dumps(input_payload, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
            ),
        )
        row = _fetch(conn, payload.batch_id)
        return _state(row, [])


def register_confirmation(batch_id: str, payload: BatchConfirmationRequest) -> dict:
    with write_transaction() as conn:
        row = _fetch(conn, batch_id)
        if row is None:
            raise BatchNotFound(batch_id)
        if row["version"] != payload.version:
            raise VersionConflict(row["version"])

        current = {
            (relation["manuscript_id"], relation["reviewer_id"])
            for relation in json.loads(row["assignment"])["assignments"]
        }
        confirmed = set(_confirmed(conn, batch_id))
        for relation in payload.relations:
            pair = (relation.manuscript_id, relation.reviewer_id)
            if pair not in current:
                raise InvalidBatchRequest(
                    f"确认关系 {pair} 不属于当前版本（v{row['version']}）的分派"
                )
            if pair in confirmed:
                raise InvalidBatchRequest(f"确认关系 {pair} 已在当前版本登记")
            confirmed.add(pair)
            conn.execute(
                "INSERT INTO confirmations (batch_id, manuscript_id, reviewer_id) "
                "VALUES (?, ?, ?)",
                (batch_id, relation.manuscript_id, relation.reviewer_id),
            )

        next_version = row["version"] + 1
        conn.execute(
            "UPDATE batches SET version = ? WHERE id = ?",
            (next_version, batch_id),
        )
        return _state(_fetch(conn, batch_id), sorted(confirmed))


def publish_reassignment(batch_id: str, payload: BatchReassignmentRequest) -> dict:
    """Run the existing reassign solver; commit only a feasible result.

    On infeasibility the transaction rolls back (via :class:`Rollback`) so the
    batch, its confirmations and its version remain exactly as read.
    """
    try:
        with write_transaction() as conn:
            row = _fetch(conn, batch_id)
            if row is None:
                raise BatchNotFound(batch_id)
            if row["version"] != payload.version:
                raise VersionConflict(row["version"])

            input_payload = json.loads(row["input"])
            manuscripts, reviewers = to_domain(input_payload)
            reviewer_ids = {reviewer.id for reviewer in reviewers}
            if payload.withdrawn_reviewer_id not in reviewer_ids:
                raise InvalidBatchRequest(
                    f"退出者 {payload.withdrawn_reviewer_id} 不在批次评审人列表中"
                )

            assignment = json.loads(row["assignment"])
            original = [
                (relation["manuscript_id"], relation["reviewer_id"])
                for relation in assignment["assignments"]
            ]
            # The withdrawer's relations are being revoked; its stale
            # confirmations must not be passed to the solver (which rejects
            # them) — they are dropped along with the revoked relations.
            confirmed = [
                pair
                for pair in _confirmed(conn, batch_id)
                if pair[1] != payload.withdrawn_reviewer_id
            ]

            result = reassign(
                manuscripts,
                reviewers,
                original=original,
                withdrawn_id=payload.withdrawn_reviewer_id,
                confirmed=confirmed,
            )
            if result["status"] != "feasible":
                # Roll back without surfacing an error: the caller gets the
                # solver's infeasible reason; state and version stay untouched.
                raise Rollback(
                    {
                        "status": "infeasible",
                        "reason": result["reason"],
                        "batch_id": batch_id,
                        "version": row["version"],
                    }
                )

            new_pairs = {
                (relation["manuscript_id"], relation["reviewer_id"])
                for relation in result["assignments"]
            }
            # Only confirmations that survived (not the withdrawer's, not
            # revoked relations) carry into the new version.
            surviving = [pair for pair in confirmed if pair in new_pairs]

            conn.execute(
                "DELETE FROM confirmations WHERE batch_id = ?", (batch_id,)
            )
            conn.executemany(
                "INSERT INTO confirmations "
                "(batch_id, manuscript_id, reviewer_id) VALUES (?, ?, ?)",
                [(batch_id, m_id, r_id) for m_id, r_id in surviving],
            )
            conn.execute(
                "UPDATE batches SET assignment = ?, version = ? WHERE id = ?",
                (
                    json.dumps(result, ensure_ascii=False),
                    row["version"] + 1,
                    batch_id,
                ),
            )
            response = dict(result)
            response["batch_id"] = batch_id
            response["version"] = row["version"] + 1
            return response
    except Rollback as rolled_back:
        return rolled_back.value
