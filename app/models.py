"""Request/response schemas for the peer-review assignment API."""
from __future__ import annotations

import math
from collections import Counter
from typing import Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ManuscriptIn(BaseModel):
    id: str = Field(min_length=1, description="稿件 ID")
    topics: list[str] = Field(min_length=1, description="稿件主题列表（至少一个，且不重复）")
    required_reviewers: int = Field(ge=1, description="所需评审人数")

    @field_validator("topics")
    @classmethod
    def _topics_ok(cls, value: list[str]) -> list[str]:
        if any(not topic.strip() for topic in value):
            raise ValueError("topics 不能包含空白字符串")
        if len(set(value)) != len(value):
            raise ValueError("topics 不能重复")
        return value


class ReviewerIn(BaseModel):
    id: str = Field(min_length=1, description="评审人 ID")
    scores: dict[str, float] = Field(
        default_factory=dict, description="各主题评分（有限非负数，缺省主题按 0 计）"
    )
    capacity: int = Field(ge=0, description="最多可评审的稿件数")
    conflicts: list[str] = Field(
        default_factory=list, description="存在利益冲突、不可指派的稿件 ID 列表"
    )

    @field_validator("scores")
    @classmethod
    def _scores_ok(cls, value: dict[str, float]) -> dict[str, float]:
        for topic, score in value.items():
            if not topic.strip():
                raise ValueError("评分的主题名不能为空白字符串")
            if not math.isfinite(score) or score < 0:
                raise ValueError("评分必须为有限非负数")
        return value

    @field_validator("conflicts")
    @classmethod
    def _conflicts_ok(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("conflicts 不能重复")
        return value


class AssignRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "manuscripts": [
                        {"id": "m1", "topics": ["nlp", "ml"], "required_reviewers": 2},
                        {"id": "m2", "topics": ["vision"], "required_reviewers": 1},
                    ],
                    "reviewers": [
                        {
                            "id": "r1",
                            "scores": {"nlp": 5, "ml": 4},
                            "capacity": 2,
                            "conflicts": ["m2"],
                        },
                        {
                            "id": "r2",
                            "scores": {"nlp": 3, "vision": 5},
                            "capacity": 1,
                            "conflicts": [],
                        },
                        {
                            "id": "r3",
                            "scores": {"ml": 4, "vision": 2},
                            "capacity": 1,
                            "conflicts": [],
                        },
                    ],
                }
            ]
        }
    )

    manuscripts: list[ManuscriptIn] = Field(
        min_length=1, max_length=10, description="稿件列表（1～10 篇）"
    )
    reviewers: list[ReviewerIn] = Field(
        min_length=1, max_length=20, description="评审人列表（1～20 名）"
    )

    @model_validator(mode="after")
    def _cross_check(self) -> "AssignRequest":
        manuscript_ids = [m.id for m in self.manuscripts]
        if len(set(manuscript_ids)) != len(manuscript_ids):
            raise ValueError("稿件 ID 必须唯一")
        reviewer_ids = [r.id for r in self.reviewers]
        if len(set(reviewer_ids)) != len(reviewer_ids):
            raise ValueError("评审人 ID 必须唯一")
        known = set(manuscript_ids)
        for reviewer in self.reviewers:
            unknown = sorted(set(reviewer.conflicts) - known)
            if unknown:
                raise ValueError(
                    f"评审人 {reviewer.id} 的 conflicts 包含未知稿件: {unknown}"
                )
        return self


class AssignmentRelation(BaseModel):
    manuscript_id: str = Field(min_length=1, description="稿件 ID")
    reviewer_id: str = Field(min_length=1, description="评审人 ID")


