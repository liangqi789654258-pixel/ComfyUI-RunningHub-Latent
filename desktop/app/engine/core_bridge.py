"""桥接层：把插件 core 与 ComfyUI 的 comfy 挂进本进程。

红线：这里只做 sys.path 注入与 import，不复制、不修改任何一方代码。
"""
from __future__ import annotations

import importlib
import os
import sys
from typing import Any, Dict

from .. import appconfig, paths

_ready = False
_core_modules: Dict[str, Any] = {}


def comfy_root() -> str:
    return str(appconfig.get("comfy_root") or "")


def vae_dir() -> str:
    """VAE 目录（appconfig 的透传，供 engine 层解析裸文件名用）。"""
    return appconfig.vae_dir()


def plugin_dir() -> str:
    """定位插件目录（core 所在地）。

    两种布局都支持：
      1. 应用放在插件仓库内：<插件>/desktop/  → core 就在上一级；
      2. 应用独立安装：<ComfyUI>/custom_nodes/ComfyUI-RunningHub-Latent。
    """
    candidate = os.path.dirname(paths.APP_ROOT)
    if os.path.isdir(os.path.join(candidate, "core")):
        return candidate
    root = comfy_root()
    if not root:
        raise RuntimeError(
            "还没有配置 ComfyUI 根目录，找不到插件自带的 core 模块。\n"
            "请点右上角「设置」，把 ComfyUI 根目录填成你本机 ComfyUI 的安装位置"
            "（里面有 custom_nodes 文件夹的那一层）。"
        )
    return os.path.join(root, "custom_nodes", "ComfyUI-RunningHub-Latent")


def ensure_core() -> None:
    """把插件目录加进 sys.path，使 `import core.xxx` 可用（幂等）。"""
    global _ready
    if _ready:
        return
    pdir = plugin_dir()
    if not os.path.isdir(pdir):
        raise RuntimeError(
            f"找不到插件目录：{pdir}\n"
            "说明 ComfyUI 根目录填错了，或者插件没有装进 custom_nodes。\n"
            "请在「设置」里核对 ComfyUI 根目录。"
        )
    if pdir not in sys.path:
        sys.path.insert(0, pdir)
    _ready = True


def core(name: str) -> Any:
    """取插件里的 `core.<name>` 模块（带缓存，幂等）。

    以前每个模块一个 accessor（7 个复制粘贴的两行函数），加一个 core 模块就要
    再抄一份；现在统一走这里。下面的具名函数保留只是为了不改调用点。
    """
    ensure_core()
    module = _core_modules.get(name)
    if module is None:
        module = importlib.import_module(f"core.{name}")
        _core_modules[name] = module
    return module


def core_settings() -> Any:
    return core("settings")


def core_client() -> Any:
    return core("client")


def core_workflow() -> Any:
    return core("workflow")


def core_latent() -> Any:
    return core("latent")


def core_cache() -> Any:
    return core("cache")


def core_errors() -> Any:
    return core("errors")


def core_ledger() -> Any:
    return core("ledger")


def core_mapping() -> Any:
    return core("mapping")


def core_output() -> Any:
    return core("output")


def ensure_comfy() -> None:
    """把 ComfyUI 根目录加进 sys.path，使 `import comfy` 可用（幂等）。"""
    root = comfy_root()
    if not os.path.isdir(root):
        raise RuntimeError(f"找不到 ComfyUI 根目录：{root}")
    if root not in sys.path:
        sys.path.insert(0, root)
