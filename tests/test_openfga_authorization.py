"""默认全部离线；真实 Docker 测试仅由 OPENFGA_INTEGRATION=1 显式开启。

python -m unittest tests.test_openfga_authorization -v
OPENFGA_INTEGRATION=1 python -m unittest tests.test_openfga_authorization.OpenFGAIntegrationTests -v
真实测试只访问 OpenFGA，创建随机 ID 的临时 tuples 并在 finally 清理。
不导入应用 Settings/ORM，不启动 API，不访问付费模型。
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import fcntl
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import traceback
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx

from app.services.openfga_authorization import OpenFGAPolicy, OpenFGAPolicyError
from scripts.setup_openfga import DEFAULT_CONFIG_PATH, MODEL_PATH, main, setup_openfga


REAL_CLIENT = httpx.Client
STORE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
MODEL_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
OTHER_STORE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAX"
OTHER_MODEL_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAY"
URL = "http://127.0.0.1:8091"
SECRET = "test-only-sensitive-token"


class MockTransportCase(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.client_options = []
        self.handler = lambda request: httpx.Response(200, json={})

        def handle(request):
            self.requests.append(request)
            return self.handler(request)

        def client_factory(**kwargs):
            self.client_options.append(kwargs)
            return REAL_CLIENT(transport=httpx.MockTransport(handle), **kwargs)

        context = patch("app.services.openfga_authorization.httpx.Client", side_effect=client_factory)
        context.start()
        self.addCleanup(context.stop)

    def policy(self, **overrides):
        options = {"url": URL, "store_id": STORE_ID, "model_id": MODEL_ID}
        return OpenFGAPolicy(**(options | overrides))


class OpenFGAPolicyTests(MockTransportCase):
    def test_grant_maps_and_deduplicates_all_tuples_in_one_transaction(self):
        policy = self.policy(token=SECRET)
        policy.grant("g-1", ["p-1", "p-1", "p-2"], ["portfolio:read", "portfolio:research-run", "portfolio:read"])
        self.assertEqual((policy.store_id, policy.model_id), (STORE_ID, MODEL_ID))
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url.path, f"/stores/{STORE_ID}/write")
        self.assertEqual(request.headers["Authorization"], f"Bearer {SECRET}")
        self.assertEqual(json.loads(request.content), {
            "authorization_model_id": MODEL_ID,
            "writes": {
                "on_duplicate": "ignore",
                "tuple_keys": [
                    {"user": "grant:g-1", "relation": relation, "object": f"portfolio:{portfolio}"}
                    for portfolio in ["p-1", "p-2"]
                    for relation in ["reader", "researcher"]
                ],
            },
        })

    def test_research_does_not_implicitly_grant_read(self):
        self.policy().grant("g", ["p"], ["portfolio:research-run"])
        body = json.loads(self.requests[0].content)
        self.assertEqual(body["writes"]["tuple_keys"], [
            {"user": "grant:g", "relation": "researcher", "object": "portfolio:p"},
        ])

    def test_revoke_uses_idempotent_missing_delete_option(self):
        self.policy().revoke("g", ["p", "p"], ["portfolio:read"])
        self.assertEqual(json.loads(self.requests[0].content), {
            "authorization_model_id": MODEL_ID,
            "deletes": {
                "on_missing": "ignore",
                "tuple_keys": [{"user": "grant:g", "relation": "reader", "object": "portfolio:p"}],
            },
        })

    def test_check_pins_model_uses_higher_consistency_and_does_not_cache(self):
        policy = self.policy()
        for allowed, action, relation in [
            (True, "portfolio:read", "reader"),
            (False, "portfolio:read", "reader"),
            (True, "portfolio:research-run", "researcher"),
        ]:
            self.handler = lambda request: httpx.Response(200, json={"allowed": allowed})
            self.assertIs(policy.check("g", action, "p"), allowed)
            self.assertEqual(self.requests[-1].url.path, f"/stores/{STORE_ID}/check")
            self.assertEqual(json.loads(self.requests[-1].content), {
                "authorization_model_id": MODEL_ID,
                "tuple_key": {"user": "grant:g", "relation": relation, "object": "portfolio:p"},
                "consistency": "HIGHER_CONSISTENCY",
            })
        self.assertEqual(len(self.requests), 3)

    def test_http_options_keep_secrets_out_of_url_and_disable_redirects_and_env_proxies(self):
        self.handler = lambda request: httpx.Response(200, json={"allowed": False})
        self.policy(url="http://localhost:8091/openfga/", timeout_seconds=0.7).check("g", "portfolio:read", "p")
        request = self.requests[0]
        self.assertEqual(request.url.path, f"/openfga/stores/{STORE_ID}/check")
        self.assertNotIn("authorization", request.headers)
        self.assertEqual(set(request.extensions["timeout"].values()), {0.7})
        self.assertIs(self.client_options[0]["follow_redirects"], False)
        self.assertIs(self.client_options[0]["trust_env"], False)

    def test_http_failures_raise_once_for_check_grant_and_revoke(self):
        for method in ["check", "grant", "revoke"]:
            for status in [201, 202, 204, 301, 307, 400, 401, 403, 404, 409, 429, 500, 503]:
                with self.subTest(method=method, status=status):
                    self.requests.clear()
                    self.handler = lambda request: httpx.Response(status, text=SECRET, headers={"Location": "/other"})
                    policy = self.policy(token=SECRET)
                    with self.assertRaises(OpenFGAPolicyError) as raised:
                        if method == "check":
                            policy.check("g", "portfolio:read", "p")
                        else:
                            getattr(policy, method)("g", ["p"], ["portfolio:read"])
                    self.assertIn(f"HTTP {status}", str(raised.exception))
                    self.assertNotIn(SECRET, str(raised.exception))
                    self.assertEqual(len(self.requests), 1)

    def test_transport_failure_is_not_retried_and_exception_chain_is_redacted(self):
        for failure in [httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError]:
            with self.subTest(failure=failure):
                self.requests.clear()

                def fail(request):
                    raise failure(SECRET, request=request)

                self.handler = fail
                try:
                    self.policy(token=SECRET).check("g", "portfolio:read", "p")
                except OpenFGAPolicyError:
                    self.assertNotIn(SECRET, traceback.format_exc())
                else:
                    self.fail("传输错误必须拒绝")
                self.assertEqual(len(self.requests), 1)

    def test_previous_allow_is_not_reused_when_backend_becomes_unavailable(self):
        policy = self.policy()
        self.handler = lambda request: httpx.Response(200, json={"allowed": True})
        self.assertTrue(policy.check("g", "portfolio:read", "p"))
        self.handler = lambda request: httpx.Response(503, text=SECRET)
        with self.assertRaises(OpenFGAPolicyError):
            policy.check("g", "portfolio:read", "p")
        self.assertEqual(len(self.requests), 2)

    def test_invalid_check_response_never_becomes_truthy(self):
        for payload in [{}, {"allowed": None}, {"allowed": 1}, {"allowed": 0}, {"allowed": "false"}, {"allowed": "true"}, [], True, None]:
            with self.subTest(payload=payload):
                self.handler = lambda request: httpx.Response(200, content=json.dumps(payload))
                with self.assertRaises(OpenFGAPolicyError):
                    self.policy().check("g", "portfolio:read", "p")

    def test_non_json_response_raises_without_echoing_body(self):
        self.handler = lambda request: httpx.Response(200, text=SECRET)
        for operation in [lambda: self.policy().check("g", "portfolio:read", "p"), lambda: self.policy().grant("g", ["p"], ["portfolio:read"])]:
            with self.assertRaises(OpenFGAPolicyError) as raised:
                operation()
            self.assertNotIn(SECRET, str(raised.exception))

    def test_write_requires_empty_success_object_not_an_error_envelope(self):
        self.handler = lambda request: httpx.Response(200, json={"error": SECRET})
        for method in [self.policy().grant, self.policy().revoke]:
            with self.assertRaises(OpenFGAPolicyError) as raised:
                method("g", ["p"], ["portfolio:read"])
            self.assertNotIn(SECRET, str(raised.exception))

    def test_unsupported_capabilities_are_rejected_before_any_write(self):
        for action in ["reader", "portfolio:delete", "tool:run", "", None, []]:
            with self.subTest(action=action), self.assertRaises(OpenFGAPolicyError):
                self.policy().check("g", action, "p")
        for method in ["grant", "revoke"]:
            with self.assertRaises(OpenFGAPolicyError):
                getattr(self.policy(), method)("g", ["p"], ["portfolio:read", "unknown"])
        self.assertEqual(self.requests, [])

    def test_ids_cannot_inject_wildcard_usersets_or_paths(self):
        for invalid in ["", "*", "grant:g", "g#reader", "g/p", "../stores", "..", "g?x", "g%2f", " p", "p\n", None, 1, "x" * 201]:
            with self.subTest(invalid=invalid):
                for field in ["store_id", "model_id"]:
                    with self.assertRaises(OpenFGAPolicyError):
                        self.policy(**{field: invalid})
                with self.assertRaises(OpenFGAPolicyError):
                    self.policy().check(invalid, "portfolio:read", "p")
                with self.assertRaises(OpenFGAPolicyError):
                    self.policy().grant("g", ["p", invalid], ["portfolio:read"])
        self.assertEqual(self.requests, [])

    def test_bad_config_is_rejected_without_network_or_secret_in_error(self):
        for url in ["", "relative", "ftp://localhost", "http://", f"http://user:{SECRET}@localhost", f"http://localhost?token={SECRET}", f"http://localhost#{SECRET}"]:
            with self.subTest(url=url), self.assertRaises(OpenFGAPolicyError) as raised:
                self.policy(url=url)
            self.assertNotIn(SECRET, str(raised.exception))
        for timeout in [0, -1, float("nan"), float("inf"), "3", None, True]:
            with self.subTest(timeout=timeout), self.assertRaises(OpenFGAPolicyError):
                self.policy(timeout_seconds=timeout)
        for token in ["abc\r\nInjected: x", "has space", None]:
            with self.assertRaises(OpenFGAPolicyError):
                self.policy(token=token)
        self.assertEqual(self.requests, [])

    def test_empty_lists_are_noops_but_wrong_container_types_are_errors(self):
        policy = self.policy()
        for method in [policy.grant, policy.revoke]:
            method("g", [], ["portfolio:read"])
            method("g", ["p"], [])
            for portfolios, capabilities in [("p", ["portfolio:read"]), (["p"], "portfolio:read"), (None, [])]:
                with self.assertRaises(OpenFGAPolicyError):
                    method("g", portfolios, capabilities)
        self.assertEqual(self.requests, [])

    def test_tuple_limit_is_atomic_and_deduplication_precedes_counting(self):
        policy = self.policy()
        policy.grant("g", [f"p-{index}" for index in range(50)], ["portfolio:read", "portfolio:research-run"])
        self.assertEqual(len(json.loads(self.requests[0].content)["writes"]["tuple_keys"]), 100)
        policy.grant("g", ["p"] * 101, ["portfolio:read"])
        self.requests.clear()
        for method in [policy.grant, policy.revoke]:
            with self.assertRaises(OpenFGAPolicyError):
                method("g", [f"p-{index}" for index in range(101)], ["portfolio:read"])
        self.assertEqual(self.requests, [])


class OpenFGASetupTests(MockTransportCase):
    def setUp(self):
        super().setUp()
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config_path = Path(directory.name) / "openfga.json"
        self.model = json.loads(MODEL_PATH.read_text(encoding="utf-8"))
        self.stores = []
        self.models = []
        self.handler = self.server

    def server(self, request):
        path = request.url.path
        if path == "/stores":
            if request.method == "GET":
                return httpx.Response(200, json={"stores": self.stores, "continuation_token": ""})
            self.stores.append({"id": STORE_ID, "name": json.loads(request.content)["name"]})
            return httpx.Response(201, json=self.stores[-1])
        if request.method == "GET" and path == f"/stores/{STORE_ID}" and self.stores:
            return httpx.Response(200, json=self.stores[0])
        if path == f"/stores/{STORE_ID}/authorization-models":
            if request.method == "GET":
                return httpx.Response(200, json={"authorization_models": self.models, "continuation_token": ""})
            self.models.append(json.loads(request.content) | {"id": MODEL_ID})
            return httpx.Response(201, json={"authorization_model_id": MODEL_ID})
        if request.method == "GET" and path == f"/stores/{STORE_ID}/authorization-models/{MODEL_ID}" and self.models:
            return httpx.Response(200, json={"authorization_model": self.models[0]})
        return httpx.Response(404, json={"message": SECRET})

    def setup_store(self, **options):
        return setup_openfga(**({"url": URL, "config_path": self.config_path} | options))

    def existing(self):
        self.stores = [{"id": STORE_ID, "name": "fundscope"}]
        self.models = [self.model | {"id": MODEL_ID}]

    def test_fresh_setup_and_repeat_are_idempotent_and_never_persist_secret(self):
        expected = {"url": URL, "store_id": STORE_ID, "model_id": MODEL_ID}
        self.assertEqual(self.setup_store(token=SECRET), expected)
        raw = self.config_path.read_bytes()
        modified = self.config_path.stat().st_mtime_ns
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o644)
        self.assertNotIn(SECRET.encode(), raw)
        self.requests.clear()
        self.assertEqual(self.setup_store(token=SECRET), expected)
        self.assertEqual([r.method for r in self.requests], ["GET", "GET"])
        self.assertEqual(self.config_path.read_bytes(), raw)
        self.assertEqual(self.config_path.stat().st_mtime_ns, modified)
        self.assertEqual(len(self.stores), 1)
        self.assertEqual(len(self.models), 1)

    def test_lost_local_config_recovers_remote_store_and_model_without_post(self):
        self.existing()
        self.assertEqual(self.setup_store()["model_id"], MODEL_ID)
        self.assertTrue(all(r.method == "GET" for r in self.requests))

    def test_created_store_survives_failure_and_model_response_loss_is_recoverable(self):
        def response_lost(request):
            response = self.server(request)
            if request.method == "POST" and request.url.path.endswith("authorization-models"):
                raise httpx.ReadTimeout(SECRET, request=request)
            return response

        self.handler = response_lost
        with self.assertRaises(OpenFGAPolicyError):
            self.setup_store()
        self.assertEqual(json.loads(self.config_path.read_text())["store_id"], STORE_ID)
        self.assertEqual(json.loads(self.config_path.read_text())["model_id"], "")
        self.requests.clear()
        self.handler = self.server
        self.assertEqual(self.setup_store()["model_id"], MODEL_ID)
        self.assertTrue(all(r.method == "GET" for r in self.requests))
        self.assertEqual(len(self.models), 1)

    def test_deleted_store_fails_instead_of_silently_replacing_it(self):
        self.setup_store()
        before = self.config_path.read_bytes()
        self.stores.clear()
        self.requests.clear()
        with self.assertRaises(OpenFGAPolicyError):
            self.setup_store()
        self.assertEqual([r.method for r in self.requests], ["GET"])
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_changed_url_cannot_silently_reuse_old_ids(self):
        self.setup_store()
        self.requests.clear()
        with self.assertRaises(OpenFGAPolicyError):
            self.setup_store(url="http://other-host:8091")
        self.assertEqual(self.requests, [])

    def test_fixed_model_mismatch_or_missing_model_never_creates_replacement(self):
        self.setup_store()
        before = self.config_path.read_bytes()
        for changed in [[], [self.model | {"id": MODEL_ID, "conditions": {"unexpected": {}}}]]:
            with self.subTest(changed=changed):
                self.models = changed
                self.requests.clear()
                with self.assertRaises(OpenFGAPolicyError):
                    self.setup_store()
                self.assertTrue(all(r.method == "GET" for r in self.requests))
                self.assertEqual(self.config_path.read_bytes(), before)

    def test_model_normalization_accepts_empty_protocol_defaults_not_wildcards(self):
        self.existing()
        self.models[0] = deepcopy(self.models[0])
        self.models[0]["type_definitions"][0].update({"relations": {}, "metadata": {"relations": {}}})
        self.models[0]["conditions"] = {}
        metadata = self.models[0]["type_definitions"][1]["metadata"]
        metadata.update({"module": "", "source_info": None})
        for relation in metadata["relations"].values():
            relation.update({"module": "", "source_info": None})
            relation["directly_related_user_types"][0]["condition"] = ""
        self.setup_store()
        self.models[0]["type_definitions"][1]["metadata"]["relations"]["reader"]["directly_related_user_types"][0]["wildcard"] = {}
        with self.assertRaises(OpenFGAPolicyError):
            self.setup_store()

    def test_duplicate_store_names_require_explicit_id(self):
        self.existing()
        self.stores.append({"id": OTHER_STORE_ID, "name": "fundscope"})
        with self.assertRaises(OpenFGAPolicyError):
            self.setup_store()
        self.assertFalse(self.config_path.exists())
        self.assertEqual(self.setup_store(store_id=STORE_ID, model_id=MODEL_ID)["store_id"], STORE_ID)

    def test_pagination_finds_store_and_model_on_later_pages(self):
        self.existing()

        def pages(request):
            if request.method == "GET" and request.url.path in ["/stores", f"/stores/{STORE_ID}/authorization-models"]:
                field, values = ("stores", self.stores) if request.url.path == "/stores" else ("authorization_models", self.models)
                if not request.url.params["continuation_token"]:
                    return httpx.Response(200, json={field: [], "continuation_token": "next"})
                self.assertEqual(request.url.params["continuation_token"], "next")
                return httpx.Response(200, json={field: values, "continuation_token": ""})
            return self.server(request)

        self.handler = pages
        self.assertEqual(self.setup_store()["model_id"], MODEL_ID)
        self.assertEqual(len(self.requests), 4)
        self.assertTrue(all(r.method == "GET" for r in self.requests))

    def test_cyclic_pagination_and_invalid_lists_fail_without_writes(self):
        for response in [{"stores": [], "continuation_token": "repeat"}, {"stores": "invalid"}, {}]:
            with self.subTest(response=response):
                self.requests.clear()
                self.handler = lambda request: httpx.Response(200, json=response)
                with self.assertRaises(OpenFGAPolicyError):
                    self.setup_store()
                self.assertLessEqual(len(self.requests), 2)
                self.assertTrue(all(r.method == "GET" for r in self.requests))

    def test_environment_ids_take_precedence_and_cli_does_not_touch_dotenv(self):
        self.existing()
        dotenv = self.config_path.parent / ".env"
        dotenv.write_text(f"DO_NOT_TOUCH={SECRET}\n", encoding="utf-8")
        # 模拟本地旧 ID；环境变量指定另一套已存在的 store/model。
        self.config_path.write_text(json.dumps({"url": URL, "store_id": OTHER_STORE_ID, "model_id": OTHER_MODEL_ID}))
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {
            "OPENFGA_URL": URL, "OPENFGA_STORE_ID": STORE_ID, "OPENFGA_MODEL_ID": MODEL_ID,
            "OPENFGA_TOKEN": SECRET, "OPENFGA_CONFIG_PATH": str(self.config_path),
        }, clear=True), redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(main([]), 0)
        self.assertNotIn(SECRET, stdout.getvalue() + stderr.getvalue())
        self.assertEqual(dotenv.read_text(), f"DO_NOT_TOUCH={SECRET}\n")
        self.assertEqual(json.loads(self.config_path.read_text())["store_id"], STORE_ID)
        self.assertTrue(all(r.method == "GET" for r in self.requests))

    def test_corrupt_config_is_not_overwritten_or_echoed(self):
        self.config_path.write_text(SECRET, encoding="utf-8")
        stderr = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stderr(stderr):
            self.assertEqual(main(["--config", str(self.config_path)]), 1)
        self.assertNotIn(SECRET, stderr.getvalue())
        self.assertEqual(self.config_path.read_text(), SECRET)
        self.assertEqual(self.requests, [])

    def test_concurrent_setup_is_rejected_before_network(self):
        with open(str(self.config_path) + ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(OpenFGAPolicyError):
                self.setup_store()
        self.assertEqual(self.requests, [])

    def test_model_id_without_store_id_cannot_create_unrelated_store(self):
        with self.assertRaises(OpenFGAPolicyError):
            self.setup_store(model_id=MODEL_ID)
        self.assertEqual(self.requests, [])

    def test_config_cannot_overwrite_dotenv_even_when_it_contains_json(self):
        for name in [".env", ".env.production"]:
            path = self.config_path.parent / name
            path.write_text("{}", encoding="utf-8")
            with self.assertRaises(OpenFGAPolicyError):
                self.setup_store(config_path=path)
            self.assertEqual(path.read_text(), "{}")
        self.assertEqual(self.requests, [])


@unittest.skipUnless(os.environ.get("OPENFGA_INTEGRATION") == "1", "显式设置 OPENFGA_INTEGRATION=1 才访问本机 OpenFGA")
class OpenFGAIntegrationTests(unittest.TestCase):
    def policy(self):
        config_path = Path(os.environ.get("OPENFGA_CONFIG_PATH", DEFAULT_CONFIG_PATH))
        config = json.loads(config_path.read_text()) if config_path.exists() else {}
        return OpenFGAPolicy(
            url=os.environ.get("OPENFGA_URL") or config.get("url", URL),
            store_id=os.environ.get("OPENFGA_STORE_ID") or config.get("store_id", ""),
            model_id=os.environ.get("OPENFGA_MODEL_ID") or config.get("model_id", ""),
            token=os.environ.get("OPENFGA_TOKEN", ""),
        )

    def test_real_setup_reuses_model_and_keeps_ids(self):
        policy = self.policy()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "openfga.json"
            kwargs = {
                "url": policy._client.url,
                "store_id": policy.store_id,
                "config_path": path,
                "token": os.environ.get("OPENFGA_TOKEN", ""),
            }
            # 第一遍从服务端的模型列表复用；第二遍通过固定 ID 读取同一模型。
            first = setup_openfga(**kwargs)
            self.assertEqual(first["model_id"], policy.model_id)
            before = path.read_bytes()
            self.assertEqual(setup_openfga(**kwargs), first)
            self.assertEqual(path.read_bytes(), before)

    def test_real_grant_check_revoke_and_isolation(self):
        policy = self.policy()
        grant_id = f"integration-{uuid4().hex}"
        portfolios = [f"integration-{uuid4().hex}" for _ in range(2)]
        capabilities = ["portfolio:read", "portfolio:research-run"]
        try:
            self.assertFalse(policy.check(grant_id, "portfolio:read", portfolios[0]))
            policy.grant(grant_id, [portfolios[0]], capabilities)
            policy.grant(grant_id, [portfolios[0]], capabilities)
            for capability in capabilities:
                self.assertTrue(policy.check(grant_id, capability, portfolios[0]))
                self.assertFalse(policy.check(grant_id, capability, portfolios[1]))
                self.assertFalse(policy.check(f"other-{grant_id}", capability, portfolios[0]))
            policy.grant(grant_id, [portfolios[1]], ["portfolio:research-run"])
            self.assertTrue(policy.check(grant_id, "portfolio:research-run", portfolios[1]))
            self.assertFalse(policy.check(grant_id, "portfolio:read", portfolios[1]))
            policy.revoke(grant_id, [portfolios[0]], ["portfolio:research-run"])
            self.assertFalse(policy.check(grant_id, "portfolio:research-run", portfolios[0]))
            self.assertTrue(policy.check(grant_id, "portfolio:read", portfolios[0]))
            policy.revoke(grant_id, portfolios, capabilities)
            policy.revoke(grant_id, portfolios, capabilities)
            for portfolio in portfolios:
                for capability in capabilities:
                    self.assertFalse(policy.check(grant_id, capability, portfolio))
        finally:
            policy.revoke(grant_id, portfolios, capabilities)


if __name__ == "__main__":
    unittest.main()
