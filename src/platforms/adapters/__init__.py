"""平台适配器集合。FeishuAdapter 惰性加载，避免未安装 lark-oapi 时导入失败。"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.platforms.adapters.feishu import FeishuAdapter

__all__ = ["FeishuAdapter"]


def __getattr__(name: str):
    if name == "FeishuAdapter":
        from src.platforms.adapters.feishu import FeishuAdapter
        return FeishuAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
