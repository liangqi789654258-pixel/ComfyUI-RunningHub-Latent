"""应用设置（区别于插件 config.ini）：语言 / 输出目录 / VAE 目录 / ComfyUI 根目录。

插件 config.ini 继续管 API Key 与云端参数（Key 单一来源，避免两处配置漂移）；
本文件只管桌面应用自己的偏好。存放在 <应用目录>/data/app_config.json。
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict

from . import paths

_LOCK = threading.Lock()
_CONFIG_PATH = os.path.join(paths.DATA_DIR, "app_config.json")

_DEFAULTS: Dict[str, Any] = {
    "language": "zh",           # zh / en
    "api_key": "",              # 应用内 Key（新用户主入口）；空 = 回落插件 config.ini
    "comfy_root": paths.DEFAULT_COMFY_ROOT,
    "output_dir": paths.DEFAULT_OUTPUT_DIR,
    "vae_dir": "",              # 空 = 跟随 comfy_root/models/vae
    "timeout": 600.0,
    "poll_interval": 5.0,
    "timeout_retries": 1.0,
    # 视频解码默认值（主界面可临时改；这里存的是「上次用过的」）
    "video_tier": "auto",       # auto / 480p / 720p / 720p_long
    "video_fps": 16,            # Wan 2.1 原生 16fps；H3 是 24fps
    "video_crf": 17,
    "window_geometry": [],
}


def _ensure() -> None:
    paths.ensure_dirs()


def path() -> str:
    return _CONFIG_PATH


def load() -> Dict[str, Any]:
    _ensure()
    data = dict(_DEFAULTS)
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
            user = json.load(fh)
        if isinstance(user, dict):
            data.update(user)
    except Exception:
        pass  # 文件缺失 / 写坏 → 默认值
    return data


def save(config: Dict[str, Any]) -> None:
    _ensure()
    with _LOCK:
        tmp = _CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(config, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, _CONFIG_PATH)


def get(key: str, fallback: Any = None) -> Any:
    return load().get(key, _DEFAULTS.get(key, fallback))


def set_value(key: str, value: Any) -> None:
    cfg = load()
    cfg[key] = value
    save(cfg)


def vae_dir() -> str:
    cfg = load()
    return cfg.get("vae_dir") or paths.default_vae_dir(cfg.get("comfy_root") or "")


def output_dir() -> str:
    return load().get("output_dir") or paths.DEFAULT_OUTPUT_DIR


def api_key() -> str:
    """只读应用自身保存的 Key（data/app_config.json，仅存本机）。

    不做任何回落（如插件 config.ini）：Key 栏为空就是没配置，
    出图会明确报错要求先设置——开源发布后绝不能有隐式取 Key 的路径。
    """
    return str(load().get("api_key") or "").strip()
