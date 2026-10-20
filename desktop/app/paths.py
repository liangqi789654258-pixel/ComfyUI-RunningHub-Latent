"""路径定位：ComfyUI 根目录、插件目录、应用自身目录。

应用不安装进 ComfyUI，与插件平级摆放；所有可变路径都允许用户在设置里改，
这里只提供默认值与集中读取。
"""

from __future__ import annotations

import os
import sys

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(APP_ROOT, "data")
PRESETS_DIR = os.path.join(APP_ROOT, "presets")
DEFAULT_OUTPUT_DIR = os.path.join(APP_ROOT, "output")


def _looks_like_comfy(root: str) -> bool:
    """粗判一个目录是不是 ComfyUI 根（有 custom_nodes 就够，models 可能被挪走）。"""
    return bool(root) and os.path.isdir(os.path.join(root, "custom_nodes"))


def detect_comfy_root() -> str:
    """自动探测 ComfyUI 根目录。

    以前这里写死成作者本机路径（`C:\\AI\\dongshan\\ComfyUI`）——对任何别人
    都是错的：装完打开就报「找不到插件目录」，还得自己去设置里摸路径。
    现在按可靠度依次尝试：

      1. 环境变量 RH_COMFY_ROOT（批量部署 / 多环境切换用）；
      2. 仓库布局：本应用在 `<ComfyUI>/custom_nodes/<插件>/desktop/` 里；
      3. 解释器布局：用 ComfyUI 自己的 python 启动（`.venv` / `python_embeded`）。

    都失败就返回空串——设置界面里会显示为空，让用户显式填，
    而不是指到一个根本不存在的路径上装成功。
    """
    env = os.environ.get("RH_COMFY_ROOT", "").strip()
    if _looks_like_comfy(env):
        return os.path.normpath(env)

    # 2) APP_ROOT = .../desktop，上三级 = ComfyUI 根
    cand = os.path.abspath(os.path.join(APP_ROOT, "..", "..", ".."))
    if _looks_like_comfy(cand):
        return cand

    # 3) 从解释器位置反推
    exe = os.path.abspath(sys.executable).replace("\\", "/")
    parts = exe.split("/")
    for i, seg in enumerate(parts[:-1]):
        low = seg.lower()
        root = "/".join(parts[:i])
        if low in (".venv", "venv"):
            if _looks_like_comfy(root):
                return os.path.normpath(root)
        elif low.startswith("python_embeded"):
            # 便携版：<父目录>/python_embeded/python.exe + <父目录>/ComfyUI/
            inner = os.path.join(root, "ComfyUI")
            if _looks_like_comfy(inner):
                return os.path.normpath(inner)
            if _looks_like_comfy(root):
                return os.path.normpath(root)
    return ""


# 默认 ComfyUI 根目录（用户可在应用设置里改；改完影响插件定位与 comfy import）
DEFAULT_COMFY_ROOT = detect_comfy_root()


def plugin_dir(comfy_root: str) -> str:
    """插件目录（core 单一代码源所在地）。"""
    return os.path.join(comfy_root, "custom_nodes", "ComfyUI-RunningHub-Latent")


def default_vae_dir(comfy_root: str) -> str:
    return os.path.join(comfy_root, "models", "vae")


def ensure_dirs() -> None:
    for d in (DATA_DIR, PRESETS_DIR, DEFAULT_OUTPUT_DIR):
        os.makedirs(d, exist_ok=True)
