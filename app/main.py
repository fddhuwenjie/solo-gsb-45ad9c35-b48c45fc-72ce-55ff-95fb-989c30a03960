"""FastAPI application exposing the peer-review assignment solver."""
from __future__ import annotations

from fractions import Fraction

from fastapi import FastAPI, Response, status
from fastapi.responses import JSONResponse

from . import batches
from .batches import (
    AssignmentInfeasible,
    BatchAlreadyExists,
    BatchNotFound,
    InvalidBatchRequest,
    VersionConflict,
)
from .models import (
    AssignRequest,
    AssignResponse,
    BatchConfirmationRequest,
    BatchConfirmationResponse,
    BatchCreateRequest,
    BatchReassignmentRequest,
    BatchReassignmentResponse,
    BatchStateResponse,
    FeasibleResponse,
    HealthResponse,
    InfeasibleResponse,
    ReassignFeasibleResponse,
    ReassignInfeasibleResponse,
    ReassignRequest,
    ReassignResponse,
)
from .solver import Manuscript, Reviewer, assign, reassign

app = FastAPI(
    title="Peer Review Assignment API",
    version="1.2.0",
    description=(
        "同行评审分派服务。匹配分为评审人对稿件各主题评分之和；冲突或总分为 0 的组合不可用，"
        "同一稿件不会重复使用同一评审人。完整分派依次优化：最小化评审人最大负载率 → "
        "最大化总匹配分 → 按 (稿件 ID, 评审人 ID) 取字典序最小。无解时返回最小容量缺口证明。"
        "POST /reassign 在评审人退出后做应急改派：撤销退出者的全部关系、保留已确认关系，"
        "依次优化：保留原关系数最大 → 最小化最大负载率 → 最大化总匹配分 → 字典序最小；"
        "无解时明确返回无解。"
        "POST /batches 把分派保存为可查询、带版本的评审批次（本地 SQLite 持久化）："
        "可在当前版本登记确认；评审人退出时以批次内原关系与确认记录调用同一改派求解，"
        "发布为下一版本。确认与发布都需携带读取时的版本，旧版本返回 409 冲突。"
    ),
)


def _to_domain(payload: AssignRequest):
    manuscripts = [
        Manuscript(
            id=manuscript.id,
            topics=tuple(manuscript.topics),
            required=manuscript.required_reviewers,
        )
        for manuscript in payload.manuscripts
    ]
    reviewers = [
        Reviewer(
            id=reviewer.id,
            scores={topic: Fraction(score) for topic, score in reviewer.scores.items()},
            capacity=reviewer.capacity,
            conflicts=frozenset(reviewer.conflicts),
        )
        for reviewer in payload.reviewers
    ]
    return manuscripts, reviewers


def _error(message: str, **extra) -> dict:
    return {"detail": {"message": message, **extra}}


@app.exception_handler(BatchNotFound)
def _handle_not_found(_request, exc: BatchNotFound):
    return JSONResponse(status_code=404, content=_error(str(exc)))


@app.exception_handler(BatchAlreadyExists)
def _handle_already_exists(_request, exc: BatchAlreadyExists):
    return JSONResponse(status_code=409, content=_error(str(exc)))


@app.exception_handler(AssignmentInfeasible)
def _handle_infeasible_create(_request, exc: AssignmentInfeasible):
    # The /assign-compatible proof body is preserved under "result"; nothing
    # is stored, so the client may fix the input and retry the same batch id.
    return JSONResponse(
        status_code=422,
        content=_error(str(exc), result=exc.result),
    )


@app.exception_handler(InvalidBatchRequest)
def _handle_invalid(_request, exc: InvalidBatchRequest):
    return JSONResponse(status_code=422, content=_error(str(exc)))


@app.exception_handler(VersionConflict)
def _handle_version_conflict(_request, exc: VersionConflict):
    return JSONResponse(
        status_code=409,
        content={
            "detail": {
                "code": "version_conflict",
                "message": str(exc),
                "current_version": exc.current_version,
            }
        },
    )


