"""
本地配置管理 —— 让用户不必每次都在画布上重复粘贴 API Key。

配置文件放在插件根目录下的 config.json（已加入 .gitignore，绝不会被提交到仓库）：

    {
      "api_key": "你的 RunningHub API Key",
      "base_url": "https://www.runninghub.cn",
      "default_workflow_id": "",
      "timeout": 600,
      "poll_interval": 5
    }

优先级：节点显式输入  >  config.json  >  内置默认值。
这样「临时换一个 Key 试跑」不会被配置文件绑架。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict

_CONFIG_FILENAME = "config.json"
_EXAMPLE_FILENAME = "config.example.json"

_DEFAULTS: Dict[str, Any] = {
    "api_key": "",
    "base_url": "https://www.runninghub.cn",
    "default_workflow_id": "",
    "timeout": 600.0,
    "poll_interval": 5.0,
}

_cache: Dict[str, Any] = {}
_loaded = False


def config_path() -> str:
    """插件根目录下的 config.json 绝对路径。"""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), _CONFIG_FILENAME)


def load(force: bool = False) -> Dict[str, Any]:
    """读取配置（带进程内缓存）。文件缺失或格式错误时静默回落到默认值。"""
    global _cache, _loaded
    if _loaded and not force:
        return _cache

    merged = dict(_DEFAULTS)
    path = config_path()
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                merged.update(json.load(fh) or {})
        except Exception as exc:
            # 配置文件写坏了不该让整个插件加载失败
            print(f"[RunningHub-Latent] config.json 解析失败，已回退为默认配置：{exc}")

    _cache = merged
    _loaded = True
    return _cache


def get(key: str, fallback: Any = None) -> Any:
    return load().get(key, fallback)


def resolve(explicit: Any, key: str, fallback: Any = None) -> Any:
    """统一的「显式输入优先」解析：空字符串 / None 都视为未填写。"""
    if explicit is not None and str(explicit).strip() != "":
        return explicit
    configured = get(key, None)
    if configured is not None and str(configured).strip() != "":
        return configured
    return fallback


__all__ = ["load", "get", "resolve", "config_path", "_EXAMPLE_FILENAME"]
