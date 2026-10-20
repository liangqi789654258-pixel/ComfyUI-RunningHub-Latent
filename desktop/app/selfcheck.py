"""启动自检：把「为什么一打开就出不了图」的四种常见原因一次查完。

以前这些问题是分散暴露的：VAE 列表空着、点生成才报「找不到插件目录」、
视频解码到最后一步才发现没 ffmpeg。新用户得挨个踩一遍才知道缺什么。

这里只做**检查 + 给修复入口**，不改任何配置——检查项全部是只读的。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, List

from . import appconfig
from .engine import core_bridge

# 级别：block = 不修就出不了图；warn = 能跑但有风险
BLOCK = "block"
WARN = "warn"


@dataclass
class Check:
    key: str          # i18n key（标题）
    ok: bool
    level: str
    detail: str = ""  # 具体值（路径等），直接展示
    fix: str = ""     # 修复建议（i18n key）


def _check_api_key() -> Check:
    key = appconfig.api_key()
    if key:
        return Check("sc_api_key", True, BLOCK, detail=f"{key[:4]}••••{key[-4:]}")
    return Check("sc_api_key", False, BLOCK, fix="sc_fix_api_key")


def _check_comfy_root() -> Check:
    root = core_bridge.comfy_root()
    if not root:
        return Check("sc_comfy_root", False, BLOCK, fix="sc_fix_comfy_root")
    if not os.path.isdir(os.path.join(root, "custom_nodes")):
        return Check("sc_comfy_root", False, BLOCK, detail=root, fix="sc_fix_comfy_root")
    return Check("sc_comfy_root", True, BLOCK, detail=root)


def _check_plugin() -> Check:
    try:
        pdir = core_bridge.plugin_dir()
    except Exception as exc:  # comfy_root 没配时会抛
        return Check("sc_plugin", False, BLOCK, detail=str(exc).splitlines()[0],
                     fix="sc_fix_comfy_root")
    if os.path.isfile(os.path.join(pdir, "core", "client.py")):
        return Check("sc_plugin", True, BLOCK, detail=pdir)
    return Check("sc_plugin", False, BLOCK, detail=pdir, fix="sc_fix_plugin")


def _check_vae() -> Check:
    vae_dir = appconfig.vae_dir()
    names: List[str] = []
    if os.path.isdir(vae_dir):
        names = [
            n for n in os.listdir(vae_dir)
            if n.lower().endswith((".safetensors", ".pt", ".ckpt", ".pth"))
        ]
    if names:
        return Check("sc_vae", True, BLOCK, detail=f"{vae_dir}（{len(names)} 个）")
    return Check("sc_vae", False, BLOCK, detail=vae_dir or "（未设置）", fix="sc_fix_vae")


def _check_ffmpeg() -> Check:
    from .engine import video as video_mod

    try:
        if video_mod.available():
            return Check("sc_ffmpeg", True, BLOCK, detail=video_mod.ffmpeg_exe())
    except Exception:
        pass
    return Check("sc_ffmpeg", False, BLOCK, fix="sc_fix_ffmpeg")


def _check_output() -> Check:
    out = appconfig.output_dir()
    try:
        os.makedirs(out, exist_ok=True)
        probe = os.path.join(out, ".rh_write_probe")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        return Check("sc_output", True, WARN, detail=out)
    except Exception as exc:
        return Check("sc_output", False, WARN, detail=f"{out}（{exc}）",
                     fix="sc_fix_output")


def run_checks() -> List[Check]:
    """跑一遍全部检查。任何一项抛异常都只算该项失败，不影响其他项。"""
    checks: List[Callable[[], Check]] = [
        _check_api_key,
        _check_comfy_root,
        _check_plugin,
        _check_vae,
        _check_ffmpeg,
        _check_output,
    ]
    out: List[Check] = []
    for fn in checks:
        try:
            out.append(fn())
        except Exception as exc:  # 自检本身绝不能把应用带崩
            out.append(Check(fn.__name__, False, WARN, detail=str(exc)[:120]))
    return out


def blocking(checks: List[Check]) -> List[Check]:
    """必须修的项目。全部 ok 时返回空列表——界面据此决定要不要弹窗。"""
    return [c for c in checks if not c.ok and c.level == BLOCK]