@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["meta"],
    summary="健康检查",
)
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/assign",
    response_model=AssignResponse,
    responses={
        200: {
            "description": "求解完成：可行时返回最优分派，否则返回最小容量缺口证明。",
            "model": AssignResponse,
        },
        422: {"description": "请求体不符合约束（数量范围、ID 唯一性、评分非负等）。"},
    },
    tags=["solver"],
    summary="求解同行评审分派",
)
def create_assignment(payload: AssignRequest) -> dict:
    manuscripts, reviewers = _to_domain(payload)
    return assign(manuscripts, reviewers)


@app.post(
    "/reassign",
    response_model=ReassignResponse,
    responses={
        200: {
            "description": "求解完成：可行时返回完整改派及撤销/新增关系，否则明确返回无解。",
            "model": ReassignResponse,
        },
        422: {
            "description": (
                "请求体不合法（原分派不满足稿件需求/冲突/容量约束、"
                "已确认关系不属于原分派或涉及退出者等）。"
            )
        },
    },
    tags=["solver"],
    summary="评审人退出后的应急改派",
)
def create_reassignment(payload: ReassignRequest) -> dict:
    manuscripts, reviewers = _to_domain(payload)
    return reassign(
        manuscripts,
        reviewers,
        original=[
            (relation.manuscript_id, relation.reviewer_id)
            for relation in payload.original_assignments
        ],
        withdrawn_id=payload.withdrawn_reviewer_id,
        confirmed=[
            (relation.manuscript_id, relation.reviewer_id)
            for relation in payload.confirmed
        ],
    )


# ---------------------------------------------------------------------------
# Persisted, versioned review batches
# ---------------------------------------------------------------------------


@app.post(
    "/batches",
    response_model=BatchStateResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "批次已创建：先经 /assign 求完整分派，可行后才落库。"},
        409: {"description": "批次 ID 已存在，拒绝覆盖。"},
        422: {
            "description": "请求体不合法，或输入不存在完整分派（result 中为容量缺口证明）。"
        },
    },
    tags=["batches"],
    summary="创建评审批次（复用 /assign 求完整分派后保存）",
)
def create_batch(payload: BatchCreateRequest) -> dict:
    return batches.create_batch(payload)


@app.get(
    "/batches/{batch_id}",
    response_model=BatchStateResponse,
    responses={404: {"description": "批次不存在。"}},
    tags=["batches"],
    summary="查询批次：返回当前输入、分派、确认与版本",
)
def get_batch(batch_id: str) -> dict:
    return batches.get_state(batch_id)


@app.post(
    "/batches/{batch_id}/confirmations",
    response_model=BatchConfirmationResponse,
    responses={
        200: {"description": "确认已在新版本登记，返回批次最新状态。"},
        404: {"description": "批次不存在。"},
        409: {"description": "携带的版本已过期（同一版本仅一个并发请求生效）。"},
        422: {"description": "确认关系重复，或不属于当前版本的分派，或已登记过。"},
    },
    tags=["batches"],
    summary="在当前版本登记确认（乐观并发，版本递增）",
)
def confirm_relation(batch_id: str, payload: BatchConfirmationRequest) -> dict:
    return batches.register_confirmation(batch_id, payload)


@app.post(
    "/batches/{batch_id}/reassignments",
    response_model=BatchReassignmentResponse,
    responses={
        200: {
            "description": (
                "求解完成：可行时完整改派连同撤销/新增关系发布为下一版本；"
                "无解时批次、确认与版本均不变。"
            ),
            "model": BatchReassignmentResponse,
        },
        404: {"description": "批次不存在。"},
        409: {"description": "携带的版本已过期（同一版本仅一个并发请求生效）。"},
        422: {"description": "退出者不在批次评审人列表中等非法请求。"},
    },
    tags=["batches"],
    summary="评审人退出：以批次原关系与确认记录改派并发布下一版本",
)
def publish_reassignment(
    batch_id: str, payload: BatchReassignmentRequest, response: Response
) -> dict:
    result = batches.publish_reassignment(batch_id, payload)
    if result["status"] == "feasible":
        response.status_code = status.HTTP_201_CREATED
    return result


__all__ = [
    "app",
    "FeasibleResponse",
    "InfeasibleResponse",
    "ReassignFeasibleResponse",
    "ReassignInfeasibleResponse",
]
