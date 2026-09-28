"""测试 FeishuAdapter - 飞书平台适配器"""
import pytest
from unittest.mock import Mock, MagicMock, patch, PropertyMock
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestMessageDeduplicator:
    """消息去重器测试"""

    def test_first_message_not_duplicate(self):
        """测试首次消息不是重复"""
        from src.platforms.adapters.feishu import MessageDeduplicator
        
        dedup = MessageDeduplicator()
        assert dedup.is_duplicate("msg1") is False

    def test_duplicate_message_detected(self):
        """测试重复消息被检测"""
        from src.platforms.adapters.feishu import MessageDeduplicator
        
        dedup = MessageDeduplicator()
        dedup.mark_processed("msg1")
        
        assert dedup.is_duplicate("msg1") is True
        assert dedup.is_duplicate("msg2") is False

    def test_ttl_expiration(self):
        """测试 TTL 过期"""
        from src.platforms.adapters.feishu import MessageDeduplicator
        
        # 使用极短的 TTL
        dedup = MessageDeduplicator(ttl_seconds=0)
        dedup.mark_processed("msg1")
        
        # 短暂等待后应该过期
        import time
        time.sleep(0.1)
        
        assert dedup.is_duplicate("msg1") is False

    def test_max_size_eviction(self):
        """测试容量上限驱逐"""
        from src.platforms.adapters.feishu import MessageDeduplicator
        
        dedup = MessageDeduplicator(max_size=3)
        
        # 添加 3 个消息
        for i in range(3):
            dedup.mark_processed(f"msg{i}")
        
        # 前 3 个应该都不重复
        for i in range(3):
            assert dedup.is_duplicate(f"msg{i}") is True
        
        # 添加第 4 个，最老的应该被驱逐
        dedup.mark_processed("msg3")
        assert dedup.is_duplicate("msg0") is False
        assert dedup.is_duplicate("msg3") is True


class TestFeishuAdapter:
    """FeishuAdapter 测试"""

    def test_init_requires_config(self):
        """测试初始化需要配置"""
        from src.platforms.adapters.feishu import FeishuAdapter
        
        config = {"app_id": "test_id", "app_secret": "test_secret"}
        adapter = FeishuAdapter(config)
        
        assert adapter.app_id == "test_id"
        assert adapter.app_secret == "test_secret"
        assert adapter._stopped is False

    def test_init_missing_app_id(self):
        """测试缺少 app_id 抛出异常"""
        from src.platforms.adapters.feishu import FeishuAdapter
        
        with pytest.raises(KeyError):
            FeishuAdapter({"app_secret": "test"})

    def test_init_missing_app_secret(self):
        """测试缺少 app_secret 抛出异常"""
        from src.platforms.adapters.feishu import FeishuAdapter
        
        with pytest.raises(KeyError):
            FeishuAdapter({"app_id": "test_id"})

    def test_platform_name(self):
        """测试平台名称"""
        from src.platforms.adapters.feishu import FeishuAdapter
        
        adapter = FeishuAdapter({"app_id": "id", "app_secret": "secret"})
        assert adapter.platform == "feishu"

    def test_stopped_flag(self):
        """测试停止标志"""
        from src.platforms.adapters.feishu import FeishuAdapter
        
        adapter = FeishuAdapter({"app_id": "id", "app_secret": "secret"})
        
        assert adapter._stopped is False
        adapter._stopped = True
        assert adapter._stopped is True

    def test_strip_think_tags(self):
        """测试移除 think 标签"""
        from src.platforms.adapters.feishu import FeishuAdapter
        
        adapter = FeishuAdapter({"app_id": "id", "app_secret": "secret"})
        
        # 测试标准格式
        text = "<think> some thought</think> result"
        assert adapter._strip_think_tags(text) == "result"
        
        # 测试多行格式
        text2 = "<think>\nthought\n</think> result2"
        assert adapter._strip_think_tags(text2) == "result2"
        
        # 测试无标签
        text3 = "plain text"
        assert adapter._strip_think_tags(text3) == "plain text"

    def test_should_use_card(self):
        """测试判断是否使用卡片"""
        from src.platforms.adapters.feishu import FeishuAdapter
        
        # 根据实际实现，检查的是表格分隔线格式
        assert FeishuAdapter._should_use_card("|---|") is True
        assert FeishuAdapter._should_use_card("| --- |") is True
        assert FeishuAdapter._should_use_card("| col1 | col2 |\n|---|") is True
        assert FeishuAdapter._should_use_card("plain text") is False
        assert FeishuAdapter._should_use_card("hello world") is False


class TestSendWithRetry:
    """_send_with_retry 重试逻辑测试：传输错误重试 / 业务错误不重试"""

    def _adapter(self):
        from src.platforms.adapters.feishu import FeishuAdapter
        # _send_with_retry 不依赖实例状态，绕过需要 config 的 __init__
        return object.__new__(FeishuAdapter)

    def test_transport_error_retries_then_none(self):
        """传输错误（ConnectionResetError）重试 max_retries 次后返回 None"""
        from unittest.mock import patch
        adapter = self._adapter()
        calls = []

        def flaky():
            calls.append(1)
            raise ConnectionResetError(54, "Connection reset by peer")

        with patch("src.platforms.adapters.feishu.time.sleep") as mock_sleep:
            result = adapter._send_with_retry(flaky, max_retries=2, retry_interval=0)

        assert result is None
        assert len(calls) == 3  # 1 次初始 + 2 次重试
        assert mock_sleep.call_count == 2

    def test_api_error_no_retry(self):
        """FeishuAPIError 业务错误立即抛出，不 sleep 不重试"""
        from unittest.mock import patch
        from src.feishu.client import FeishuAPIError
        adapter = self._adapter()
        calls = []

        def biz_error():
            calls.append(1)
            raise FeishuAPIError("feishu api error: code=230002 msg=Bot/User can NOT be out of the chat")

        with patch("src.platforms.adapters.feishu.time.sleep") as mock_sleep:
            with pytest.raises(FeishuAPIError):
                adapter._send_with_retry(biz_error, max_retries=2, retry_interval=0)

        assert len(calls) == 1  # 只调用 1 次，未重试
        assert mock_sleep.call_count == 0

    def test_api_error_not_caught_by_oserror_branch(self):
        """FeishuAPIError 不是 OSError 子类（防止回归为可重试异常）"""
        from src.feishu.client import FeishuAPIError
        assert not issubclass(FeishuAPIError, OSError)
        assert issubclass(FeishuAPIError, RuntimeError)  # 兼容既有 except RuntimeError

    def test_success_passthrough(self):
        """正常返回原样透传，不 sleep"""
        from unittest.mock import patch
        adapter = self._adapter()

        with patch("src.platforms.adapters.feishu.time.sleep") as mock_sleep:
            result = adapter._send_with_retry(lambda: "msg_id_123")

        assert result == "msg_id_123"
        assert mock_sleep.call_count == 0
