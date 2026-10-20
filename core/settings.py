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
    cache_max_mb = 512

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
    # latent 缓存目录总大小上限（MB），超过自动删最旧的；0 = 关闭清理
    "cache_max_mb": 512.0,
    # 轮询超时后的自动续等次数（极简版/配置文件路径；专业版节点上有同名 widget）
    "timeout_retries": 1.0,
    # 是否查询积分消耗并写台账（查询失败不影响任务）
    "query_coins": True,
}

# 哪些键应当解析成数字（ini 读回来一律是字符串）
_NUMBER_KEYS = ("timeout", "poll_interval", "cache_max_mb", "timeout_retries")

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


# ---------------------------------------------------------------------------
# API Key 状态标签
#
# 设计要点：节点上的 api_key 输入框**不存密钥**，只存一个无害的状态标签。
#   * 有 Key：标签 = 「已配置 · 使用 config.ini」
#   * 没 Key：空串（由 placeholder 提示怎么配置）
# 为什么这么做：ComfyUI 的画布只渲染 widget.value，想让用户一眼看到「已配置」
# 就必须让 value 非空；而 value 会被写进工作流 JSON，所以它绝不能是密钥。
# 标签是明文、无敏感信息，序列化天然安全（导出时仍会被前端清空，见 web 脚本）。
# ---------------------------------------------------------------------------

STATUS_LABEL = "已配置 · 使用 config.ini"


def is_status_label(value: Any) -> bool:
    """判断一个 api_key 输入值是不是我们写的状态标签（而非真 Key）。"""
    text = str(value or "").strip()
    return text == STATUS_LABEL or text.startswith("已配置")


def api_key_hint() -> str:
    """当前应显示的状态标签：有 Key 给标签，没 Key 给空串。"""
    key = get("api_key", "")
    return STATUS_LABEL if str(key or "").strip() else ""


def api_key_placeholder() -> str:
    """输入框为空时的提示文案（也随配置状态变化）。

    这不是冗余：万一前端脚本没加载成功（浏览器缓存 / 前端改版），
    标签写不进去，placeholder 仍然能告诉用户「已配置 / 未配置」。
    """
    key = str(get("api_key", "") or "").strip()
    if not key:
        return "未配置：右键本节点 →「设置 API Key」（保存到 config.ini）"
    return f"已配置（{mask_key(key)}）· 粘贴新 Key 可覆盖"


def resolve_api_key(raw: Any) -> str:
    """解析节点的 api_key 输入，返回真正可用的 Key。

    三种输入：
      * 状态标签（「已配置 · 使用 config.ini」）→ 视为未填，走配置；
      * 空串 → 走配置；
      * 真 Key（用户直接粘贴的）→ 优先用它。
    """
    if is_status_label(raw):
        raw = ""
    return str(resolve(raw, "api_key", "") or "").strip()


# ---------------------------------------------------------------------------
# API Key 转存（前端守卫的配套后端）
#
# 设计目标：API Key 只存在于 config.ini，绝不进入工作流 JSON / 图片元数据。
# 前端在序列化出口（graphToPrompt / graph.serialize）统一清空 api_key 字段，
# 并把用户填写的 Key 通过 HTTP 接口转存到 config.ini；节点执行时走
# 「留空自动读取 config.ini」的既有契约（resolve）。
# ---------------------------------------------------------------------------


def save_api_key(key: Any, path: str | None = None) -> bool:
    """把 API Key 写入 config.ini 的 [runninghub] 段。

    用行级替换而不是 configparser 整体重写，保住用户手写的注释。
    key 为空时拒绝写入（清空 Key 请手动编辑 config.ini）。
    返回是否写入成功。
    """
    key = str(key or "").strip()
    if not key:
        return False
    # 换行会把 ini 结构写坏（粘贴时很容易带上），直接剔掉
    key = key.replace("\r", "").replace("\n", "").strip()
    if not key:
        return False

    target = path or ini_path()
    lines: List[str] = []
    if os.path.isfile(target):
        try:
            with open(target, "r", encoding="utf-8-sig") as fh:
                lines = fh.read().splitlines()
        except Exception as exc:
            print(f"[RunningHub-Latent] config.ini 读取失败，拒绝覆盖：{exc}")
            return False

    # 找到第一个 [runninghub] 类段（runninghub / RunningHub / RH）
    section_idx = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            name = stripped[1:-1].strip().lower()
            if name in ("runninghub", "rh"):
                section_idx = i
                break

    if section_idx == -1:
        # 没有该段：追加一个新段
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.append("[runninghub]")
        lines.append(f"api_key = {key}")
    else:
        # 段内找 api_key 行；顺带确定段尾（下一个段头）位置
        key_idx = -1
        end_idx = len(lines)
        for i in range(section_idx + 1, len(lines)):
            stripped = lines[i].strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                end_idx = i
                break
            low = stripped.lower()
            if low.startswith("api_key") and ("=" in stripped or stripped.endswith("api_key")):
                key_idx = i
                break
        if key_idx >= 0:
            lines[key_idx] = f"api_key = {key}"
        else:
            lines.insert(end_idx, f"api_key = {key}")

    try:
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception as exc:
        print(f"[RunningHub-Latent] config.ini 写入失败：{exc}")
        return False

    load(force=True)
    return True


def mask_key(key: Any) -> str:
    """Key 的脱敏展示：只露头尾各 4 位，中间用圆点。"""
    key = str(key or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "•" * len(key)
    return key[:4] + "••••" + key[-4:]


def key_status(path: str | None = None) -> Dict[str, str]:
    """给前端的状态查询：只返回「是否已配置 + 脱敏预览」，绝不回传完整 Key。"""
    configured = str(get("api_key", "") or "").strip()
    return {
        "present": "true" if configured else "false",
        "masked": mask_key(configured),
    }


__all__ = [
    "load",
    "get",
    "resolve",
    "reload",
    "config_path",
    "ini_path",
    "json_path",
    "save_api_key",
    "mask_key",
    "key_status",
]
