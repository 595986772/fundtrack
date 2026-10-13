"""人工反馈转评测草稿的确定性测试。

这些测试只使用临时 SQLite 和临时 JSON 文件，不调用大模型，也不会改动项目中的
``data/evals/fund_agent_v2.json``。
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.database import Database
from app.schemas import AgentEvalDraftUpsertRequest
from app.services.agent_feedback import AgentFeedbackService
from app.services.conversations import ConversationService
from app.services.eval_drafts import (
    AgentEvalDraftService,
    AgentEvalDraftValidationError,
)


class AgentEvalDraftServiceTests(unittest.TestCase):
    """覆盖审核边界、正式集边界和草稿唯一性。"""

    def test_approved_feedback_creates_user_isolated_draft(self) -> None:
        """已保留反馈应生成兼容场景，同时不能改写正式评测集。"""

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            feedbacks, feedback_id = self._make_feedback(root)
            feedbacks.review(
                user_id="chen",
                feedback_id=feedback_id,
                status="approved",
                review_note="需要加入风险事实回归。",
            )
            suite_path = self._write_suite(root)
            original_suite = suite_path.read_bytes()
            service = AgentEvalDraftService(
                feedbacks=feedbacks,
                draft_root=root / "drafts",
                target_suite_path=suite_path,
            )

            draft = service.upsert(
                feedback_id=feedback_id,
                request=self._request(),
            )

            self.assertEqual(draft.source_feedback_id, feedback_id)
            self.assertEqual(draft.question, "这只基金现在有什么风险？")
            self.assertEqual(draft.scenario["expected_route"], "fund_assessment")
            self.assertEqual(
                draft.scenario["required_tools"],
                ["get_fund_assessment_tool"],
            )
            self.assertEqual(
                draft.scenario["required_answer_groups"][0]["terms"],
                ["最大回撤", "不确定性"],
            )
            self.assertEqual(len(service.list(user_id="chen").drafts), 1)
            self.assertEqual(service.list(user_id="other-user").drafts, [])
            self.assertEqual(suite_path.read_bytes(), original_suite)

            # 同一条反馈再次保存只更新原草稿，不新增第二个文件。
            updated = service.upsert(
                feedback_id=feedback_id,
                request=self._request(name="风险回答回归（已复核）"),
            )
            self.assertEqual(updated.created_at, draft.created_at)
            self.assertEqual(updated.name, "风险回答回归（已复核）")
            self.assertEqual(len(list((root / "drafts").glob("*.json"))), 1)

    def test_pending_feedback_and_duplicate_case_id_are_rejected(self) -> None:
        """草稿不能绕过人工保留，也不能占用正式评测集已有 ID。"""

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            feedbacks, feedback_id = self._make_feedback(root)
            suite_path = self._write_suite(root)
            service = AgentEvalDraftService(
                feedbacks=feedbacks,
                draft_root=root / "drafts",
                target_suite_path=suite_path,
            )

            with self.assertRaisesRegex(
                AgentEvalDraftValidationError,
                "只有已执行",
            ):
                service.upsert(
                    feedback_id=feedback_id,
                    request=self._request(),
                )

            feedbacks.review(
                user_id="chen",
                feedback_id=feedback_id,
                status="approved",
            )
            with self.assertRaisesRegex(
                AgentEvalDraftValidationError,
                "已存在于正式评测集",
            ):
                service.upsert(
                    feedback_id=feedback_id,
                    request=self._request(case_id="existing-case"),
                )

    @staticmethod
    def _make_feedback(root: Path) -> tuple[AgentFeedbackService, str]:
        """建立一轮真实问答快照，并给 Assistant 回答提交负向反馈。"""

        database = Database(f"sqlite:///{root / 'feedback.db'}")
        conversations = ConversationService(database)
        feedbacks = AgentFeedbackService(database)
        conversation = conversations.create(user_id="chen")
        _, _, assistant = conversations.append_exchange(
            user_id="chen",
            conversation_id=conversation["id"],
            user_content="这只基金现在有什么风险？",
            assistant_content="风险不大。",
            run_id="run-eval-draft-1",
            run_metadata={},
        )
        feedback = feedbacks.submit(
            user_id="chen",
            conversation_id=conversation["id"],
            assistant_message_id=assistant["id"],
            rating="unhelpful",
            reason_code="incomplete",
            comment="缺少可核查的风险依据。",
            correction="必须说明最大回撤和信息不确定性。",
        )
        return feedbacks, feedback.id

    @staticmethod
    def _write_suite(root: Path) -> Path:
        """建立最小正式套件，用于验证冲突检查和只读边界。"""

        path = root / "fund_agent_v2.json"
        path.write_text(
            json.dumps(
                {
                    "id": "fund-agent-v2",
                    "version": "2.0.0",
                    "scenarios": [{"id": "existing-case"}],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _request(
        *,
        case_id: str = "feedback-risk-answer",
        name: str = "风险回答回归",
    ) -> AgentEvalDraftUpsertRequest:
        return AgentEvalDraftUpsertRequest(
            user_id="chen",
            case_id=case_id,
            name=name,
            description="验证风险回答包含关键事实与边界。",
            category="fund_assessment",
            fund_code="110020",
            expected_route="fund_assessment",
            expected_tools=["get_fund_assessment_tool"],
            required_answer_terms=["最大回撤", "不确定性"],
            answer_match="all",
            require_evidence_reference=True,
        )


if __name__ == "__main__":
    unittest.main()
