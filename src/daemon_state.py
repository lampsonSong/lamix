"""daemon 相关模块共享的可变状态。

只放跨模块共享的对象，避免循环 import。
"""

from __future__ import annotations

import threading

_shutdown = threading.Event()
