import asyncio
import importlib.util
import json
import unittest

from mmar import Limits, ModelReply, StepSnapshot
from mmar.protocol import MmarError, TransientModelError


@unittest.skipUnless(importlib.util.find_spec("httpx"), "Install .[api] to test HTTP transport")
class HttpModelTests(unittest.IsolatedAsyncioTestCase):
    def snapshot(self):
        return StepSnapshot("step", "a", "s", "identity", "# Memory",
                            {"peers": ["b"], "groups": []}, ())

    async def test_request_contract_single_call_no_secrets_in_context(self):
        import httpx
        from mmar.http_model import ChatCompletionsModel
        calls = []
        def handler(request):
            calls.append(request)
            body = json.loads(request.content)
            self.assertNotIn("test-secret", json.dumps(body))
            self.assertEqual(body["max_completion_tokens"], 512)
            self.assertEqual(body["response_format"], {"type": "json_object"})
            self.assertEqual(request.headers["Authorization"], "Bearer test-secret")
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"actions":[],"memory_replace":null}'}}], "usage": {"total_tokens": 12}})
        model = ChatCompletionsModel(base_url="https://example.test/v1", model="test",
            api_key="test-secret", max_output_tokens=512, transport=httpx.MockTransport(handler))
        try:
            reply = await model.complete(self.snapshot())
            self.assertEqual(reply.usage["total_tokens"], 12)
            self.assertEqual(len(calls), 1)
            self.assertEqual(str(calls[0].url), "https://example.test/v1/chat/completions")
        finally:
            await model.aclose()

    async def test_rate_limit_is_returned_without_hidden_retry(self):
        import httpx
        from mmar.http_model import ChatCompletionsModel
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(429, text="untrusted response including a secret")
        model = ChatCompletionsModel(base_url="https://example.test/v1", model="test",
            api_key="key", transport=httpx.MockTransport(handler))
        try:
            with self.assertRaisesRegex(TransientModelError, "provider_temporary_error"):
                await model.complete(self.snapshot())
            self.assertEqual(len(calls), 1)
        finally:
            await model.aclose()

    async def test_reject_insecure_remote_endpoint(self):
        from mmar.http_model import ChatCompletionsModel
        with self.assertRaises(ValueError):
            ChatCompletionsModel(base_url="http://remote.test/v1", model="test", api_key="key")

    async def test_bounded_response(self):
        import httpx
        from mmar.http_model import ChatCompletionsModel
        model = ChatCompletionsModel(base_url="https://example.test/v1", model="test",
            api_key="key", max_response_bytes=64,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 1024)))
        try:
            with self.assertRaisesRegex(MmarError, "provider_response_too_large"):
                await model.complete(self.snapshot())
        finally:
            await model.aclose()

    async def test_transport_cancellation_propagates(self):
        import httpx
        from mmar.http_model import ChatCompletionsModel
        async def handler(_):
            await asyncio.sleep(10)
        model = ChatCompletionsModel(base_url="https://example.test/v1", model="test",
            api_key="key", transport=httpx.MockTransport(handler))
        try:
            task = asyncio.create_task(model.complete(self.snapshot()))
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            await model.aclose()