class ReassignRequest(AssignRequest):
    """应急改派请求：稿件/评审人字段与 /assign 相同，另需原分派、退出者与已确认关系。"""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "manuscripts": [
                        {"id": "m1", "topics": ["nlp", "ml"], "required_reviewers": 2},
                        {"id": "m2", "topics": ["vision"], "required_reviewers": 1},
                    ],
                    "reviewers": [
                        {
                            "id": "r1",
                            "scores": {"nlp": 5, "ml": 4},
                            "capacity": 2,
                            "conflicts": ["m2"],
                        },
                        {
                            "id": "r2",
                            "scores": {"nlp": 3, "vision": 5},
                            "capacity": 1,
                            "conflicts": [],
                        },
                        {
                            "id": "r3",
                            "scores": {"ml": 4, "vision": 2},
                            "capacity": 2,
                            "conflicts": [],
                        },
                    ],
                    "original_assignments": [
                        {"manuscript_id": "m1", "reviewer_id": "r1"},
                        {"manuscript_id": "m1", "reviewer_id": "r3"},
                        {"manuscript_id": "m2", "reviewer_id": "r2"},
                    ],
                    "withdrawn_reviewer_id": "r2",
                    "confirmed": [
                        {"manuscript_id": "m1", "reviewer_id": "r1"},
                    ],
                }
            ]
        }
    )

    original_assignments: list[AssignmentRelation] = Field(
        description="原完整分派的 (稿件, 评审人) 关系列表"
    )
    withdrawn_reviewer_id: str = Field(min_length=1, description="退出评审人的 ID")
    confirmed: list[AssignmentRelation] = Field(
        default_factory=list,
        description="原分派中已确认、必须保留的关系（不得涉及退出者）",
    )

    @model_validator(mode="after")
    def _reassign_check(self) -> "ReassignRequest":
        reviewer_ids = {reviewer.id for reviewer in self.reviewers}
        if self.withdrawn_reviewer_id not in reviewer_ids:
            raise ValueError(f"退出者 {self.withdrawn_reviewer_id} 不在评审人列表中")

        required = {m.id: m.required_reviewers for m in self.manuscripts}
        capacity = {r.id: r.capacity for r in self.reviewers}
        conflicts = {r.id: set(r.conflicts) for r in self.reviewers}
        manuscript_ids = set(required)

        original: set[tuple[str, str]] = set()
        for relation in self.original_assignments:
            pair = (relation.manuscript_id, relation.reviewer_id)
            if relation.manuscript_id not in manuscript_ids:
                raise ValueError(f"原分派包含未知稿件: {relation.manuscript_id}")
            if relation.reviewer_id not in reviewer_ids:
                raise ValueError(f"原分派包含未知评审人: {relation.reviewer_id}")
            if pair in original:
                raise ValueError(f"原分派包含重复关系: {pair}")
            original.add(pair)
            if relation.manuscript_id in conflicts[relation.reviewer_id]:
                raise ValueError(
                    f"原分派包含冲突关系: 评审人 {relation.reviewer_id} "
                    f"与稿件 {relation.manuscript_id} 冲突"
                )

        per_manuscript = Counter(m_id for m_id, _ in original)
        for manuscript_id, need in required.items():
            got = per_manuscript.get(manuscript_id, 0)
            if got != need:
                raise ValueError(
                    f"原分派不满足稿件需求: 稿件 {manuscript_id} "
                    f"需要 {need} 名评审人，实际 {got} 名"
                )
        per_reviewer = Counter(r_id for _, r_id in original)
        for reviewer_id, load in per_reviewer.items():
            if load > capacity[reviewer_id]:
                raise ValueError(
                    f"原分派超出评审人容量: 评审人 {reviewer_id} "
                    f"容量 {capacity[reviewer_id]}，实际 {load} 篇"
                )

        confirmed: set[tuple[str, str]] = set()
        for relation in self.confirmed:
            pair = (relation.manuscript_id, relation.reviewer_id)
            if pair in confirmed:
                raise ValueError(f"已确认关系重复: {pair}")
            confirmed.add(pair)
            if pair not in original:
                raise ValueError(f"已确认关系 {pair} 不属于原分派")
            if relation.reviewer_id == self.withdrawn_reviewer_id:
                raise ValueError(
                    f"已确认关系涉及退出者 {self.withdrawn_reviewer_id}，无法保留"
                )
        return self


class AssignmentOut(BaseModel):
    manuscript_id: str = Field(description="稿件 ID")
    reviewer_id: str = Field(description="评审人 ID")
    score: float = Field(description="该 (稿件, 评审人) 组合的匹配分")


class FeasibleResponse(BaseModel):
    status: Literal["feasible"] = "feasible"
    assignments: list[AssignmentOut] = Field(
        description="分派结果，按 (稿件 ID, 评审人 ID) 字典序排列"
    )
    total_score: float = Field(description="总匹配分")
    max_load_ratio: float = Field(description="评审人最大负载率（已最小化）")
    loads: dict[str, int] = Field(description="每名评审人被指派的稿件数")


class CapacityGapProof(BaseModel):
    manuscripts: list[str] = Field(description="需求无法被满足的稿件子集（按 ID 排序）")
    reviewers: list[str] = Field(description="该子集的全部可用评审人（按 ID 排序）")
    demand: int = Field(description="子集的评审需求总数")
    capacity: int = Field(
        description="可用评审人对着子集的有效容量和：每人取 min(容量, 子集中其可评审稿件数) 后求和"
    )
    gap: int = Field(description="容量缺口，即 demand - capacity")


