"""
本地配置管理 —— 让用户不必每次都在画布上重复粘贴 API Key。

支持两种配置文件，放在插件根目录（两者都已加入 .gitignore，不会被提交）：

1) config.ini（推荐，可读性最好）

    [runninghub]
    api_key = 你的 RunningHub API Key
    base_url = https://www.runninghub.cn
    default_workflow_id = 123456
    timeout = 600
    poll_interval = 5

2) config.json（旧版格式，继续兼容）

    {"api_key": "...", "base_url": "...", "default_workflow_id": ""}

优先级（前者覆盖后者）：
    节点上显式填写  >  config.ini  >  config.json  >  内置默认值

这样「临时换一个 Key 试跑」不会被配置文件绑架。
"""

from __future__ import annotations

import configparser
import json
import os
from typing import Any, Dict, List

_INI_FILENAME = "config.ini"
_JSON_FILENAME = "config.json"

_DEFAULTS: Dict[str, Any] = {
    "api_key": "",
    "base_url": "https://www.runninghub.cn",
    "default_workflow_id": "",
    "timeout": 600.0,
    "poll_interval": 5.0,
}

# 哪些键应当解析成数字（ini 读回来一律是字符串）
_NUMBER_KEYS = ("timeout", "poll_interval")

_cache: Dict[str, Any] = {}
_loaded = False


def _plugin_root() -> str:
    """插件根目录（core/ 的上一级）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ini_path() -> str:
    return os.path.join(_plugin_root(), _INI_FILENAME)


def json_path() -> str:
    return os.path.join(_plugin_root(), _JSON_FILENAME)


def config_path() -> str:
    """对外保持旧接口：优先返回存在的那一份。"""
    if os.path.isfile(ini_path()):
        return ini_path()
    return json_path()


def _coerce(key: str, value: Any) -> Any:
    """把字符串配置还原成合适类型。"""
    if key in _NUMBER_KEYS:
        try:
            return float(value)
        except (TypeError, ValueError):
            return _DEFAULTS.get(key, value)
    return value


def _read_ini(path: str) -> Dict[str, Any]:
    parser = configparser.ConfigParser()
    # 保留大小写（API Key 等字段对大小写敏感）
    parser.optionxform = str  # type: ignore[assignment]
    try:
        # 强制 utf-8，避免 Windows 下中文注释导致 UnicodeDecodeError
        with open(path, "r", encoding="utf-8-sig") as fh:
            parser.read_file(fh)
    except Exception as exc:
        print(f"[RunningHub-Latent] config.ini 解析失败，已忽略：{exc}")
        return {}

    out: Dict[str, Any] = {}
    # 允许 [runninghub] / [RH] / DEFAULT 三种写法
    sections: List[str] = []
    for wanted in ("runninghub", "RunningHub", "RH"):
        if parser.has_section(wanted):
            sections.append(wanted)
    if not sections:
        sections = parser.sections()
    for section in sections:
        for key, value in parser.items(section):
            out[str(key).strip()] = _coerce(str(key).strip(), value)
    return out


def _read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"[RunningHub-Latent] config.json 解析失败，已忽略：{exc}")
        return {}


def load(force: bool = False) -> Dict[str, Any]:
    """读取配置（进程内缓存）。文件缺失或写坏都静默回落到默认值。"""
    global _cache, _loaded
    if _loaded and not force:
        return _cache

    merged = dict(_DEFAULTS)
    if os.path.isfile(json_path()):
        merged.update(_read_json(json_path()))
    if os.path.isfile(ini_path()):
        merged.update(_read_ini(ini_path()))

    # 空字符串不该覆盖默认值（用户留空 = 未配置）
    for key, value in list(merged.items()):
        if isinstance(value, str) and value.strip() == "" and key in _DEFAULTS:
            merged[key] = _DEFAULTS[key]

    _cache = merged
    _loaded = True
    return _cache


def reload() -> Dict[str, Any]:
    """用户改了配置文件后强制重读。"""
    return load(force=True)


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


__all__ = [
    "load",
    "get",
    "resolve",
    "reload",
    "config_path",
    "ini_path",
    "json_path",
]
