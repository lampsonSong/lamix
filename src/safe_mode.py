#!/usr/bin/env python3
"""
safe_mode.py — Lamix 安全恢复入口

永远不被自学习模块修改的最小化执行通道：
- 对话（LLM）
- 命令执行（shell）
- Recovery（恢复 skills/memory）
- /exit 退出并重启主程序

配置读取 ~/.lamix/config.yaml，不依赖主程序。
"""

import json
import os
import subprocess

from src.core.config import DEFAULT_CONFIG
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

# 路径常量
LAMIX_DIR = Path.home() / ".lamix"
CONFIG_PATH = LAMIX_DIR / "config.yaml"
BACKUP_DIR = LAMIX_DIR / "backups"
LAMIX_ROOT = Path(__file__).resolve().parent.parent  # ~/lamix

DAEMON_LOG = LAMIX_DIR / "logs" / "daemon.log"
DAEMON_ERR_LOG = LAMIX_DIR / "logs" / "daemon.err.log"

# 需要恢复的关键目录（排除 venv/logs 等）
CRITICAL_DIRS = ["memory"]


def _resolve_daemon_cmd() -> list[str]:
    """解析 daemon 启动命令。

    frozen 环境：走内部子命令 ``lamix gateway daemon-run``（不能用 ``-m``）。
    源码环境：优先走 sysconfig 里的 lamix 脚本 + ``gateway daemon-run``，
    找不到则回退 ``python -m src.daemon``。
    """
    from src.core.process_launch import daemon_launch_cmd, is_frozen

    if is_frozen():
        return daemon_launch_cmd()

    import shutil
    import sysconfig

    lamix = shutil.which("lamix")
    if lamix:
        return [lamix, "gateway", "daemon-run"]

    scripts = sysconfig.get_path("scripts")
    for name in ["lamix", "lamix.exe", "lamix.bat", "lamix-script.py"]:
        path = os.path.join(scripts, name)
        if os.path.exists(path):
            return [path, "gateway", "daemon-run"]

    return daemon_launch_cmd()

# ─── 配置读取 ────────────────────────────────────────────────────────────────


def load_config() -> dict:
    """读取 config.yaml，不依赖 yaml 库时用 json 备用（极简配置格式）。"""
    if not CONFIG_PATH.exists():
        print(f"[safe_mode] 配置文件不存在: {CONFIG_PATH}")
        return {}

    try:
        import yaml
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        # yaml 不可用时，尝试解析简化的 JSON 格式
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                content = f.read()
            # 简单提取 app_id 和 app_secret（避免完整 YAML 解析）
            app_id = ""
            app_secret = ""
            for line in content.split("\n"):
                if line.strip().startswith("app_id:"):
                    app_id = line.split("app_id:")[1].strip().strip('"').strip("'")
                elif line.strip().startswith("app_secret:"):
                    app_secret = line.split("app_secret:")[1].strip().strip('"').strip("'")
            return {"feishu": {"app_id": app_id, "app_secret": app_secret}} if app_id else {}
        except Exception as e:
            print(f"[safe_mode] 配置读取失败: {e}")
            return {}


# ─── Recovery ────────────────────────────────────────────────────────────────


def list_backups() -> list:
    """列出所有备份。"""
    BACKUP_DIR.mkdir(exist_ok=True)
    backups = sorted([p.name for p in BACKUP_DIR.glob("backup-*.tar.gz")], reverse=True)
    return backups


def create_backup() -> str:
    """创建当前关键目录的备份。"""
    BACKUP_DIR.mkdir(exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    name = f"backup-{ts}.tar.gz"
    path = BACKUP_DIR / name

    with tarfile.open(path, "w:gz") as tar:
        for dir_name in CRITICAL_DIRS:
            dir_path = LAMIX_DIR / dir_name
            if dir_path.exists():
                tar.add(dir_path, arcname=dir_name)

    print(f"[safe_mode] 备份已创建: {path}")
    return name


def restore_backup(name: str) -> bool:
    """恢复指定备份，覆盖当前关键目录。"""
    path = BACKUP_DIR / name
    if not path.exists():
        print(f"[safe_mode] 备份不存在: {name}")
        return False

    # 先备份当前状态
    create_backup()

    # 提取覆盖
    with tarfile.open(path, "r:gz") as tar:
        for member in tar.getmembers():
            # 安全检查：只提取 critical dirs 内的内容
            if member.name.split("/")[0] in CRITICAL_DIRS:
                tar.extract(member, LAMIX_DIR)

    print(f"[safe_mode] 已恢复到: {name}")
    return True


# ─── 命令执行 ────────────────────────────────────────────────────────────────


def execute_command(cmd: str) -> str:
    """执行 shell 命令，返回输出。"""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = result.stdout or result.stderr or ""
        if result.returncode != 0 and not output:
            output = f"[命令执行失败，退出码: {result.returncode}]"
        return output[:2000]  # 限制输出长度
    except subprocess.TimeoutExpired:
        return "[命令超时，30秒]"
    except Exception as e:
        return f"[执行错误: {e}]"


# ─── 对话处理 ────────────────────────────────────────────────────────────────


def process_chat(text: str, llm_config: dict) -> str:
    """简单的 LLM 对话（不依赖主程序）。"""
    try:
        from openai import OpenAI
    except ImportError:
        return "❌ OpenAI SDK 未安装。请先安装: pip install openai"

    api_key = llm_config.get("api_key", "")
    base_url = llm_config.get("base_url", DEFAULT_CONFIG["llm"]["base_url"])
    model = llm_config.get("model", DEFAULT_CONFIG["llm"]["model"])

    if not base_url:
        return "❌ LLM 未配置 base_url"

    try:
        client = OpenAI(api_key=api_key or "dummy", base_url=base_url)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "你是 Lamix 的安全模式，只能执行基础命令和 recovery 操作。"},
                {"role": "user", "content": text},
            ],
            max_tokens=500,
            timeout=30,
        )
        return response.choices[0].message.content or "（空回复）"
    except Exception as e:
        return f"❌ 对话失败: {e}"


