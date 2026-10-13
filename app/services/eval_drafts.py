"""把人工保留的真实反馈整理成版本化评测草稿。

草稿和正式评测集之间有一道明确边界：这个服务只写 ``data/evals/drafts``，永远不会
修改 ``fund_agent_v2.json``。用户可以检查 JSON diff，再自行决定是否合并和升版本。
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from app.schemas import (
    AgentEvalDraftInfo,
    AgentEvalDraftListResponse,
    AgentEvalDraftUpsertRequest,
)
from app.services.agent_feedback import (
    AgentFeedbackNotFoundError,
    AgentFeedbackService,
    agent_feedback_service,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DRAFT_ROOT = PROJECT_ROOT / "data" / "evals" / "drafts"
DEFAULT_TARGET_SUITE = PROJECT_ROOT / "data" / "evals" / "fund_agent_v2.json"


class AgentEvalDraftError(RuntimeError):
    """草稿目录、JSON 或目标评测集不符合预期。"""


class AgentEvalDraftValidationError(ValueError):
    """人工填写的场景字段还不能组成可用评测草稿。"""


class AgentEvalDraftService:
    """管理一条反馈对应的一份本地 JSON 草稿。"""

    def __init__(
        self,
        *,
        feedbacks: AgentFeedbackService = agent_feedback_service,
        draft_root: Path = DEFAULT_DRAFT_ROOT,
        target_suite_path: Path = DEFAULT_TARGET_SUITE,
    ) -> None:
        self.feedbacks = feedbacks
        self.draft_root = Path(draft_root)
        self.target_suite_path = Path(target_suite_path)
        self.draft_root.mkdir(parents=True, exist_ok=True)

    def list(self, *, user_id: str) -> AgentEvalDraftListResponse:
        """只返回属于当前用户的草稿，并按最近修改时间倒序排列。"""

        drafts: list[AgentEvalDraftInfo] = []
        for path in sorted(self.draft_root.glob("*.json")):
            payload = self._read_json(path)
            if str(payload.get("source", {}).get("user_id") or "") != user_id:
                continue
            drafts.append(self._to_schema(path, payload))
        drafts.sort(key=lambda item: item.updated_at, reverse=True)
        return AgentEvalDraftListResponse(user_id=user_id, drafts=drafts)

    def upsert(
        self,
        *,
        feedback_id: str,
        request: AgentEvalDraftUpsertRequest,
    ) -> AgentEvalDraftInfo:
        """创建或覆盖一个草稿，但不触碰正式评测文件。"""

        try:
            feedback = self.feedbacks.get_candidate(
                user_id=request.user_id,
                feedback_id=feedback_id,
            )
        except AgentFeedbackNotFoundError:
            raise
        if feedback.review_status != "approved":
            raise AgentEvalDraftValidationError(
                "只有已执行“保留候选”的负向反馈才能生成评测草稿。"
            )

        tools = self._clean_list(request.expected_tools)
        terms = self._clean_list(request.required_answer_terms)
        if request.expected_route == "direct" and tools:
            raise AgentEvalDraftValidationError("direct 路由不应配置工具。")
        if request.expected_route != "direct" and not tools:
            raise AgentEvalDraftValidationError("非 direct 路由至少选择一个预期工具。")
        if not terms:
            raise AgentEvalDraftValidationError("至少填写一个回答关键事实。")

        # case_id 最终会进入一个全局评测集，所以正式场景和其他草稿都不能重名。
        self._ensure_unique_case_id(
            case_id=request.case_id,
            source_feedback_id=feedback_id,
        )

        path = self._path_for_feedback(feedback_id)
        existing = self._read_json(path) if path.exists() else None
        now = datetime.now(timezone.utc).isoformat()
        created_at = str(existing.get("created_at")) if existing else now
        target_suite = self._target_suite()

        scenario = {
            "id": request.case_id,
            "category": request.category,
            "tags": ["feedback", "manual-review"],
            "name": request.name.strip(),
            "description": request.description.strip(),
            "fund_code": request.fund_code.strip(),
            # 问题来自真实对话快照，不需要用户在表单里重新复制一遍。
            "message": feedback.question,
            "expected_route": request.expected_route,
            "required_tools": tools,
            # 草稿阶段不替用户猜错误工具；合并正式集前可结合业务再补充。
            "forbidden_tools": [],
            "max_tool_calls": len(tools),
            # 第一版人工编辑器先生成稳定的回答事实断言。精确 Tool JSON 断言需要在
            # 看过固定 fixture 后填写，因此保留为空并在 review 元数据中明确提醒。
            "tool_result_assertions": [],
            "required_answer_groups": [
                {
                    "name": "覆盖人工标注关键事实",
                    "terms": terms,
                    "match": request.answer_match,
                }
            ],
            "require_evidence_reference": request.require_evidence_reference,
            "reject_absolute_claims": True,
        }
        payload = {
            "draft_version": "1.0",
            "status": "draft",
            "target_suite": {
                "id": str(target_suite.get("id") or "fund-agent-v2"),
                "version": str(target_suite.get("version") or ""),
                "path": self._relative_path(self.target_suite_path),
            },
            "source": {
                "feedback_id": feedback.id,
                "user_id": feedback.user_id,
                "conversation_id": feedback.conversation_id,
                "assistant_message_id": feedback.assistant_message_id,
                "run_id": feedback.run_id,
                "reason_code": feedback.reason_code,
                "comment": feedback.comment,
                "correction": feedback.correction,
                "original_answer": feedback.answer,
            },
            "scenario": scenario,
            "review": {
                "status": "needs_manual_merge",
                "needs_tool_result_assertions": True,
                "notes": (
                    "人工确认固定 fixture 与 Tool JSON 字段后，再复制 scenario 到正式套件并升版本。"
                ),
            },
            "created_at": created_at,
            "updated_at": now,
        }
        self._write_json(path, payload)
        return self._to_schema(path, payload)

    def _ensure_unique_case_id(
        self,
        *,
        case_id: str,
        source_feedback_id: str,
    ) -> None:
        target_ids = {
            str(item.get("id") or "")
            for item in self._target_suite().get("scenarios") or []
        }
        if case_id in target_ids:
            raise AgentEvalDraftValidationError(
                f"case_id 已存在于正式评测集：{case_id}"
            )

        for path in self.draft_root.glob("*.json"):
            payload = self._read_json(path)
            source_id = str(payload.get("source", {}).get("feedback_id") or "")
            other_case_id = str(payload.get("scenario", {}).get("id") or "")
            if source_id != source_feedback_id and other_case_id == case_id:
                raise AgentEvalDraftValidationError(
                    f"case_id 已被另一份草稿使用：{case_id}"
                )

    def _target_suite(self) -> dict[str, Any]:
        if not self.target_suite_path.is_file():
            raise AgentEvalDraftError(
                f"目标评测集不存在：{self._relative_path(self.target_suite_path)}"
            )
        return self._read_json(self.target_suite_path)

    def _path_for_feedback(self, feedback_id: str) -> Path:
        # feedback_id 来自数据库查询结果，不把用户提供的路径片段直接拼进文件系统。
        safe_name = "".join(
            character
            for character in feedback_id
            if character.isascii() and (character.isalnum() or character in "_-")
        )
        if safe_name != feedback_id or not safe_name:
            raise AgentEvalDraftValidationError("feedback_id 格式不合法。")
        return self.draft_root / f"{safe_name}.json"

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentEvalDraftError(
                f"无法读取评测 JSON：{self._relative_path(path)}"
            ) from exc
        if not isinstance(payload, dict):
            raise AgentEvalDraftError(
                f"评测 JSON 顶层必须是对象：{self._relative_path(path)}"
            )
        return payload

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        """先写临时文件再替换，避免进程中断留下半份 JSON。"""

        temporary_path = path.with_suffix(".json.tmp")
        try:
            temporary_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary_path.replace(path)
        except OSError as exc:
            raise AgentEvalDraftError(
                f"无法保存评测草稿：{self._relative_path(path)}"
            ) from exc

    def _to_schema(
        self,
        path: Path,
        payload: dict[str, Any],
    ) -> AgentEvalDraftInfo:
        source = dict(payload.get("source") or {})
        scenario = dict(payload.get("scenario") or {})
        target_suite = dict(payload.get("target_suite") or {})
        answer_groups = list(scenario.get("required_answer_groups") or [])
        primary_group = dict(answer_groups[0]) if answer_groups else {}
        return AgentEvalDraftInfo(
            source_feedback_id=str(source.get("feedback_id") or ""),
            user_id=str(source.get("user_id") or ""),
            case_id=str(scenario.get("id") or ""),
            name=str(scenario.get("name") or ""),
            description=str(scenario.get("description") or ""),
            category=str(scenario.get("category") or "general"),
            fund_code=str(scenario.get("fund_code") or ""),
            expected_route=str(scenario.get("expected_route") or "direct"),
            expected_tools=self._clean_list(scenario.get("required_tools") or []),
            required_answer_terms=self._clean_list(primary_group.get("terms") or []),
            answer_match=(
                "any" if str(primary_group.get("match") or "all") == "any" else "all"
            ),
            require_evidence_reference=bool(
                scenario.get("require_evidence_reference")
            ),
            question=str(scenario.get("message") or ""),
            original_answer=str(source.get("original_answer") or ""),
            correction=str(source.get("correction") or ""),
            file_path=self._relative_path(path),
            target_suite=str(target_suite.get("id") or "fund-agent-v2"),
            scenario=scenario,
            created_at=str(payload.get("created_at") or ""),
            updated_at=str(payload.get("updated_at") or ""),
        )

    @staticmethod
    def _clean_list(values: list[Any]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            item = str(value or "").strip()
            if item and item not in cleaned:
                cleaned.append(item)
        return cleaned

    @staticmethod
    def _relative_path(path: Path) -> str:
        try:
            return str(path.resolve().relative_to(PROJECT_ROOT))
        except ValueError:
            return str(path.resolve())


agent_eval_draft_service = AgentEvalDraftService()
