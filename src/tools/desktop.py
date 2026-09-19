"""
桌面控制工具：截图、鼠标操作、键盘操作、Accessibility 元素查询。

依赖：pyautogui, Pillow
macOS 需要在系统设置中开启 Accessibility 权限（用于控制鼠标键盘）
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import uuid
from io import BytesIO
from typing import Any, Optional

from PIL import Image

import pyautogui  # 可选：截图和鼠标键盘控制

# pyautogui 安全设置：失败时停止
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.05  # 每次操作后暂停 50ms，避免太快


# ─── 截图 ────────────────────────────────────────────────────────────────

def take_screenshot() -> str:
    """截取全屏，保存为 PNG 文件并返回路径和尺寸信息。"""
    save_dir = os.path.expanduser("~/.lamix/screenshots")
    os.makedirs(save_dir, exist_ok=True)
    filename = f"screenshot_{uuid.uuid4().hex[:8]}.png"
    filepath = os.path.join(save_dir, filename)
    # macOS TCC：pyautogui.screenshot() 在后台进程会截到黑屏，
    # 改用 Apple 自带的 screencapture（签名健全，可正常授权）
    proc = subprocess.run(
        ["/usr/sbin/screencapture", "-x", filepath],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"screencapture failed: {proc.stderr.strip()}")
    img = Image.open(filepath)
    w, h = img.size
    file_size = os.path.getsize(filepath)
    return f"截图已保存到 {filepath}（{w}x{h}，{file_size // 1024}KB）"


def take_screenshot_region(x: int, y: int, width: int, height: int) -> str:
    """截取屏幕指定区域，保存为 PNG 文件并返回路径和尺寸信息。

    注：screencapture 的 -R 参数在部分 macOS 版本上存在 bug
    （could not create image from display with rect），
    因此改为全屏截图后用 PIL 裁剪，结果一致且更可靠。
    """
    save_dir = os.path.expanduser("~/.lamix/screenshots")
    os.makedirs(save_dir, exist_ok=True)
    filename = f"screenshot_region_{uuid.uuid4().hex[:8]}.png"
    filepath = os.path.join(save_dir, filename)
    proc = subprocess.run(
        ["/usr/sbin/screencapture", "-x", filepath],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"screencapture failed: {proc.stderr.strip()}")
    with Image.open(filepath) as img:
        cropped = img.crop((x, y, x + width, y + height))
    cropped.save(filepath, format="PNG")
    w, h = cropped.size
    file_size = os.path.getsize(filepath)
    return f"区域截图已保存到 {filepath}（{w}x{h}，{file_size // 1024}KB）"



# ─── 鼠标操作 ────────────────────────────────────────────────────────────

def move_to(x: int, y: int, duration: float = 0.2) -> str:
    """移动鼠标到 (x, y)。"""
    pyautogui.moveTo(x, y, duration=duration)
    return f"鼠标已移动到 ({x}, {y})"


def click(x: int, y: int, button: str = "left", clicks: int = 1) -> str:
    """在 (x, y) 处点击。button: left/right/middle。"""
    pyautogui.click(x, y, clicks=clicks, button=button)
    return f"已在 ({x}, {y}) 点击 {clicks} 次 {button} 键"


def double_click(x: int, y: int) -> str:
    """双击 (x, y)。"""
    pyautogui.doubleClick(x, y)
    return f"已在 ({x}, {y}) 双击"


def right_click(x: int, y: int) -> str:
    """右键单击 (x, y)。"""
    pyautogui.rightClick(x, y)
    return f"已在 ({x}, {y}) 右键单击"


def scroll(clicks: int, x: Optional[int] = None, y: Optional[int] = None) -> str:
    """滚动鼠标。clicks > 0 向上，< 0 向下。"""
    if x is not None and y is not None:
        pyautogui.scroll(clicks, x=x, y=y)
        return f"已在 ({x}, {y}) 处滚动 {clicks} 格"
    pyautogui.scroll(clicks)
    return f"已滚动 {clicks} 格"


def drag(start_x: int, start_y: int, end_x: int, end_y: int,
         duration: float = 0.5, button: str = "left") -> str:
    """从 (start_x, start_y) 拖拽到 (end_x, end_y)。"""
    pyautogui.moveTo(start_x, start_y)
    pyautogui.drag(end_x - start_x, end_y - start_y,
                   duration=duration, button=button)
    return f"已从 ({start_x}, {start_y}) 拖拽到 ({end_x}, {end_y})"


# ─── 键盘操作 ────────────────────────────────────────────────────────────

def type_text(text: str, interval: float = 0.0) -> str:
    """输入文本。"""
    pyautogui.write(text, interval=interval)
    return f"已输入文本：{text[:50]}{'...' if len(text) > 50 else ''}"


def press_key(key: str) -> str:
    """按下单个按键，如 'enter', 'esc', 'cmd', 'space', 'tab', 'delete'。"""
    pyautogui.press(key)
    return f"已按键：{key}"


def key_down(key: str) -> str:
    """按住按键不松手。"""
    pyautogui.keyDown(key)
    return f"已按下：{key}"


def key_up(key: str) -> str:
    """释放按键。"""
    pyautogui.keyUp(key)
    return f"已释放：{key}"


def hotkey(*keys: str) -> str:
    """组合键，如 hotkey('cmd', 'c') 为 Cmd+C 复制。"""
    pyautogui.hotkey(*keys)
    return f"已按组合键：{'+'.join(keys)}"


# ─── Accessibility ───────────────────────────────────────────────────────

def query_ui_element(app_name: str, element_role: str = "",
                     element_title: str = "") -> str:
    """查询应用中的 UI 元素（跨平台）。

    macOS: 通过 osascript AppleScript 查询。
    Windows: 通过 PowerShell UI Automation 查询。
    Linux: 不支持。

    Args:
        app_name: 应用名称（如 'Firefox', 'Google Chrome', 'Safari', 'Finder'）
        element_role: 元素角色（button, textfield, statictext, checkbox, menuitem...）
        element_title: 元素的标题或描述（模糊匹配）

    Returns:
        匹配元素的描述和位置信息
    """
    import sys
    if sys.platform == "darwin":
        return _query_ui_macos(app_name, element_role, element_title)
    elif sys.platform == "win32":
        return _query_ui_windows(app_name, element_role, element_title)
    else:
        return "[不支持] UI 元素查询仅在 macOS 和 Windows 上可用"


def _query_ui_macos(app_name: str, element_role: str = "",
                    element_title: str = "") -> str:
    """macOS: 通过 osascript AppleScript 查询 UI 元素。"""
    role_filter = f'role = "{element_role}"' if element_role else 'true'
    title_filter = f'name contains "{element_title}" or description contains "{element_title}"' if element_title else 'true'

    script = f'''
tell application "System Events"
    tell process "{app_name}"
        set matchedElements to every UI element whose {role_filter} and {title_filter}
        set resultList to {{}}
        repeat with elem in matchedElements
            set elemPos to position of elem
            set elemSize to size of elem
            set elemRole to role of elem
            set elemName to name of elem
            set elemDesc to description of elem
            set elemValue to value of elem
            copy (elemRole & " | " & elemName & " | " & elemDesc & " | " & elemValue & " | pos:(" & (item 1 of elemPos as string) & "," & (item 2 of elemPos as string) & ") size:(" & (item 1 of elemSize as string) & "," & (item 2 of elemSize as string) & ")") to end of resultList
        end repeat
        return resultList
    end tell
end tell
'''

    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
            env={**os.environ, "NSHighQualityMagnificationFilter": "1"},
        )
        if result.returncode != 0:
            return f"[错误] AppleScript 执行失败：{result.stderr.strip()}"

        lines = [l.strip() for l in result.stdout.strip().split(",") if l.strip()]
        if not lines:
            return f"[无结果] 在 {app_name} 中未找到匹配的元素（role={element_role}, title={element_title}）"

        header = f"在 {app_name} 找到 {len(lines)} 个元素：\n"
        return header + "\n".join(f"  {i+1}. {l}" for i, l in enumerate(lines[:20]))

    except subprocess.TimeoutExpired:
        return "[错误] AppleScript 执行超时"
    except Exception as e:
        return f"[错误] {e}"


def _query_ui_windows(app_name: str, element_role: str = "",
                      element_title: str = "") -> str:
    """Windows: 通过 PowerShell UI Automation 查询 UI 元素。"""
    script = f'''
Add-Type -AssemblyName UIAutomationClient
$apps = Get-Process -Name "{app_name}" -ErrorAction SilentlyContinue
if (-not $apps) {{ return "未找到进程: {app_name}" }}
$app = $apps[0]
$root = [System.Windows.Automation.AutomationElement]::RootElement
$cond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ProcessIdProperty, $app.Id)
$elements = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $cond)
$result = @()
foreach ($elem in $elements) {{
    $rect = $elem.Current.BoundingRectangle
    $result += "$($elem.Current.ControlType.ProgrammaticName) | $($elem.Current.Name) | $($elem.Current.HelpText) | pos:($($rect.X),$($rect.Y)) size:($($rect.Width),$($rect.Height))"
}}
return $result -join "`n"
'''
    try:
        result = subprocess.run(
            ["powershell", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
        if result.returncode != 0:
            return f"[错误] PowerShell 执行失败：{result.stderr.strip()}"
        lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
        if not lines:
            return f"[无结果] 在 {app_name} 中未找到匹配的元素"
        header = f"在 {app_name} 找到 {len(lines)} 个元素：\n"
        return header + "\n".join(f"  {i+1}. {l}" for i, l in enumerate(lines[:20]))
    except subprocess.TimeoutExpired:
        return "[错误] PowerShell 执行超时"
    except Exception as e:
        return f"[错误] {e}"


def get_screen_info() -> str:
    """获取屏幕分辨率信息。"""
    import sys
    size = pyautogui.size()
    if sys.platform == "darwin":
        return (f"屏幕分辨率：{size.width} x {size.height} "
                f"（Retina 逻辑分辨率，实际像素为 2x）")
    else:
        return f"屏幕分辨率：{size.width} x {size.height}"


# ─── 工具注册 ─────────────────────────────────────────────────────────────

SCHEMAS = {
    "desktop_screenshot": {
        "type": "function",
        "function": {
            "name": "desktop_screenshot",
            "description": "截取当前屏幕，保存为 PNG 文件并返回路径。用于获取屏幕内容后配合视觉模型分析。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    "desktop_screenshot_region": {
        "type": "function",
        "function": {
            "name": "desktop_screenshot_region",
            "description": "截取屏幕指定区域，保存为 PNG 文件并返回路径。",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "左上角 X 坐标（像素）"},
                    "y": {"type": "integer", "description": "左上角 Y 坐标（像素）"},
                    "width": {"type": "integer", "description": "区域宽度（像素）"},
                    "height": {"type": "integer", "description": "区域高度（像素）"},
                },
                "required": ["x", "y", "width", "height"],
            },
        },
    },
    "desktop_click": {
        "type": "function",
        "function": {
            "name": "desktop_click",
            "description": "在指定坐标点击鼠标左键。",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X 坐标"},
                    "y": {"type": "integer", "description": "Y 坐标"},
                },
                "required": ["x", "y"],
            },
        },
    },
    "desktop_type": {
        "type": "function",
        "function": {
            "name": "desktop_type",
            "description": "在当前焦点位置输入文本。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要输入的文本"},
                },
                "required": ["text"],
            },
        },
    },
    "desktop_press": {
        "type": "function",
        "function": {
            "name": "desktop_press",
            "description": "按下一个按键，如 enter, esc, space, tab, delete, cmd, shift, ctrl, alt",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "按键名称"},
                },
                "required": ["key"],
            },
        },
    },
    "desktop_hotkey": {
        "type": "function",
        "function": {
            "name": "desktop_hotkey",
            "description": "按组合键，如 cmd+c, cmd+v, cmd+w, cmd+tab, ctrl+c 等",
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "按键列表，如 ['cmd', 'c'] 表示 Cmd+C",
                    },
                },
                "required": ["keys"],
            },
        },
    },
    "desktop_scroll": {
        "type": "function",
        "function": {
            "name": "desktop_scroll",
            "description": "滚动鼠标。正数向上，负数向下。",
            "parameters": {
                "type": "object",
                "properties": {
                    "clicks": {"type": "integer", "description": "滚动格数，正=上，负=下"},
                },
                "required": ["clicks"],
            },
        },
    },
    "desktop_query_ui": {
        "type": "function",
        "function": {
            "name": "desktop_query_ui",
            "description": "查询应用中的 UI 元素（需要应用开启 Accessibility 权限）。返回匹配元素的角色、名称、位置和大小。macOS 和 Windows 均支持。",
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {"type": "string", "description": "应用名称，如 Firefox, Google Chrome, Safari, Finder"},
                    "element_role": {"type": "string", "description": "元素角色，如 button, textfield, statictext, checkbox, menuitem"},
                    "element_title": {"type": "string", "description": "元素标题关键词（模糊匹配）"},
                },
                "required": ["app_name"],
            },
        },
    },
    "desktop_info": {
        "type": "function",
        "function": {
            "name": "desktop_info",
            "description": "获取屏幕分辨率和鼠标位置等基本信息。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
}


def run(name: str, params: dict[str, Any]) -> str:
    handlers = {
        "desktop_screenshot":         lambda _: take_screenshot(),
        "desktop_screenshot_region":  lambda p: take_screenshot_region(p["x"], p["y"], p["width"], p["height"]),
        "desktop_click":             lambda p: click(p["x"], p["y"]),
        "desktop_type":              lambda p: type_text(p.get("text", "")),
        "desktop_press":             lambda p: press_key(p["key"]),
        "desktop_hotkey":            lambda p: hotkey(*p["keys"]),
        "desktop_scroll":            lambda p: scroll(p["clicks"]),
        "desktop_query_ui":          lambda p: query_ui_element(p["app_name"], p.get("element_role", ""), p.get("element_title", "")),
        "desktop_info":              lambda _: get_screen_info(),
    }
    if name not in handlers:
        return f"[错误] 未知工具：{name}"
    return handlers[name](params)
