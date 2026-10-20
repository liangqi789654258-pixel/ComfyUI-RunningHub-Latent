"""桌面快捷方式自安装：首次启动时在桌面创建「RH出图器.lnk」。

幂等：已存在则跳过；pywin32 缺失或创建失败都静默（只是少了快捷方式，
不影响应用本身）。用户也可以随时删除快捷方式，下次启动不会再创建。
"""

from __future__ import annotations

import os

SHORTCUT_NAME = "RH出图器.lnk"


def desktop_shortcut_path() -> str:
    desktop = os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")
    return os.path.join(desktop, SHORTCUT_NAME)


def ensure_desktop_shortcut() -> bool:
    """返回快捷方式是否就绪（已存在或创建成功）。"""
    lnk = desktop_shortcut_path()
    if os.path.isfile(lnk):
        return True
    desktop = os.path.dirname(lnk)
    if not os.path.isdir(desktop):
        return False
    try:
        from win32com.client import Dispatch  # ComfyUI venv 自带 pywin32

        app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        main_py = os.path.join(app_dir, "main.py")
        icon = os.path.join(app_dir, "app", "icon.ico")
        pythonw = os.path.join(
            os.path.dirname(sys_executable()), "pythonw.exe"
        )
        if not os.path.isfile(pythonw):
            pythonw = sys_executable()

        ws = Dispatch("WScript.Shell")
        shortcut = ws.CreateShortCut(lnk)
        shortcut.TargetPath = pythonw
        shortcut.Arguments = f'"{main_py}"'
        shortcut.WorkingDirectory = app_dir
        if os.path.isfile(icon):
            shortcut.IconLocation = icon
        shortcut.Description = "RH 出图器 - RunningHub 云端采样 + 本地 VAE 解码"
        shortcut.Save()
        return os.path.isfile(lnk)
    except Exception:
        return False


def sys_executable() -> str:
    import sys

    return sys.executable