class InfeasibleResponse(BaseModel):
    status: Literal["infeasible"] = "infeasible"
    proof: CapacityGapProof = Field(description="最小容量缺口证明")


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReassignFeasibleResponse(BaseModel):
    status: Literal["feasible"] = "feasible"
    assignments: list[AssignmentOut] = Field(
        description="改派后的完整分派，按 (稿件 ID, 评审人 ID) 字典序排列"
    )
    revoked: list[AssignmentRelation] = Field(
        description="相对原分派被撤销的关系（含退出者的全部关系），按 (稿件 ID, 评审人 ID) 排序"
    )
    added: list[AssignmentRelation] = Field(
        description="相对原分派新增的关系，按 (稿件 ID, 评审人 ID) 排序"
    )
    total_score: float = Field(description="总匹配分")
    max_load_ratio: float = Field(description="评审人最大负载率（在最大保留前提下已最小化）")
    loads: dict[str, int] = Field(description="每名评审人被指派的稿件数（退出者为 0）")


class ReassignInfeasibleResponse(BaseModel):
    status: Literal["infeasible"] = "infeasible"
    reason: str = Field(description="无解原因")


# ---------------------------------------------------------------------------
# Review batches (persisted, versioned review rounds)
# ---------------------------------------------------------------------------


class BatchCreateRequest(AssignRequest):
    """创建评审批次：在 /assign 的输入之外提供批次 ID。"""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "batch_id": "batch-2026-10",
                    "manuscripts": [
                        {"id": "m1", "topics": ["nlp", "ml"], "required_reviewers": 2},
                        {"id": "m2", "topics": ["vision"], "required_reviewers": 1},
                    ],
                    "reviewers": [
                        {
                            "id": "r1",
                            "scores": {"nlp": 5, "ml": 4},
                            "capacity": 2,
                            "conflicts": ["m2"],
                        },
                        {
                            "id": "r2",
                            "scores": {"nlp": 3, "vision": 5},
                            "capacity": 1,
                            "conflicts": [],
                        },
                        {
                            "id": "r3",
                            "scores": {"ml": 4, "vision": 2},
                            "capacity": 2,
                            "conflicts": [],
                        },
                    ],
                }
            ]
        }
    )

    batch_id: str = Field(min_length=1, description="批次 ID（唯一，重复 ID 不覆盖）")


class BatchConfirmationRequest(BaseModel):
    version: int = Field(ge=1, description="读取批次时看到的版本（乐观并发）")
    relations: list[AssignmentRelation] = Field(
        min_length=1, description="要在当前版本上登记确认的关系列表（不可重复）"
    )

    @field_validator("relations")
    @classmethod
    def _relations_unique(cls, value: list[AssignmentRelation]) -> list[AssignmentRelation]:
        pairs = [(r.manuscript_id, r.reviewer_id) for r in value]
        if len(set(pairs)) != len(pairs):
            raise ValueError("确认关系不可重复")
        return value


class BatchReassignmentRequest(BaseModel):
    version: int = Field(ge=1, description="读取批次时看到的版本（乐观并发）")
    withdrawn_reviewer_id: str = Field(min_length=1, description="退出评审人的 ID")


class BatchStateResponse(BaseModel):
    """批次当前状态：当前输入、分派、确认与版本。"""

    batch_id: str = Field(description="批次 ID")
    version: int = Field(description="当前版本（初始为 1，每次确认/发布递增）")
    input: AssignRequest = Field(description="创建批次时传入的稿件与评审人输入")
    assignment: FeasibleResponse = Field(description="当前版本的完整分派")
    confirmed: list[AssignmentRelation] = Field(
        description="当前版本已登记确认的关系，按 (稿件 ID, 评审人 ID) 排序"
    )


class BatchConfirmationResponse(BatchStateResponse):
    pass


class BatchReassignmentFeasibleResponse(ReassignFeasibleResponse):
    version: int = Field(description="发布后的新版本号")
    batch_id: str = Field(description="批次 ID")


class BatchReassignmentInfeasibleResponse(ReassignInfeasibleResponse):
    version: int = Field(description="当前版本（无解时版本不变）")
    batch_id: str = Field(description="批次 ID")


AssignResponse = Union[FeasibleResponse, InfeasibleResponse]
ReassignResponse = Union[ReassignFeasibleResponse, ReassignInfeasibleResponse]
BatchReassignmentResponse = Union[
    BatchReassignmentFeasibleResponse, BatchReassignmentInfeasibleResponse
]