# ─── 消息处理 ────────────────────────────────────────────────────────────────


def process_message(text: str, config: dict) -> Tuple[Optional[str], bool]:
    """处理消息，返回 (reply, should_exit)。

    - 返回 reply 表示需要发送给用户的回复
    - should_exit=True 表示需要退出 safe_mode
    """
    text = text.strip()

    # /exit 退出并重启主程序
    if text.lower() == "/exit":
        return "正在退出 Safe Mode，重启主程序...", True

    # Recovery 命令
    cmd = text.strip().lower()

    if cmd == "/recovery":
        backups = list_backups()
        if not backups:
            reply = "📦 没有可用备份。\n\n可用命令：\n- /backup: 创建当前状态备份\n- /recovery list: 查看备份列表\n- /recovery restore <name>: 恢复到指定备份\n- /exit: 退出并重启主程序"
        else:
            lines = ["📦 可用备份列表："]
            for b in backups[:10]:
                lines.append(f"  - {b}")
            if len(backups) > 10:
                lines.append(f"  ...共 {len(backups)} 个")
            lines.append("\n命令：\n- /backup: 创建当前备份\n- /recovery restore <name>\n- /exit: 退出重启主程序")
            reply = "\n".join(lines)
        return reply, False

    if cmd == "/backup":
        name = create_backup()
        return f"✅ 备份已创建: {name}", False

    if cmd.startswith("/recovery restore "):
        name = cmd.replace("/recovery restore ", "").strip()
        if not name.endswith(".tar.gz"):
            name += ".tar.gz"
        if restore_backup(name):
            return f"✅ 已恢复到: {name}", False
        return f"❌ 恢复失败: {name} 不存在", False

    if cmd == "/recovery list":
        backups = list_backups()
        if not backups:
            return "📦 没有可用备份", False
        lines = ["📦 备份列表："]
        for b in backups[:10]:
            lines.append(f"  - {b}")
        return "\n".join(lines), False

    # Shell 命令
    if text.startswith("/sh "):
        shell_cmd = text[4:].strip()
        if not shell_cmd:
            return "用法: /sh <command>\n例如: /sh ls -la ~/.lamix", False
        dangerous = ["rm -rf", "dd", "mkfs", ":(){:|:&};:", "curl | sh", "wget -O- | sh"]
        for d in dangerous:
            if d in shell_cmd:
                return f"⚠️ 拒绝执行危险命令: {d}", False
        return execute_command(shell_cmd), False

    # LLM 对话
    llm_config = config.get("llm", {})
    return process_chat(text, llm_config), False


# ─── 飞书消息处理 ────────────────────────────────────────────────────────────


def send_feishu_message(chat_id: str, text: str, config: dict) -> None:
    """发送飞书消息。"""
    try:
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
    except ImportError:
        print("[safe_mode] lark_oapi 未安装")
        return

    feishu_config = config.get("feishu", {})
    app_id = feishu_config.get("app_id", "")
    app_secret = feishu_config.get("app_secret", "")

    if not app_id or not app_secret:
        print("[safe_mode] 飞书配置不完整")
        return

    try:
        client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .build()
            )
            .build()
        )
        resp = client.im.v1.message.create(request)
        if resp.success():
            print(f"[safe_mode] 回复已发送", flush=True)
        else:
            print(f"[safe_mode] 发送失败: {resp.code} {resp.msg}", flush=True)
    except Exception as e:
        print(f"[safe_mode] 发送消息异常: {e}", flush=True)


