"""初始化真实 OpenFGA，仅保存 URL/store_id/model_id，不读写用户 .env。

本机独立部署与验证（在项目根目录运行，不启动主项目 API）：
    docker compose -f docker-compose.openfga.yml up -d --wait
    python -m scripts.setup_openfga
    python -m scripts.setup_openfga
    python -m unittest tests.test_openfga_authorization -v
    OPENFGA_INTEGRATION=1 python -m unittest tests.test_openfga_authorization.OpenFGAIntegrationTests -v

正式 Compose（预先在运行环境安全注入 OPENFGA_POSTGRES_PASSWORD 和 OPENFGA_TOKEN）：
    docker compose --profile openfga up -d --wait openfga
    python -m scripts.setup_openfga
容器内使用 http://openfga:8080；API/Worker 从共享 data/openfga.json 读取 ID。
密码用于连接 URI，应采用 URL-safe 字符；不要把密钥写进命令行参数或版本库。
两套 Compose 是替代方案，默认都占用 8091；更换数据库后不能复用旧 ID。
本地生成的 data/openfga.json 及其 .lock 是运行数据，不应提交到版本库。

OPENFGA_URL 默认 http://127.0.0.1:8091，支持 OPENFGA_STORE_ID、OPENFGA_MODEL_ID、
OPENFGA_TOKEN、OPENFGA_TIMEOUT_SECONDS 和 OPENFGA_CONFIG_PATH。环境/命令行指定
的 ID 优先于配置文件；指定的模型必须与 infra/openfga-model.json 一致。
重复执行会读取和校验已有模型，不悄悄升级模型，也不在 404/断网时另建 store。
丢失配置时先按名称查 store、按内容查 model；同名 store 有多个时要求明确 ID。

官方资料（2026-09-21 核对稳定版 v1.20.0）：
https://github.com/openfga/openfga/releases/tag/v1.20.0
https://openfga.dev/docs/getting-started/setup-openfga/docker
https://openfga.dev/docs/getting-started/update-tuples
https://openfga.dev/docs/interacting/consistency
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "data" / "openfga.json"
MODEL_PATH = PROJECT_ROOT / "infra" / "openfga-model.json"

# 同时支持 python -m scripts.setup_openfga 和 python scripts/setup_openfga.py。
if __package__ in {None, ""}:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.openfga_authorization import (  # noqa: E402
    OpenFGAPolicyError,
    _OpenFGAClient,
    _validated_id,
)


def _read_json(path: Path) -> dict:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise OpenFGAPolicyError("OpenFGA 配置或模型文件无法读取为 JSON") from None
    if not isinstance(result, dict):
        raise OpenFGAPolicyError("OpenFGA 配置或模型文件必须是 JSON 对象")
    return result


def _save_config(path: Path, config: dict) -> None:
    # 文件只含非敏感 ID；允许容器的非特权 UID 读取，但只有所有者可写。
    # 临时文件与目标同目录，replace 是原子替换。
    if path.exists() and _read_json(path) == config:
        path.chmod(0o644)
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(config, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fchmod(handle.fileno(), 0o644)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _model_definition(model: dict) -> dict:
    """去除 ID 和协议补齐的空字段，但保留 this/wildcard 等授权语义。"""
    if not isinstance(model, dict) or not isinstance(model.get("type_definitions"), list):
        raise OpenFGAPolicyError("OpenFGA 模型响应缺少 type_definitions")
    definitions = model["type_definitions"]
    if not definitions or any(not isinstance(item, dict) or not isinstance(item.get("type"), str) for item in definitions):
        raise OpenFGAPolicyError("OpenFGA 模型的类型定义无效")

    def normalize(value):
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            item = normalize(item)
            if key in {"relations", "metadata", "conditions"} and item in ({}, None):
                continue
            if key in {"condition", "module"} and item == "":
                continue
            if key == "source_info" and item is None:
                continue
            result[key] = item
        return result

    return normalize({
        "schema_version": model.get("schema_version"),
        "type_definitions": sorted(definitions, key=lambda item: item["type"]),
        "conditions": model.get("conditions", {}),
    })


def _list_items(client: _OpenFGAClient, path: str, field: str, **filters):
    continuation = ""
    seen = set()
    while True:
        page = client.request("GET", path, params={"page_size": 100, "continuation_token": continuation, **filters})
        items = page.get(field)
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise OpenFGAPolicyError("OpenFGA 列表响应无效")
        yield from items
        continuation = page.get("continuation_token", "")
        if not isinstance(continuation, str) or continuation in seen:
            raise OpenFGAPolicyError("OpenFGA 列表分页响应无效")
        if not continuation:
            return
        seen.add(continuation)


def setup_openfga(
    *,
    url: str = "http://127.0.0.1:8091",
    config_path: Path = DEFAULT_CONFIG_PATH,
    store_id: str = "",
    model_id: str = "",
    token: str = "",
    timeout_seconds: float = 3,
    store_name: str = "fundscope",
) -> dict[str, str]:
    """创建或复用固定模型，失败时保留已有配置，不自动重试。"""
    client = _OpenFGAClient(url, token, timeout_seconds)
    config_path = Path(config_path)
    if config_path.name == ".env" or config_path.name.startswith(".env.") or config_path.is_symlink():
        raise OpenFGAPolicyError("OpenFGA 配置不得指向 .env 文件或符号链接")
    model = _read_json(MODEL_PATH)
    expected = _model_definition(model)
    if not isinstance(store_name, str) or not 3 <= len(store_name) <= 64:
        raise OpenFGAPolicyError("OpenFGA store 名称长度必须为 3 到 64")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    # 避免两个本机初始化进程同时创建 store；锁文件本身不保存凭据。
    lock_fd = os.open(str(config_path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(lock_fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise OpenFGAPolicyError("已有 OpenFGA 初始化正在运行") from None
        saved = _read_json(config_path) if config_path.exists() else {}
        if saved:
            _validated_id(saved.get("store_id"), "配置 store_id")
            if saved.get("model_id"):
                _validated_id(saved["model_id"], "配置 model_id")
            if not isinstance(saved.get("url"), str):
                raise OpenFGAPolicyError("OpenFGA 配置缺少 url")
            if not store_id and saved["url"].rstrip("/") != client.url:
                raise OpenFGAPolicyError("OpenFGA URL 与已有配置不同，请明确指定 store_id 或使用另一配置文件")

        selected_store = store_id or saved.get("store_id", "")
        selected_model = model_id
        if not selected_model and selected_store == saved.get("store_id"):
            selected_model = saved.get("model_id", "")
        if selected_model and not selected_store:
            raise OpenFGAPolicyError("指定 OpenFGA model_id 时必须同时提供 store_id")

        if selected_store:
            selected_store = _validated_id(selected_store, "store_id")
            store = client.request("GET", f"stores/{selected_store}")
            if store.get("id") != selected_store:
                raise OpenFGAPolicyError("OpenFGA store 响应 ID 不一致")
        else:
            matches = [item for item in _list_items(client, "stores", "stores", name=store_name) if item.get("name") == store_name]
            if len(matches) > 1:
                raise OpenFGAPolicyError("存在多个同名 OpenFGA store，请明确指定 store_id")
            store = matches[0] if matches else client.request("POST", "stores", body={"name": store_name}, expected_status=201)
            selected_store = _validated_id(store.get("id"), "响应 store_id")
            # 先落下 store ID；后续模型写入失败时，下一次可接着初始化。
            _save_config(config_path, {"url": client.url, "store_id": selected_store, "model_id": ""})

        model_path = f"stores/{selected_store}/authorization-models"
        if selected_model:
            selected_model = _validated_id(selected_model, "model_id")
            response = client.request("GET", f"{model_path}/{selected_model}")
            remote = response.get("authorization_model")
            if _model_definition(remote) != expected or remote.get("id") != selected_model:
                raise OpenFGAPolicyError("OpenFGA 固定模型与项目模型不一致，未修改配置或模型")
        else:
            for remote in _list_items(client, model_path, "authorization_models"):
                if _model_definition(remote) == expected:
                    selected_model = _validated_id(remote.get("id"), "响应 model_id")
                    break
            if not selected_model:
                response = client.request("POST", model_path, body=model, expected_status=201)
                selected_model = _validated_id(response.get("authorization_model_id"), "响应 model_id")

        config = {"url": client.url, "store_id": selected_store, "model_id": selected_model}
        _save_config(config_path, config)
        return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.environ.get("OPENFGA_URL", "http://127.0.0.1:8091"))
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("OPENFGA_CONFIG_PATH", DEFAULT_CONFIG_PATH)))
    parser.add_argument("--store-id", default=os.environ.get("OPENFGA_STORE_ID", ""))
    parser.add_argument("--model-id", default=os.environ.get("OPENFGA_MODEL_ID", ""))
    parser.add_argument("--store-name", default="fundscope")
    args = parser.parse_args(argv)
    try:
        try:
            timeout = float(os.environ.get("OPENFGA_TIMEOUT_SECONDS", "3"))
        except ValueError:
            raise OpenFGAPolicyError("OPENFGA_TIMEOUT_SECONDS 必须是有限正数") from None
        config = setup_openfga(
            url=args.url,
            config_path=args.config,
            store_id=args.store_id,
            model_id=args.model_id,
            store_name=args.store_name,
            token=os.environ.get("OPENFGA_TOKEN", ""),
            timeout_seconds=timeout,
        )
    except OpenFGAPolicyError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError:
        print("OpenFGA 本地配置无法安全读写", file=sys.stderr)
        return 1
    print(f"OpenFGA 已就绪: store_id={config['store_id']} model_id={config['model_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
