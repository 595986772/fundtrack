"""本项目自带的教学 MCP Servers。

这里的 Server 会作为独立子进程运行，通过标准输入/输出与 FastAPI 里的
MCP Client 通信。把它单独放在一个包里，是为了强调：MCP Server 是外部能力，
并不属于主 Agent 的内置工具实现。
"""
