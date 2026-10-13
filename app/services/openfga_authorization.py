"""OpenFGA 同步 REST 传输层，不读取 Settings，也不参与 SQL 授权生命周期。

两种 capability 独立授权，researcher 不隐含 reader。调用方必须先确认
SQL grant 仍有效，再使用 check；撤销时也应先让 SQL grant 失效。
任何不确定的响应都抛错，调用方不能把异常转换成放行。
"""

from __future__ import annotations

import math
import re

import httpx


_RELATIONS = {
    "portfolio:read": "reader",
    "portfolio:research-run": "researcher",
}
_ID_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}\Z")
_MAX_TUPLES = 100


class OpenFGAPolicyError(RuntimeError):
    """配置、请求或响应有问题，不能据此认定用户有权限。"""


def _validated_id(value: str, field: str) -> str:
    # 只接收原始 ID，拒绝 wildcard、userset 和路径注入，不自动修改 ID。
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise OpenFGAPolicyError(f"OpenFGA {field} 无效")
    return value


class _OpenFGAClient:
    """供策略和初始化脚本共用的最小 HTTP 边界。每次请求后关闭连接。"""

    def __init__(self, url: str, token: str = "", timeout_seconds: float = 3):
        try:
            parsed = httpx.URL(url)
        except (httpx.InvalidURL, TypeError, ValueError):
            raise OpenFGAPolicyError("OpenFGA URL 无效") from None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.host
            or parsed.userinfo
            or parsed.query
            or parsed.fragment
        ):
            raise OpenFGAPolicyError("OpenFGA URL 必须是无凭据、查询参数和片段的 HTTP(S) 地址")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise OpenFGAPolicyError("OpenFGA timeout_seconds 必须是有限正数")
        if not isinstance(token, str) or any(not 33 <= ord(char) <= 126 for char in token):
            raise OpenFGAPolicyError("OpenFGA token 格式无效")
        self.url = str(parsed).rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._headers = {"Accept": "application/json"}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        params: dict | None = None,
        expected_status: int = 200,
    ) -> dict:
        try:
            # HTTPX 默认不重试；不跟随重定向，也不经由环境代理转发凭据。
            with httpx.Client(
                base_url=self.url + "/",
                headers=self._headers,
                timeout=self.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = client.request(method, path, json=body, params=params)
        except (httpx.HTTPError, httpx.InvalidURL):
            # 原始异常和响应正文可能带有凭据，不拼进消息或异常链。
            raise OpenFGAPolicyError("OpenFGA 请求失败，未取得可信结果") from None
        if response.status_code != expected_status:
            raise OpenFGAPolicyError(f"OpenFGA 返回 HTTP {response.status_code}")
        try:
            result = response.json()
        except ValueError:
            raise OpenFGAPolicyError("OpenFGA 返回无效 JSON") from None
        if not isinstance(result, dict):
            raise OpenFGAPolicyError("OpenFGA 返回的 JSON 必须是对象")
        return result


class OpenFGAPolicy:
    """固定 store/model 的授权客户端；所有失败显式抛 OpenFGAPolicyError。"""

    def __init__(
        self,
        url: str,
        store_id: str,
        model_id: str,
        token: str = "",
        timeout_seconds: float = 3,
    ):
        self.store_id = _validated_id(store_id, "store_id")
        self.model_id = _validated_id(model_id, "model_id")
        self._client = _OpenFGAClient(url, token, timeout_seconds)

    def check_available(self) -> None:
        """就绪检查读取固定模型，不创建授权，也不访问用户组合。"""

        result = self._client.request(
            "GET", f"stores/{self.store_id}/authorization-models/{self.model_id}"
        )
        model = result.get("authorization_model")
        if not isinstance(model, dict) or model.get("id") != self.model_id:
            raise OpenFGAPolicyError("OpenFGA 固定模型不可用")

    def grant(
        self, grant_id: str, portfolio_ids: list[str], capabilities: list[str]
    ) -> None:
        """增加指定 tuples，重复授予不报错；不会替换或缩小已有权限。"""
        self._write(grant_id, portfolio_ids, capabilities, deleting=False)

    def revoke(
        self, grant_id: str, portfolio_ids: list[str], capabilities: list[str]
    ) -> None:
        """幂等删除指定 tuples；SQL 撤销顺序由业务层负责。"""
        self._write(grant_id, portfolio_ids, capabilities, deleting=True)

    def check(self, grant_id: str, action: str, portfolio_id: str) -> bool:
        grant_id = _validated_id(grant_id, "grant_id")
        portfolio_id = _validated_id(portfolio_id, "portfolio_id")
        relation = self._relation(action)
        result = self._client.request(
            "POST",
            f"stores/{self.store_id}/check",
            body={
                "authorization_model_id": self.model_id,
                "tuple_key": {
                    "user": f"grant:{grant_id}",
                    "relation": relation,
                    "object": f"portfolio:{portfolio_id}",
                },
                "consistency": "HIGHER_CONSISTENCY",
            },
        )
        # 不能用 bool(...)：字符串 "false" 和整数 1 都不是可信授权结果。
        allowed = result.get("allowed")
        if type(allowed) is not bool:
            raise OpenFGAPolicyError("OpenFGA check 缺少布尔 allowed 字段")
        return allowed

    @staticmethod
    def _relation(action: str) -> str:
        if not isinstance(action, str) or action not in _RELATIONS:
            raise OpenFGAPolicyError("OpenFGA 不支持该 capability")
        return _RELATIONS[action]

    def _write(
        self,
        grant_id: str,
        portfolio_ids: list[str],
        capabilities: list[str],
        *,
        deleting: bool,
    ) -> None:
        grant_id = _validated_id(grant_id, "grant_id")
        if not isinstance(portfolio_ids, list) or not isinstance(capabilities, list):
            raise OpenFGAPolicyError("OpenFGA portfolio_ids 和 capabilities 必须是列表")
        # 请求前完成全部校验和去重，避免错误输入已经写入部分权限。
        portfolios = dict.fromkeys(_validated_id(item, "portfolio_id") for item in portfolio_ids)
        relations = dict.fromkeys(self._relation(item) for item in capabilities)
        if len(portfolios) * len(relations) > _MAX_TUPLES:
            raise OpenFGAPolicyError("OpenFGA 单次授权最多 100 个唯一 tuples，不自动拆分事务")
        tuples = [
            {"user": f"grant:{grant_id}", "relation": relation, "object": f"portfolio:{portfolio}"}
            for portfolio in portfolios
            for relation in relations
        ]
        if not tuples:
            return
        operation = "deletes" if deleting else "writes"
        conflict = "on_missing" if deleting else "on_duplicate"
        result = self._client.request(
            "POST",
            f"stores/{self.store_id}/write",
            body={
                "authorization_model_id": self.model_id,
                operation: {"tuple_keys": tuples, conflict: "ignore"},
            },
        )
        # v1.20.0 的 Write 实测返回 HTTP 200 {}，不能把异常正文当作成功确认。
        if result:
            raise OpenFGAPolicyError("OpenFGA write 返回了非预期响应")
