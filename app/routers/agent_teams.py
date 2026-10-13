"""s15-s17 团队、协议与 Autonomous Agents 的确定性 REST 测试接口。

主 Agent 可以通过 tools 操作团队；这些接口提供相同的 service 边界，方便初学者先验证
Teammate Registry、Message Bus 和后台 teammate AgentRun，再观察模型如何自主选择工具。
s16 的协议接口继续复用同一个 router，便于比较普通消息与类型化请求的区别。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.schemas import (
    AutonomySessionActionRequest,
    AutonomySessionListResponse,
    AutonomySessionResponse,
    AutonomySessionStartRequest,
    AutonomyTaskCreateRequest,
    AgentTaskInfo,
    TeamInboxResponse,
    TeamMessageSendRequest,
    TeamMessageSendResponse,
    TeamPlanReviewRequest,
    TeamPlanSubmitRequest,
    TeamProtocolListResponse,
    TeamProtocolResponse,
    TeamShutdownRequest,
    TeammateCreateRequest,
    TeammateListResponse,
    TeammateResponse,
    WorktreeActionRequest,
    WorktreeCreateRequest,
    WorktreeListResponse,
    WorktreeReadinessResponse,
    WorktreeResponse,
)
from app.services.autonomous_agents import autonomous_agent_service
from app.services.agent_teams import MAIN_AGENT_NAME, agent_team_service
from app.services.tasks import task_store_service
from app.services.team_protocols import team_protocol_service
from app.services.worktrees import GitCommandError, worktree_service


router = APIRouter(prefix="/agent-teams", tags=["agent-teams"])


@router.get(
    "/worktrees/readiness",
    response_model=WorktreeReadinessResponse,
)
async def get_worktree_readiness():
    """确认当前项目是否已经 git init 且存在首个 commit。"""

    return worktree_service.readiness()


@router.post("/worktrees", response_model=WorktreeResponse)
async def create_worktree(request: WorktreeCreateRequest):
    """创建真实 Git Worktree，并可绑定一条尚未认领的 pending Task。"""

    try:
        return worktree_service.create_worktree(
            user_id=request.user_id,
            name=request.name,
            task_id=request.task_id,
            base_ref=request.base_ref,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (RuntimeError, GitCommandError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/worktrees", response_model=WorktreeListResponse)
async def list_worktrees(
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    status: str | None = Query(default=None),
    max_results: int = Query(default=30, ge=1, le=100),
):
    """查看 Worktree、Task 绑定、dirty 文件和领先提交数。"""

    try:
        return worktree_service.list_worktrees(
            user_id=user_id,
            status=status,
            max_results=max_results,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/worktrees/{name}", response_model=WorktreeResponse)
async def get_worktree(
    name: str,
    user_id: str = Query(default="guest", min_length=1, max_length=50),
):
    """查看一个 Worktree 的最新状态和生命周期事件。"""

    try:
        return {
            "ok": True,
            **worktree_service.get_worktree(user_id=user_id, name=name),
            "message": "Worktree 状态已刷新。",
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/worktrees/{name}/keep", response_model=WorktreeResponse)
async def keep_worktree(name: str, request: WorktreeActionRequest):
    """保留代码供人工 review；这个动作不会合并或删除分支。"""

    try:
        return worktree_service.keep_worktree(
            user_id=request.user_id,
            name=name,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except (ValueError, GitCommandError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/worktrees/{name}/remove", response_model=WorktreeResponse)
async def remove_worktree(name: str, request: WorktreeActionRequest):
    """删除临时目录和分支；默认保护未提交文件与独立 commit。"""

    try:
        return worktree_service.remove_worktree(
            user_id=request.user_id,
            name=name,
            discard_changes=request.discard_changes,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except (ValueError, GitCommandError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/autonomy/tasks", response_model=AgentTaskInfo)
async def create_autonomy_task(request: AutonomyTaskCreateRequest):
    """向 Task Board 写入任务；自治 Worker 启动后会主动扫描并认领。"""

    try:
        result = task_store_service.create_task(
            user_id=request.user_id,
            task_id=request.task_id,
            subject=request.subject,
            description=request.description,
            blocked_by=request.blocked_by,
        )
        return result["task"]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/autonomy/tasks", response_model=list[AgentTaskInfo])
async def list_autonomy_tasks(
    user_id: str = Query(default="guest", min_length=1, max_length=50),
):
    """查看完整任务板，包括 owner、依赖和是否可领取。"""

    try:
        return task_store_service.list_tasks(user_id=user_id)["tasks"]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/autonomy/sessions", response_model=AutonomySessionResponse)
async def start_autonomy_session(request: AutonomySessionStartRequest):
    """为已有 teammate 启动一个有界 IDLE/WORK 自治循环。"""

    try:
        return autonomous_agent_service.start_session(
            user_id=request.user_id,
            teammate_name=request.teammate_name,
            poll_interval_seconds=request.poll_interval_seconds,
            max_idle_polls=request.max_idle_polls,
            max_tasks=request.max_tasks,
            max_rounds_per_task=request.max_rounds_per_task,
            timeout_seconds=request.timeout_seconds,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/autonomy/sessions", response_model=AutonomySessionListResponse)
async def list_autonomy_sessions(
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    status: str | None = Query(default=None),
    max_results: int = Query(default=30, ge=1, le=100),
):
    """查看自治会话当前 phase、进度和停止原因。"""

    try:
        return autonomous_agent_service.list_sessions(
            user_id=user_id,
            status=status,
            max_results=max_results,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/autonomy/sessions/{session_id}", response_model=AutonomySessionResponse
)
async def get_autonomy_session(
    session_id: str,
    user_id: str = Query(default="guest", min_length=1, max_length=50),
):
    """读取一个会话，适合轮询观察 IDLE -> WORK -> IDLE。"""

    try:
        return autonomous_agent_service.get_session(
            user_id=user_id,
            session_id=session_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/autonomy/sessions/{session_id}/stop",
    response_model=AutonomySessionResponse,
)
async def stop_autonomy_session(
    session_id: str,
    request: AutonomySessionActionRequest,
):
    """持久化停止信号；Worker 在下一个 Harness 检查点安全退出。"""

    try:
        return autonomous_agent_service.request_stop(
            user_id=request.user_id,
            session_id=session_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/teammates", response_model=TeammateResponse)
async def spawn_teammate(request: TeammateCreateRequest):
    """注册一个长期 teammate；这个动作本身不会调用模型。"""

    # Pydantic 先校验字段形状，Service 再校验名称冲突、数量上限等业务规则。
    try:
        return agent_team_service.spawn_teammate(
            user_id=request.user_id,
            name=request.name,
            role=request.role,
            instructions=request.instructions,
            capabilities=request.capabilities,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/teammates", response_model=TeammateListResponse)
async def list_teammates(
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    max_results: int = Query(default=30, ge=1, le=100),
):
    """查看当前用户的 teammate 名称、角色和运行状态。"""

    try:
        return agent_team_service.list_teammates(
            user_id=user_id,
            max_results=max_results,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/teammates/{name}", response_model=TeammateResponse)
async def get_teammate(
    name: str,
    user_id: str = Query(default="guest", min_length=1, max_length=50),
):
    """读取一个 teammate，适合在后台 Job 完成后确认它是否回到 idle。"""

    try:
        return agent_team_service.get_teammate(user_id=user_id, name=name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/messages", response_model=TeamMessageSendResponse)
async def send_team_message(request: TeamMessageSendRequest):
    """main-agent 发消息；dispatch=true 时立即提交后台 teammate AgentRun。"""

    try:
        # REST 端固定 sender=main-agent，不能让客户端伪装成任意 teammate。
        return agent_team_service.send_message(
            user_id=request.user_id,
            sender=MAIN_AGENT_NAME,
            recipient=request.recipient,
            content=request.content,
            dispatch=request.dispatch,
            max_rounds=request.max_rounds,
            max_retries=request.max_retries,
            timeout_seconds=request.timeout_seconds,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        # 消息可能已成功落库，但 Redis/Celery 投递失败，所以用 503 表示基础设施故障。
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/inbox", response_model=TeamInboxResponse)
async def check_team_inbox(
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    recipient: str = Query(default=MAIN_AGENT_NAME, min_length=1, max_length=50),
    unread_only: bool = Query(default=False),
    mark_read: bool = Query(default=False),
    max_results: int = Query(default=30, ge=1, le=100),
):
    """查看 main-agent 或 teammate inbox；默认只读，不自动改变已读状态。"""

    # unread_only 控制查询范围；mark_read 控制是否产生写操作，两者可以独立组合。
    try:
        return agent_team_service.check_inbox(
            user_id=user_id,
            recipient=recipient,
            unread_only=unread_only,
            mark_read=mark_read,
            max_results=max_results,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/protocols/plans", response_model=TeamProtocolResponse)
async def submit_team_plan(request: TeamPlanSubmitRequest):
    """模拟 teammate 提交计划，创建 pending 的 plan_approval_request。"""

    # 正常运行时 teammate 会通过专属 submit_team_plan_tool 走到同一个 Service；
    # 这个 REST 入口用于教学测试，因此允许前端明确指定现有 teammate。
    try:
        return team_protocol_service.submit_plan(
            user_id=request.user_id,
            teammate_name=request.teammate_name,
            plan=request.plan,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/protocols/shutdown", response_model=TeamProtocolResponse)
async def request_teammate_shutdown(request: TeamShutdownRequest):
    """创建 shutdown_request；dispatch=true 时交给后台 Worker 完成握手。"""

    try:
        return team_protocol_service.request_shutdown(
            user_id=request.user_id,
            teammate_name=request.teammate_name,
            reason=request.reason,
            dispatch=request.dispatch,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        # 协议请求已经落库但 Queue/Celery 提交失败时，客户端可稍后按 request_id 查询。
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/protocols", response_model=TeamProtocolListResponse)
async def list_team_protocols(
    user_id: str = Query(default="guest", min_length=1, max_length=50),
    status: str | None = Query(default=None),
    max_results: int = Query(default=30, ge=1, le=100),
):
    """列出 ProtocolState 和近期 typed events，可用 status 筛 pending 请求。"""

    try:
        return team_protocol_service.list_protocols(
            user_id=user_id,
            status=status,
            max_results=max_results,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/protocols/{request_id}", response_model=TeamProtocolResponse)
async def get_team_protocol(
    request_id: str,
    user_id: str = Query(default="guest", min_length=1, max_length=50),
):
    """按 request_id 查看请求和响应事件是否正确配对。"""

    try:
        return team_protocol_service.get_protocol(
            user_id=user_id,
            request_id=request_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/protocols/{request_id}/review",
    response_model=TeamProtocolResponse,
)
async def review_team_plan(
    request_id: str,
    request: TeamPlanReviewRequest,
):
    """main-agent 对一条 pending 计划请求执行一次批准或拒绝。"""

    try:
        return team_protocol_service.review_plan(
            user_id=request.user_id,
            request_id=request_id,
            approve=request.approve,
            feedback=request.feedback,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