def handle_feishu_message(data, config: dict) -> Tuple[bool, Optional[str]]:
    """处理飞书消息事件，返回 (should_exit, chat_id)。

    - should_exit=True 表示需要退出 safe_mode
    - chat_id 用于退出后发送恢复通知
    """
    try:
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
    except ImportError:
        print("[safe_mode] lark_oapi 未安装")
        return False, None

    sender = data.event.sender
    message = data.event.message

    # 跳过机器人自己的消息
    sender_type = getattr(sender, "sender_type", None)
    if sender_type == "app":
        return False, None

    open_id = sender.sender_id.open_id if sender.sender_id else "unknown"
    chat_id = message.chat_id

    try:
        content_obj = json.loads(message.content)
        text = content_obj.get("text", "").strip()
    except (json.JSONDecodeError, AttributeError):
        text = message.content or ""

    if not text:
        return False, None

    print(f"[safe_mode] 收到消息: {text[:100]}", flush=True)

    reply, should_exit = process_message(text, config)

    if reply:
        send_feishu_message(chat_id, reply, config)

    return should_exit, chat_id


# ─── 飞书 WebSocket 长连接 ──────────────────────────────────────────────────


def start_feishu_listener(config: dict) -> Tuple:
    """启动飞书 WebSocket 长连接，返回 (should_exit_event, ws_client, last_chat_id_holder)。"""
    try:
        import lark_oapi as lark
    except ImportError:
        print("[safe_mode] lark_oapi 未安装，请运行: pip install lark-oapi")
        return None, None, None

    feishu_config = config.get("feishu", {})
    app_id = feishu_config.get("app_id", "")
    app_secret = feishu_config.get("app_secret", "")

    if not app_id or not app_secret:
        print("[safe_mode] 飞书 app_id 或 app_secret 未配置")
        return None, None, None

    should_exit = threading.Event()
    last_chat_id = [None]  # 用 list 包装以便在闭包中修改

    def handler(data):
        """事件处理函数。"""
        exit_flag, chat_id = handle_feishu_message(data, config)
        if chat_id:
            last_chat_id[0] = chat_id
        if exit_flag:
            should_exit.set()

    ws_client = lark.ws.Client(
        app_id,
        app_secret,
        event_handler=lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(handler).build(),
        log_level=lark.LogLevel.INFO,
    )

    print(f"[safe_mode] 启动飞书监听，app_id={app_id}", flush=True)
    ws_client.start()

    return should_exit, ws_client, last_chat_id


# ─── 重启主程序 ─────────────────────────────────────────────────────────────


def restart_daemon() -> None:
    """重启 Lamix daemon。"""
    print("[safe_mode] 重启 Lamix daemon...", flush=True)
    LAMIX_DIR.mkdir(parents=True, exist_ok=True)
    try:
        # 启动 daemon（不等待）
        subprocess.Popen(
            _resolve_daemon_cmd(),
            cwd=str(LAMIX_ROOT),
            stdout=open(DAEMON_LOG, "a", encoding="utf-8"),
            stderr=open(DAEMON_ERR_LOG, "a", encoding="utf-8"),
        )
        print("[safe_mode] Daemon 已启动", flush=True)
    except Exception as e:
        print(f"[safe_mode] Daemon 启动失败: {e}", flush=True)


# ─── 主入口 ──────────────────────────────────────────────────────────────────


def main() -> None:
    """主入口。"""
    print("=" * 50)
    print("Lamix Safe Mode")
    print("=" * 50)
    print("可用命令：")
    print("  /recovery       - 查看恢复选项")
    print("  /backup         - 创建当前状态备份")
    print("  /recovery list  - 查看备份列表")
    print("  /recovery restore <name> - 恢复指定备份")
    print("  /sh <command>   - 执行 shell 命令")
    print("  /exit           - 退出并重启主程序")
    print("  其他消息        - LLM 对话")
    print("=" * 50)

    config = load_config()

    # 启动飞书监听
    feishu_config = config.get("feishu", {})
    last_chat_id = None
    if feishu_config.get("app_id") and feishu_config.get("app_secret"):
        should_exit, ws_client, chat_id_holder = start_feishu_listener(config)
        if ws_client is None:
            print("[safe_mode] 飞书监听启动失败，仅支持本地运行")
            should_exit = None
        else:
            last_chat_id = chat_id_holder
    else:
        print("[safe_mode] 飞书未配置，仅支持本地运行")
        should_exit = None

    print("[safe_mode] Safe Mode 运行中...", flush=True)

    # 等待退出信号
    try:
        if should_exit:
            while not should_exit.is_set():
                should_exit.wait(timeout=1)
        else:
            # 无飞书时，本地轮询
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        print("\n[safe_mode] 收到退出信号")

    print("[safe_mode] 退出 Safe Mode")

    # 重启 daemon
    restart_daemon()

    # 等待 daemon 启动，然后发送恢复通知
    if last_chat_id and last_chat_id[0]:
        time.sleep(5)  # 等待 daemon 完全启动
        print(f"[safe_mode] 发送恢复通知到 chat_id={last_chat_id[0]}", flush=True)
        send_feishu_message(last_chat_id[0], "✅ Safe Mode 已退出，主程序已恢复。", config)


if __name__ == "__main__":
    main()
