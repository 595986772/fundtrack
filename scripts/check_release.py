"""Run the offline release suite with temporary storage and no provider access."""
from contextlib import ExitStack
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch


MODULES = (
    "tests.test_fund_fixtures",
    "tests.test_portfolios",
    "tests.test_portfolio_trades",
    "tests.test_fund_transaction_timing",
    "tests.test_agent_loop_guard",
    "tests.test_tool_discovery",
    "tests.test_tool_recovery",
    "tests.test_fund_report_retrieval",
    "tests.test_fund_report_answer",
    "tests.test_fund_report_runtime",
    "tests.test_reliable_jobs",
    "tests.test_reliable_research_checkpoints",
    "tests.test_agent_task_authorization",
    "tests.test_ai_governance.UsageTests",
    "tests.test_ai_governance.CapacityTests",
    "tests.test_ai_governance.ModelTests",
    "tests.test_production_runtime",
    "tests.test_backup_restore.RecoveryAuditTests",
    "tests.test_backup_restore.BackupSafetyTests",
)


def main():
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    with tempfile.TemporaryDirectory(prefix="fundtrack-check-") as directory, ExitStack() as stack:
        # Import application settings only after replacing environment and dotenv loading.
        # Test cases requiring auth or budget checks enable them explicitly with local fakes.
        base = Path(directory)
        stack.enter_context(patch.dict(os.environ, {
            "DATABASE_URL": f"sqlite:///{base / 'runtime.db'}",
            "DATABASE_AUTO_CREATE_SCHEMA": "true", "DEPLOYMENT_MODE": "local",
            "LANGGRAPH_CHECKPOINT_BACKEND": "sqlite", "JOB_EXECUTION_MODE": "local",
            "LANGGRAPH_CHECKPOINT_DATABASE_URL": f"sqlite:///{base / 'checkpoints.db'}",
            "MPLCONFIGDIR": str(base / "matplotlib"),
            "CHROMA_PERSIST_DIR": str(base / "chroma"),
            "FUND_REPORT_STORAGE_DIR": str(base / "reports"),
            "OPENFGA_CONFIG_PATH": str(base / "openfga.json"),
            "OPENFGA_STORE_ID": "", "OPENFGA_MODEL_ID": "", "OPENFGA_TOKEN": "",
            "AUTH_REQUIRED": "false", "AUTH_COOKIE_SECURE": "false",
            "AI_CONTROL_BACKEND": "memory", "AI_GOVERNANCE_ENABLED": "false",
            "TRACE_ENABLED": "false", "TRACE_LOCAL_ENABLED": "false",
            "TRACE_OTLP_ENDPOINT": "", "LANGFUSE_SECRET_KEY": "",
            "LANGFUSE_PUBLIC_KEY": "", "LANGCHAIN_TRACING_V2": "false",
            "LANGSMITH_TRACING": "false", "LANGSMITH_API_KEY": "",
            "DEEPSEEK_API_KEY": "offline-no-network", "DASHSCOPE_API_KEY": "",
            "OPENAI_API_KEY": "", "LLM_FALLBACK_API_KEY": "",
            "FUND_NAV_SYNC_ENABLED": "false", "MARKET_SNAPSHOT_SYNC_ENABLED": "false",
            "MARKET_EVENT_SYNC_ENABLED": "false", "XHS_SOCIAL_SIGNAL_ENABLED": "false",
        }))
        stack.enter_context(patch("dotenv.load_dotenv", return_value=False))

        original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex

        def local_only(original):
            def connect(sock, address):
                if sock.family in (socket.AF_INET, socket.AF_INET6):
                    raise RuntimeError("External connections are disabled in the release suite")
                return original(sock, address)
            return connect

        stack.enter_context(patch.object(socket.socket, "connect", local_only(original_connect)))
        stack.enter_context(patch.object(socket.socket, "connect_ex", local_only(original_connect_ex)))
        suite = unittest.defaultTestLoader.loadTestsFromNames(MODULES)
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
