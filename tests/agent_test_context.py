"""Agent 权限测试使用的服务端身份工厂。"""

from app.services.agent_execution_context import AgentExecutionContext


def developer_context(user_id: str = "chen") -> AgentExecutionContext:
    """模拟已经由后端管理员依赖验证过的开发者调用。"""

    return AgentExecutionContext.internal_developer(
        user_id=user_id,
        source="deterministic-test",
    )
