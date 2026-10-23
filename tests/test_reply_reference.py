"""验证入站引用仅传递消息 ID，不提前取回或下载原消息。"""

from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

import sys
import unittest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT.parent))
NapCatInboundCodec = import_module(f"{PLUGIN_ROOT.name}.codecs.inbound.message_codec").NapCatInboundCodec
NapCatAdapterPlugin = import_module(f"{PLUGIN_ROOT.name}.plugin").NapCatAdapterPlugin
NapCatActionError = import_module(f"{PLUGIN_ROOT.name}.services.action_service").NapCatActionError
NapCatActionService = import_module(f"{PLUGIN_ROOT.name}.services.action_service").NapCatActionService
NapCatQueryService = import_module(f"{PLUGIN_ROOT.name}.services.query_service").NapCatQueryService


class ReplyReferenceTests(IsolatedAsyncioTestCase):
    async def test_inbound_reply_does_not_fetch_or_download_original_message(self) -> None:
        query_service = Mock()
        query_service.get_message_detail = AsyncMock(
            return_value={"message": [{"type": "image", "data": {"url": "https://example.com/quoted.png"}}]}
        )
        query_service.download_binary = AsyncMock(return_value=b"quoted-image")
        codec = NapCatInboundCodec(Mock(), query_service)
        segments, is_at = await codec.convert_segments(
            {
                "message": [
                    {"type": "reply", "data": {"id": 12345}},
                    {"type": "text", "data": {"text": "回复正文"}},
                ]
            },
            "67890",
        )

        self.assertEqual(
            segments,
            [
                {"type": "reply", "data": {"target_message_id": "12345"}},
                {"type": "text", "data": "回复正文"},
            ],
        )
        self.assertFalse(is_at)
        query_service.get_message_detail.assert_not_awaited()
        query_service.download_binary.assert_not_awaited()

    async def test_standard_message_api_preserves_media_and_nested_reply_id(self) -> None:
        query_service = Mock()
        query_service.call_action_data = AsyncMock(
            return_value={
                "message_id": -12345,
                "message_type": "group",
                "group_id": 10001,
                "time": 1780000000,
                "sender": {"user_id": 10002, "nickname": "原消息发送者"},
                "message": [
                    {"type": "reply", "data": {"id": 12344}},
                    {"type": "image", "data": {"url": "https://example.com/quoted.png"}},
                ],
            }
        )
        query_service.download_binary = AsyncMock(return_value=b"quoted-image")
        runtime = SimpleNamespace(
            query_service=query_service,
            inbound_codec=NapCatInboundCodec(Mock(), query_service),
            runtime_state=SimpleNamespace(account_id="10003"),
        )
        plugin = NapCatAdapterPlugin()
        with patch.object(plugin, "_require_runtime_bundle", return_value=runtime):
            message = await plugin.api_get_message("-12345")

        self.assertEqual(message["message_id"], "-12345")
        self.assertEqual(message["platform"], "qq")
        self.assertEqual(message["message_info"]["group_info"]["group_id"], "10001")
        self.assertEqual(message["message_info"]["additional_config"]["self_id"], "10003")
        self.assertEqual(message["raw_message"][0], {"type": "reply", "data": {"target_message_id": "12344"}})
        self.assertEqual(message["raw_message"][1]["type"], "image")
        self.assertEqual(message["raw_message"][1]["binary_data_base64"], "cXVvdGVkLWltYWdl")
        query_service.call_action_data.assert_awaited_once_with("get_msg", {"message_id": "-12345"})
        query_service.download_binary.assert_awaited_once_with("https://example.com/quoted.png")

    async def test_standard_message_api_checks_private_target_and_unavailable_message(self) -> None:
        detail = {
            "message_id": 12345,
            "message_type": "private",
            "time": 1780000000,
            "self_id": 10003,
            "sender": {"user_id": 10003, "nickname": "机器人"},
            "message": [{"type": "text", "data": {"text": "之前的回复"}}],
        }
        query_service = Mock()
        query_service.call_action_data = AsyncMock(return_value=detail)
        runtime = SimpleNamespace(
            query_service=query_service,
            inbound_codec=NapCatInboundCodec(Mock(), query_service),
            runtime_state=SimpleNamespace(account_id="10003"),
        )
        plugin = NapCatAdapterPlugin()
        with patch.object(plugin, "_require_runtime_bundle", return_value=runtime):
            with self.assertRaisesRegex(ValueError, "缺少目标用户 ID"):
                await plugin.api_get_message("12345")
            detail["target_id"] = 10002
            message = await plugin.api_get_message("12345")
            self.assertEqual(message["message_info"]["user_info"]["user_id"], "10003")
            self.assertEqual(message["message_info"]["additional_config"]["platform_io_target_user_id"], "10002")

            query_service.call_action_data.side_effect = NapCatActionError("get_msg", "消息不存在")
            self.assertIsNone(await plugin.api_get_message("12345"))

    async def test_standard_message_api_propagates_transport_and_response_errors(self) -> None:
        transport = Mock(call_action=AsyncMock())
        query_service = NapCatQueryService(NapCatActionService(Mock(), transport), Mock())
        runtime = SimpleNamespace(query_service=query_service)
        plugin = NapCatAdapterPlugin()
        with patch.object(plugin, "_require_runtime_bundle", return_value=runtime):
            for error in [RuntimeError("连接已断开"), TimeoutError("查询超时")]:
                with self.subTest(error=error):
                    transport.call_action.side_effect = error
                    with self.assertRaisesRegex(RuntimeError, str(error)):
                        await plugin.api_get_message("12345")

            transport.call_action.side_effect = None
            transport.call_action.return_value = {"status": "failed", "wording": "消息不存在或已被撤回: PromiseTimer 超时"}
            with self.assertRaisesRegex(NapCatActionError, "PromiseTimer"):
                await plugin.api_get_message("12345")
            for malformed_detail in [None, [], "无效数据"]:
                with self.subTest(detail=malformed_detail):
                    transport.call_action.return_value = {"status": "ok", "data": malformed_detail}
                    with self.assertRaisesRegex(ValueError, "必须是对象"):
                        await plugin.api_get_message("12345")
            transport.call_action.return_value = {"status": "failed", "wording": "消息不存在或已被撤回"}
            self.assertIsNone(await plugin.api_get_message("12345"))


if __name__ == "__main__":
    unittest.main()
