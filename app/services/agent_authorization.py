"""Agent 的任务授权：用户有权限，不代表本次任务能访问他的全部组合。

SQL 保存可撤销的任务身份、期限和父子关系，OpenFGA 判断资源与动作的关系。
两边都通过才放行；工具参数、旧 checkpoint 和模型返回值都不能创建授权。
普通用户浏览页面没有 Agent 作用域，仍沿用业务 Service 原有的登录/归属检查。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.services.tracing import traced
from app.database import Database, PROJECT_ROOT, runtime_database
from app.models import AgentAuthorizationAuditModel, AgentTaskGrantModel, FundPortfolioModel
from app.services.agent_execution_context import (
    AgentExecutionContext, PRODUCT_CAPABILITIES, PRODUCT_TOOL_CAPABILITIES,
    PORTFOLIO_READ, PORTFOLIO_RESEARCH_RUN, current_agent_execution_context,
)
from app.services.ai_governance_context import AIControlError


class AgentAuthorizationError(AIControlError):
    """授权拒绝不是模型故障；沿用 AIControlError 的不重试、不降级约定。"""

    def __init__(self, code: str, message: str, *, status_code: int = 403):
        super().__init__(code, message, status_code=status_code, retry_after=0)


def _utc(value: datetime) -> datetime:
    # SQLite 读回时间时会丢失 tzinfo；数据库中的时间一律按 UTC 解释。
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _configured_policy():
    """只从服务端环境/初始化文件读取策略地址，不接收用户传入的 store/model。"""
    from app.services.openfga_authorization import OpenFGAPolicy

    store_id, model_id = settings.openfga_store_id, settings.openfga_model_id
    if not store_id or not model_id:
        path = Path(settings.openfga_config_path)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
            store_id = store_id or config["store_id"]
            model_id = model_id or config["model_id"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise AgentAuthorizationError("authorization_unavailable",
                "任务授权服务尚未初始化，请启动 OpenFGA 并运行 scripts/setup_openfga.py。",
                status_code=503) from exc
    return OpenFGAPolicy(url=settings.openfga_url, store_id=store_id, model_id=model_id,
        token=settings.openfga_token, timeout_seconds=settings.openfga_timeout_seconds)


class TaskAuthorizationService:
    """所有入口共用的判定器。刻意不缓存允许结果，撤销不依赖进程内缓存失效。"""

    def __init__(self, database: Database = runtime_database, *, policy=None, clock=None):
        self.database, self._policy = database, policy
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.database.create_schema()

    @property
    def policy(self):
        return self._policy if self._policy is not None else _configured_policy()

    def create(self, context: AgentExecutionContext, *, run_id: str, agent_id: str,
               portfolio_ids: list[str], capabilities: list[str] | None = None,
               parent_context: AgentExecutionContext | None = None,
               ttl_seconds: int | None = None) -> AgentExecutionContext:
        """建授权单 -> 写关系 -> 激活。任何一步失败都不会留下可用的半成品授权。"""
        if not run_id or len(run_id) > 100 or not agent_id or len(agent_id) > 100:
            raise ValueError("任务和 Agent 标识必须是 1-100 个字符。")
        # 调用者已处在任务内时，默认只能派生子授权，不能借 create 重新开一张全权票。
        inherited = current_agent_execution_context()
        parent = parent_context or (context if context.task_grant_id else None)
        if inherited.task_grant_id:
            if parent and parent.task_grant_id != inherited.task_grant_id:
                raise AgentAuthorizationError("delegation_denied", "不能替换当前任务的父授权。")
            parent = inherited
        ports = sorted(set(portfolio_ids))
        if len(ports) > 20:
            raise ValueError("单次任务最多授权 20 个组合。")
        caps = set(context.capabilities if capabilities is None else capabilities)
        caps &= PRODUCT_CAPABILITIES if capabilities is None else caps
        if not caps.issubset(context.capabilities & PRODUCT_CAPABILITIES):
            raise AgentAuthorizationError("delegation_denied", "任务授权不能增加用户没有的能力。")
        # 没有选择组合不等于授权全部组合；连工具 Schema 也不向模型开放组合能力。
        if not ports:
            caps -= {PORTFOLIO_READ, PORTFOLIO_RESEARCH_RUN}
        now = _utc(self.clock())
        ttl = settings.agent_task_grant_seconds if ttl_seconds is None else ttl_seconds
        if not 1 <= ttl <= 86400:
            raise ValueError("任务授权时长必须在 1 秒至 24 小时之间。")
        expires = now + timedelta(seconds=ttl)
        if parent:
            parent_row = self._checked_row(parent)
            if context.user_id != parent.user_id or not set(ports).issubset(parent_row.portfolio_ids):
                raise AgentAuthorizationError("delegation_denied", "子 Agent 不能访问父任务之外的组合。")
            if not caps.issubset(parent_row.capabilities):
                raise AgentAuthorizationError("delegation_denied", "子 Agent 不能扩大父任务的操作权限。")
            if ttl_seconds is not None and expires > _utc(parent_row.expires_at):
                raise AgentAuthorizationError("delegation_denied", "子授权不能晚于父授权到期。")
            expires = min(expires, _utc(parent_row.expires_at))
        with self.database.session() as session:
            existing = session.scalar(select(AgentTaskGrantModel).where(
                AgentTaskGrantModel.user_id == context.user_id,
                AgentTaskGrantModel.run_id == run_id, AgentTaskGrantModel.agent_id == agent_id))
            if existing:
                # 幂等重试只能恢复原票，不延长期限，也不能把 A 改成 B。
                if (existing.portfolio_ids != ports or set(existing.capabilities) != caps
                        or existing.parent_grant_id != (parent.task_grant_id if parent else None)):
                    raise AgentAuthorizationError("grant_scope_conflict", "相同任务不能更换授权范围，请新建任务。")
                return self.restore(user_id=context.user_id, grant_id=existing.id,
                    run_id=run_id, agent_id=agent_id)
            for portfolio_id in ports:
                if session.get(FundPortfolioModel, (context.user_id, portfolio_id)) is None:
                    raise AgentAuthorizationError("resource_denied", "所选组合不存在或不属于当前用户。")
        policy = self.policy
        if parent and (parent_row.policy_store_id, parent_row.policy_model_id) != (policy.store_id, policy.model_id):
            raise AgentAuthorizationError("policy_changed", "父任务授权策略版本已变化，请重新发起任务。")
        row = AgentTaskGrantModel(id=f"grant_{uuid4().hex}", user_id=context.user_id,
            run_id=run_id, agent_id=agent_id, parent_grant_id=parent.task_grant_id if parent else None,
            source=context.source[:80], status="pending", portfolio_ids=ports,
            capabilities=sorted(caps), policy_store_id=policy.store_id, policy_model_id=policy.model_id,
            created_at=now, expires_at=expires)
        try:
            with self.database.session() as session, session.begin():
                session.add(row)
        except IntegrityError:
            # 并发同键请求只认数据库里已有的票；不创建第二份授权。
            with self.database.session() as session:
                winner = session.scalar(select(AgentTaskGrantModel).where(
                    AgentTaskGrantModel.user_id == context.user_id,
                    AgentTaskGrantModel.run_id == run_id, AgentTaskGrantModel.agent_id == agent_id))
                if (winner is None or winner.portfolio_ids != ports or set(winner.capabilities) != caps
                        or winner.parent_grant_id != (parent.task_grant_id if parent else None)):
                    raise AgentAuthorizationError("grant_scope_conflict", "并发请求的授权范围不一致。")
                return self.restore(user_id=context.user_id, grant_id=winner.id, run_id=run_id, agent_id=agent_id)
        try:
            # 公开行情/计算能力留在任务能力表；OpenFGA 只持有本轮的组合资源关系。
            policy.grant(row.id, ports, sorted(caps & {PORTFOLIO_READ, PORTFOLIO_RESEARCH_RUN}))
            if parent:
                self.check_context(parent)
            with self.database.session() as session, session.begin():
                stored = session.get(AgentTaskGrantModel, row.id)
                # 撤销可以发生在网络写入期间；不能把已撤销的记录重新改为 active。
                if stored.status != "pending" or _utc(stored.expires_at) <= _utc(self.clock()):
                    raise AgentAuthorizationError("grant_inactive", "任务授权在初始化期间已失效。")
                stored.status = "active"
        except Exception as exc:
            with self.database.session() as session, session.begin():
                stored = session.get(AgentTaskGrantModel, row.id)
                if stored.status == "pending":
                    stored.status = "failed"
            if isinstance(exc, AgentAuthorizationError):
                raise
            raise AgentAuthorizationError("authorization_unavailable", "无法建立任务权限，请检查 OpenFGA 服务。",
                status_code=503) from exc
        result = self.restore(user_id=context.user_id, grant_id=row.id, run_id=run_id, agent_id=agent_id)
        self._audit(result, "grant:create", ",".join(ports), True, "任务授权已激活")
        return result

    def restore(self, *, user_id: str, grant_id: str, run_id: str, agent_id: str) -> AgentExecutionContext:
        """只恢复已落库身份；客户端提供一个 grant_id 本身不会获得权限。"""
        context = replace(AgentExecutionContext.product(user_id=user_id, source="task-grant"),
            task_grant_id=grant_id, task_run_id=run_id, task_agent_id=agent_id)
        row = self._checked_row(context)
        return replace(context, capabilities=frozenset(row.capabilities), authenticated=True)

    def _checked_row(self, context: AgentExecutionContext) -> AgentTaskGrantModel:
        """每次重新查 SQL，并逐级检查祖先；父授权撤销无需逐个修改后代记录。"""
        if not context.task_grant_id:
            raise AgentAuthorizationError("grant_required", "本次 Agent 尚未取得任务授权。")
        with self.database.session() as session:
            row = session.get(AgentTaskGrantModel, context.task_grant_id)
            if (row is None or row.user_id != context.user_id or row.run_id != context.task_run_id
                    or row.agent_id != context.task_agent_id):
                raise AgentAuthorizationError("grant_identity_mismatch", "授权与当前用户、任务或 Agent 身份不一致。")
            current, visited = row, set()
            while current is not None:
                if current.id in visited or len(visited) >= 16:
                    raise AgentAuthorizationError("invalid_delegation", "任务授权链无效。")
                visited.add(current.id)
                if current.user_id != context.user_id or current.status != "active":
                    raise AgentAuthorizationError("grant_inactive", "任务或父任务授权已撤销或尚未生效。")
                if _utc(current.expires_at) <= _utc(self.clock()):
                    raise AgentAuthorizationError("grant_expired", "任务授权已到期，请重新发起分析。")
                parent_id = current.parent_grant_id
                if not parent_id:
                    break
                parent = session.get(AgentTaskGrantModel, parent_id)
                if (parent is None or not set(current.portfolio_ids).issubset(parent.portfolio_ids)
                        or not set(current.capabilities).issubset(parent.capabilities)
                        or _utc(current.expires_at) > _utc(parent.expires_at)):
                    raise AgentAuthorizationError("invalid_delegation", "子授权超出父任务范围。")
                current = parent
            return row

    def check_context(self, context: AgentExecutionContext) -> None:
        self._checked_row(context)

    def authorize(self, context: AgentExecutionContext, *, action: str, portfolio_id: str,
                  audit: bool = True) -> None:
        """用户归属、任务动作、资源关系、有效期必须同时通过。"""
        try:
            row = self._checked_row(context)
            if action not in {PORTFOLIO_READ, PORTFOLIO_RESEARCH_RUN} or action not in row.capabilities:
                raise AgentAuthorizationError("action_denied", "本任务未授权该操作。")
            if portfolio_id not in row.portfolio_ids:
                raise AgentAuthorizationError("resource_denied", "该组合不在本次任务的授权范围内，请切换组合后新建任务。")
            with self.database.session() as session:
                if session.get(FundPortfolioModel, (context.user_id, portfolio_id)) is None:
                    raise AgentAuthorizationError("resource_denied", "组合不存在或已不属于当前用户。")
            policy = self.policy
            if (row.policy_store_id, row.policy_model_id) != (policy.store_id, policy.model_id):
                raise AgentAuthorizationError("policy_changed", "授权策略版本已变化，请重新发起任务。")
            # 除了当前 grant，父链的关系被策略管理员删除后也必须影响子授权。
            with self.database.session() as session:
                cursor = row
                while cursor:
                    if (cursor.policy_store_id, cursor.policy_model_id) != (policy.store_id, policy.model_id):
                        raise AgentAuthorizationError("policy_changed", "父任务授权策略版本已变化，请重新发起任务。")
                    if not policy.check(cursor.id, action, portfolio_id):
                        raise AgentAuthorizationError("policy_denied", "OpenFGA 未允许本任务访问该组合。")
                    cursor = session.get(AgentTaskGrantModel, cursor.parent_grant_id) if cursor.parent_grant_id else None
            # 网络检查期间可能发生撤销，返回前再读一次权威生命周期。
            self.check_context(context)
        except AgentAuthorizationError as exc:
            if audit:
                self._audit(context, action, portfolio_id, False, exc.code)
            raise
        except Exception as exc:
            if audit:
                self._audit(context, action, portfolio_id, False, "authorization_unavailable")
            raise AgentAuthorizationError("authorization_unavailable", "无法确认任务权限，已停止访问，请检查授权服务。",
                status_code=503) from exc
        if audit:
            self._audit(context, action, portfolio_id, True, "allowed")

    @traced("agent.authorize_tool", **{"langfuse.observation.type": "guardrail"})
    def authorize_tool(self, context: AgentExecutionContext, tool_name: str, arguments: dict) -> None:
        """只读工具也要检查具体资源；工具能被发现不等于拥有这次调用的权限。"""
        row = self._checked_row(context)
        capability = PRODUCT_TOOL_CAPABILITIES.get(tool_name)
        if capability not in row.capabilities:
            self._audit(context, tool_name, "", False, "tool_not_granted")
            raise AgentAuthorizationError("tool_not_granted", "该工具不在本次任务授权范围内。")
        requested_user = arguments.get("user_id")
        if requested_user and requested_user != context.user_id:
            self._audit(context, tool_name, "", False, "user_mismatch")
            raise AgentAuthorizationError("resource_denied", "不能访问其他用户的资源。")
        portfolio_id = arguments.get("portfolio_id")
        if capability in {PORTFOLIO_READ, PORTFOLIO_RESEARCH_RUN} and not portfolio_id:
            raise AgentAuthorizationError("resource_required", "请先选择本次要分析的组合。")
        if portfolio_id:
            action = PORTFOLIO_RESEARCH_RUN if capability == PORTFOLIO_RESEARCH_RUN else PORTFOLIO_READ
            self.authorize(context, action=action, portfolio_id=str(portfolio_id))
        # 这些编号都要按数据库真实关联解析，不能仅相信模型同时填写的 portfolio_id。
        from app.models import BackgroundJobModel, PortfolioStressRunModel, PortfolioAllocationDraftModel
        references = [("job_id", BackgroundJobModel), ("source_run_id", PortfolioStressRunModel),
            ("draft_id", PortfolioAllocationDraftModel)]
        if tool_name == "get_portfolio_stress_tool":
            references.append(("run_id", PortfolioStressRunModel))
        for key, model in references:
            resource_id = arguments.get(key)
            if not resource_id:
                continue
            with self.database.session() as session:
                related = session.get(model, (context.user_id, resource_id))
                real_portfolio = ((related.payload or {}).get("portfolio_id") if model is BackgroundJobModel
                    else related.portfolio_id) if related else None
            if not real_portfolio or (portfolio_id and portfolio_id != real_portfolio):
                self._audit(context, tool_name, str(resource_id), False, "resource_binding_mismatch")
                raise AgentAuthorizationError("resource_denied", "记录不属于本次选择的组合。")
            self.authorize(context, action=PORTFOLIO_READ, portfolio_id=real_portfolio)
        if not portfolio_id:
            self._audit(context, tool_name, "public", True, "allowed")

    def revoke(self, *, user_id: str, grant_id: str, reason: str = "用户撤销") -> dict:
        """撤销先写 SQL，OpenFGA 无需在线也能阻止后续执行；不删除历史审计证据。"""
        with self.database.session() as session, session.begin():
            row = session.get(AgentTaskGrantModel, grant_id)
            if row is None or row.user_id != user_id:
                raise LookupError("任务授权不存在。")
            row.status = "revoked"
            row.revoked_at = _utc(self.clock())
            row.revoke_reason = str(reason).strip()[:200] or "用户撤销"
            result = self._view(row)
        context = replace(AgentExecutionContext.product(user_id=user_id), task_grant_id=grant_id)
        self._audit(context, "grant:revoke", "", True, "用户撤销，后代授权同步失效")
        return result

    def detail(self, *, user_id: str, grant_id: str) -> dict:
        with self.database.session() as session:
            row = session.get(AgentTaskGrantModel, grant_id)
            if row is None or row.user_id != user_id:
                raise LookupError("任务授权不存在。")
            result = self._view(row)
            events = session.scalars(select(AgentAuthorizationAuditModel).where(
                AgentAuthorizationAuditModel.user_id == user_id,
                AgentAuthorizationAuditModel.grant_id == grant_id)
                .order_by(AgentAuthorizationAuditModel.created_at.desc()).limit(50)).all()
            result["audit"] = [{"action": item.action, "resource_id": item.resource_id,
                "allowed": item.allowed, "reason": item.reason, "created_at": _utc(item.created_at).isoformat()}
                for item in events]
        try:
            self.restore(user_id=user_id, grant_id=grant_id, run_id=result["run_id"], agent_id=result["agent_id"])
            result["effective"] = True
        except AgentAuthorizationError as exc:
            result["effective"], result["inactive_reason"] = False, str(exc)
        return result

    def list(self, *, user_id: str, limit: int = 30) -> list[dict]:
        with self.database.session() as session:
            rows = session.scalars(select(AgentTaskGrantModel).where(AgentTaskGrantModel.user_id == user_id)
                .order_by(AgentTaskGrantModel.created_at.desc()).limit(max(1, min(limit, 100)))).all()
            result = []
            for row in rows:
                view = self._view(row)
                context = replace(AgentExecutionContext.product(user_id=user_id), task_grant_id=row.id,
                    task_run_id=row.run_id, task_agent_id=row.agent_id)
                try:
                    self.check_context(context)
                    view["effective"] = True
                except AgentAuthorizationError as exc:
                    view["effective"], view["inactive_reason"] = False, str(exc)
                result.append(view)
            return result

    def _view(self, row: AgentTaskGrantModel) -> dict:
        status = "expired" if row.status == "active" and _utc(row.expires_at) <= _utc(self.clock()) else row.status
        return {"id": row.id, "run_id": row.run_id, "agent_id": row.agent_id,
            "parent_grant_id": row.parent_grant_id, "source": row.source, "status": status,
            "portfolio_ids": list(row.portfolio_ids), "capabilities": list(row.capabilities),
            "created_at": _utc(row.created_at).isoformat(), "expires_at": _utc(row.expires_at).isoformat(),
            "revoked_at": _utc(row.revoked_at).isoformat() if row.revoked_at else None,
            "revoke_reason": row.revoke_reason}

    def _audit(self, context, action, resource_id, allowed, reason):
        with self.database.session() as session, session.begin():
            session.add(AgentAuthorizationAuditModel(id=f"authz_{uuid4().hex}", user_id=context.user_id,
                grant_id=context.task_grant_id or "missing", action=str(action)[:80],
                resource_id=str(resource_id)[:100], allowed=allowed, reason=str(reason)[:200], created_at=_utc(self.clock())))


@lru_cache(maxsize=1)
def get_task_authorization_service() -> TaskAuthorizationService:
    return TaskAuthorizationService()


def guard_agent_portfolio(user_id: str, portfolio_id: str, action: str = PORTFOLIO_READ) -> None:
    """业务数据出口使用的同步 guard；线程池必须显式传播 Agent ContextVar。"""
    context = current_agent_execution_context()
    if not context.task_grant_id:
        return
    if context.user_id != user_id:
        raise AgentAuthorizationError("resource_denied", "当前任务不能访问其他用户的组合。")
    # 工具边界已经记审计。领域 Service 可能持有账本事务，这里不另开审计写事务，
    # 避免 SQLite 的单写锁互相等待；仍然每次真实查询 SQL/OpenFGA，不使用允许缓存。
    get_task_authorization_service().authorize(context, action=action, portfolio_id=portfolio_id, audit=False)


def guard_agent_write() -> None:
    """本轮授权不包含资产变更。即使未来错误地把写工具放入池中，也不能改账。"""
    if current_agent_execution_context().task_grant_id:
        raise AgentAuthorizationError("write_not_granted", "Agent 任务只能分析，不能修改持仓或交易。")


def allowed_agent_portfolio_ids(user_id: str) -> frozenset[str] | None:
    context = current_agent_execution_context()
    if not context.task_grant_id:
        return None
    if context.user_id != user_id:
        raise AgentAuthorizationError("resource_denied", "任务与资源用户不一致。")
    row = get_task_authorization_service()._checked_row(context)
    return frozenset(row.portfolio_ids)


def check_active_task_authorization() -> None:
    """模型调用、结果交付前的廉价生命周期检查，不把已有数据当成永久通行证。"""
    context = current_agent_execution_context()
    if context.task_grant_id:
        get_task_authorization_service().check_context(context)
