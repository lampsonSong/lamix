"""REPL 异步输入执行器。

让 agent 任务在 worker 线程执行，主线程保留输入框（prompt_toolkit patch_stdout
保护下，worker 的打印不破坏正在编辑的输入行），支持处理期间输入新消息打断当前任务。

设计约束：
- 不改 Session 内部逻辑（CLI 渠道仍走 _run_single），打断通过 agent.request_interrupt()
- AgentInterrupted 由 Session._run_single 兜底转为 HandleResult，本模块无需特判
- is_exit / is_new 等需要主线程处理的标志，通过实例属性传递，主循环消费
"""

from __future__ import annotations

import threading
from types import SimpleNamespace


class AsyncInputRunner:
    """管理 handle_input 的后台执行：提交、打断、等待、结果渲染。"""

    def __init__(self) -> None:
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()
        self.last_result = None
        self.last_error: Exception | None = None
        # 以下标志由 worker 设置、主循环消费
        self.pending_confirm = False   # agent 输出计划等待 y/n 确认
        self.needs_reset = False       # /new：主线程需 reset session
        self.exit_requested = False    # /exit：主线程需退出 REPL

    # ── 状态查询 ──────────────────────────────────────────────────────────

    def busy(self) -> bool:
        """是否有任务正在 worker 线程执行。"""
        return self._worker is not None and self._worker.is_alive()

    # ── 任务提交 ──────────────────────────────────────────────────────────

    def submit(self, session, user_input: str, render) -> bool:
        """提交一条用户输入到 worker 线程执行。

        Args:
            session: Agent session（需提供 handle_input）
            user_input: 用户输入文本
            render: 结果渲染回调 render(result)，在 worker 线程内调用
                    （调用方需保证其在 patch_stdout 下打印安全）

        Returns:
            True=已受理；False=已有任务在跑（应先 request_interrupt + wait_done）
        """
        if self.busy():
            return False

        def _work() -> None:
            try:
                result = session.handle_input(user_input)
                with self._lock:
                    self.last_result = result
                    self.last_error = None
                self._apply_flags(result)
                if render is not None:
                    render(result)
            except Exception as e:  # noqa: BLE001 - worker 兜底，不能让线程静默死掉
                with self._lock:
                    self.last_error = e
                try:
                    from src.cli_ui.styled import print_error
                    print_error(f"[worker] 任务异常: {e}")
                except Exception:
                    pass

        self._worker = threading.Thread(
            target=_work, daemon=True, name="cli-input-worker"
        )
        self._worker.start()
        return True

    def submit_confirm(self, session, render, after=None) -> bool:
        """提交「确认执行计划」到 worker 线程（confirm_and_execute 也是长任务）。

        Args:
            after: 计划执行完成后的回调（无参数），用于压缩等收尾逻辑。
        """
        if self.busy():
            return False

        def _work() -> None:
            try:
                exec_result = session.agent.confirm_and_execute()
                if exec_result:
                    pseudo = SimpleNamespace(
                        reply=exec_result, is_command=False,
                        is_exit=False, is_new=False, compaction_msg="",
                    )
                    if render is not None:
                        render(pseudo)
                if after is not None:
                    try:
                        after()
                    except Exception:
                        pass
            except Exception as e:  # noqa: BLE001
                try:
                    from src.cli_ui.styled import print_error
                    print_error(f"[worker] 计划执行异常: {e}")
                except Exception:
                    pass

        self._worker = threading.Thread(
            target=_work, daemon=True, name="cli-confirm-worker"
        )
        self._worker.start()
        return True

    # ── 打断 ──────────────────────────────────────────────────────────────

    def request_interrupt(self, session) -> None:
        """请求打断当前正在执行的任务（无任务时静默）。"""
        if not self.busy():
            return
        try:
            agent = getattr(session, "agent", None)
            if agent is not None and hasattr(agent, "request_interrupt"):
                agent.request_interrupt()
        except Exception:
            pass

    def wait_done(self, timeout: float = 30.0) -> bool:
        """等待 worker 结束。True=已结束；False=超时仍在跑（线程不强杀）。"""
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=timeout)
        return not self.busy()

    # ── 内部 ──────────────────────────────────────────────────────────────

    def _apply_flags(self, result) -> None:
        """从 HandleResult 提取需要主线程处理的标志。"""
        if result is None:
            return
        if getattr(result, "is_exit", False):
            self.exit_requested = True
        if getattr(result, "is_new", False):
            self.needs_reset = True
        reply = getattr(result, "reply", "") or ""
        if isinstance(reply, str) and "请确认是否执行此计划" in reply:
            self.pending_confirm = True
