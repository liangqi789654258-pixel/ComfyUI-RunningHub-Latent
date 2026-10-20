"""VAE 加载与解码 —— 以「只读库」方式使用 ComfyUI 的 comfy.sd。

为什么这样设计：
  * VAE 网络结构与分块解码策略都在 ComfyUI 里实现并持续维护；
    重写等于把 comfy.sd / comfy.ldm 抄一遍且永远跟不上上游。
  * 只 import、不修改：不触碰「绝不修改 ComfyUI 本体」的红线。
  * decode 阶段 ComfyUI 主程序不运行，显存独占。

显存策略（RTX 3060 Laptop 6GB 实测沉淀）：
  * 图像先尝试整图解码，显存不足（torch OOM）自动降级 tiled 解码；
  * 视频一律走分块解码（整流一次 81 帧必炸 6GB），并按分辨率/帧数选档位，
    再 OOM 就沿「降 temporal_size → 降 tile_size」的阶梯自动重试。

档位数值来自插件 docs/video-guide.md 的真机结论（单位=像素，进 VAE 前
要除以 VAE 自身的时间/空间压缩比）：
  * 480p（832×480）          tile 384 / overlap 64 / temporal 32 / temporal_overlap 8
  * 720p（1280×720）         tile 320 / overlap 64 / temporal 16 / temporal_overlap 4
  * 720p 长片段（>121 帧）    tile 256 / overlap 64 / temporal 16 / temporal_overlap 4

关键坑（插件踩过，勿回退）：`temporal_size` 的单位是**像素帧**，不是 latent 帧；
ComfyUI 的 VAEDecodeTiled 会先除以时间压缩比（Wan = 4），填 64（默认值）
等于一次解 64 帧，6GB 必炸。
"""

from __future__ import annotations

import os
import threading
from typing import Any, Dict, List, Optional, Tuple

import torch

from . import core_bridge

_LOCK = threading.Lock()
_VAE_CACHE: dict = {}

# 档位名 -> (tile_size, overlap, temporal_size, temporal_overlap)，全部为像素单位
VIDEO_TIERS: Dict[str, Tuple[int, int, int, int]] = {
    "480p": (384, 64, 32, 8),
    "720p": (320, 64, 16, 4),
    "720p_long": (256, 64, 16, 4),
}

# 超过这个像素帧数就算「长片段」，6GB 上要再降一档
LONG_VIDEO_FRAMES = 121


def pick_video_tier(samples: "torch.Tensor", spatial: int = 8, temporal: int = 4) -> str:
    """按 latent 尺寸推算该用哪个档位（纯函数，便于单测）。

    samples 为 5 维 [B,C,F,H,W]；spatial / temporal 是 VAE 的压缩比，
    Wan 2.1 为 8 / 4。
    """
    latent_h = int(samples.shape[-2])
    latent_f = int(samples.shape[2])
    pixel_h = latent_h * max(1, int(spatial))
    pixel_f = (latent_f - 1) * max(1, int(temporal)) + 1
    if pixel_h <= 480:
        return "480p"
    if pixel_h <= 720:
        return "720p_long" if pixel_f > LONG_VIDEO_FRAMES else "720p"
    # 1080p 在 6GB 上本来就不建议（不是 latent 装不下，是解码中间态撑不住），
    # 仍然给最省的一档，让它有机会慢慢跑完。
    return "720p_long"


def match_av_vaes(vae_dir: str) -> Tuple[str, str]:
    """在 VAE 目录按文件名自动匹配 (视频 VAE, 音频 VAE)，找不到返回空串。

    H3 双 VAE 的命名约定是 video / audio 字样（本机：
    minimax_h3_video_vae_* / minimax_h3_audio_vae_*），优先带 h3 的。
    """
    try:
        files = [
            f for f in os.listdir(vae_dir)
            if f.lower().endswith((".safetensors", ".pt", ".ckpt"))
        ]
    except OSError:
        files = []

    def pick(pred, prefer: Tuple[str, ...]) -> str:
        cands = [f for f in files if pred(f.lower())]
        if not cands:
            return ""
        for key in prefer:
            for f in sorted(cands):
                if key in f.lower():
                    return os.path.join(vae_dir, f)
        return os.path.join(vae_dir, sorted(cands)[0])

    video = pick(lambda f: "video" in f, ("h3",))
    audio = pick(lambda f: "audio" in f, ("h3",))
    return video, audio


def _resolve_vae_file(vae_path: str, on_stage=None) -> str:
    """VAE 路径解析：绝对/已存在的直接用；纯文件名自动到 VAE 目录找。

    历史背景：界面下拉曾把裸文件名当路径传进来，相对路径会拼到程序
    根目录报「文件不存在」——这里统一兜底。
    """
    if os.path.isfile(vae_path):
        return os.path.abspath(vae_path)
    candidate = os.path.join(core_bridge.vae_dir(), os.path.basename(vae_path))
    if os.path.isfile(candidate):
        return os.path.abspath(candidate)
    raise FileNotFoundError(
        f"VAE 文件不存在：{vae_path}\n"
        f"（也找过 VAE 目录：{core_bridge.vae_dir()}）\n"
        "请检查文件是否在 VAE 目录里，或在界面重新选择。"
    )


def _is_oom(exc: BaseException) -> bool:
    if isinstance(exc, torch.cuda.OutOfMemoryError):
        return True
    return "out of memory" in str(exc).lower()


_comfy_nodes_cache: dict = {}


def _comfy_nodes():
    """懒加载 ComfyUI 官方节点实现 —— 解码一律直接调用节点函数，不自己重写。

    这是对「重写等于抄 ComfyUI 且永远跟不上上游」原则的贯彻：
      * Wan 类分块解码 → nodes.VAEDecodeTiled（像素→latent 换算、
        overlap 兜底、压缩比换算全部由节点自己完成）；
      * H3 视频解码 → nodes.VAEDecode（含 nested 拆分）；
      * 音频解码 → comfy_extras.nodes_audio.vae_decode_audio
        （std 归一化、采样率契约与 ComfyUI 永远一致）。
    nodes.py 与 comfy_extras 已验证可独立 import（不启动 ComfyUI）。
    """
    if "tiled" not in _comfy_nodes_cache:
        core_bridge.ensure_comfy()
        import sys  # noqa: PLC0415 - 懒加载

        # 名字冲突：ComfyUI 也有顶层 `app` 包（app.governance），而我们应用
        # 自己的包也叫 app 且已先于它注册进 sys.modules。摘掉我们的、
        # 让 nodes.py 的内部 import 解析到 ComfyUI 自己的包，导完再恢复。
        evicted = {
            name: sys.modules.pop(name)
            for name in list(sys.modules)
            if name == "app" or name.startswith("app.")
        }
        try:
            import nodes as comfy_nodes  # noqa: PLC0415 - 懒加载
            from comfy_extras import nodes_audio  # noqa: PLC0415

            _comfy_nodes_cache["tiled"] = comfy_nodes.VAEDecodeTiled()
            _comfy_nodes_cache["decode"] = comfy_nodes.VAEDecode()
            try:
                _comfy_nodes_cache["audio"] = nodes_audio.vae_decode_audio
            except Exception:
                _comfy_nodes_cache["audio"] = None  # 极老版本无音频节点
        finally:
            sys.modules.update(evicted)  # 恢复我们自己的 app 包
    return _comfy_nodes_cache


class Decoder:
    """VAE 加载 + 图像解码。线程内使用（生成线程串行调用）。"""

    def __init__(self, t=None) -> None:
        # 进度文案的翻译函数：GUI 传入 i18n.tr；不传就原样返回 key（测试/无界面）
        self.t = t or (lambda key, **kw: key)
        self._vae: Any = None
        self._loaded_path: str = ""
        # 音频 VAE（H3 音视频复合 latent 用）：与视频 VAE 并存，各走各的缓存
        self._audio_vae: Any = None
        self._audio_loaded_path: str = ""

    def load(self, vae_path: str, on_stage=None) -> None:
        """加载 VAE（同一路径命中缓存则复用，换路径自动重载）。"""
        vae_path = _resolve_vae_file(vae_path)
        if self._vae is not None and self._loaded_path == vae_path:
            return
        if on_stage:
            on_stage(self.t("dec_load_vae", name=os.path.basename(vae_path)))
        with _LOCK:
            core_bridge.ensure_comfy()
            from comfy.sd import VAE as ComfyVAE  # type: ignore
            from comfy.utils import load_torch_file  # type: ignore

            # 全程包在 inference_mode 里：ComfyUI 内部执行节点时由执行引擎套了
            # 这层上下文，独立调用必须自己补——否则会出现「Inplace update to
            # inference tensor outside InferenceMode is not allowed」。
            # 加载与解码必须同在世界里（权重一旦成为 inference tensor，
            # 之后所有使用都得在 inference_mode 内）。
            with torch.inference_mode():
                sd = load_torch_file(vae_path)
                self._vae = ComfyVAE(sd=sd)
            self._loaded_path = vae_path

    # ------------------------------------------------------------------
    def decode_images(self, samples: torch.Tensor, on_stage=None) -> List[Any]:
        """解码 latent，返回 [N,H,W,C] / 0..1 的 numpy 列表（N 为帧数）。

        接受 4 维 [B,C,H,W]（图像）与 5 维 [B,C,F,H,W]（视频 VAE 原生形态，
        含 F=1 的图像类 3D 卷积 VAE）；解码层不做业务限制，
        放行什么由 runner 决定。
        """
        if self._vae is None:
            raise RuntimeError("VAE 尚未加载")
        if samples.dim() not in (4, 5):
            raise ValueError(
                f"decode_images 需要 4/5 维 latent，收到 {tuple(samples.shape)}"
            )
        # 通道数守卫：选错 VAE（比如给 anima 选了 FLUX 的 VAE）时给说人话的报错，
        # 而不是丢一屏卷积形状天书。
        expected = self._latent_channels()
        if expected is not None and int(samples.shape[1]) != expected:
            raise RuntimeError(
                f"VAE 与 latent 不匹配：当前 VAE 期望 {expected} 通道，"
                f"latent 是 {int(samples.shape[1])} 通道。\n"
                "请在主界面的「VAE 模型」下拉框里换成 latent 对应模型的 VAE。"
            )
        if on_stage:
            on_stage(self.t("dec_whole"))
        with torch.inference_mode():
            try:
                output = self._vae.decode(samples)
            except torch.cuda.OutOfMemoryError:
                torch.cuda.empty_cache()
                if on_stage:
                    on_stage(self.t("dec_oom_tiled"))
                output = self._vae.decode_tiled(
                    samples, tile_x=512, tile_y=512, overlap=64
                )
        return self._normalize_output(output)

    # ------------------------------------------------------------------
    def compression(self) -> Tuple[int, int]:
        """VAE 的空间 / 时间压缩比（拿不到时按 Wan 的 8 / 4 兜底）。"""
        spatial, temporal = 8, 4
        try:
            value = self._vae.spacial_compression_decode()
            spatial = max(1, int(value))
        except Exception:
            pass
        try:
            value = self._vae.temporal_compression_decode()
            if value:
                temporal = max(1, int(value))
        except Exception:
            pass
        return spatial, temporal

    def resolve_pixel_tier(
        self, samples: "torch.Tensor", tier: str = "auto"
    ) -> Tuple[str, Tuple[int, int, int, int]]:
        """档位名 → (档位键, 像素单位参数)。tier 不是已知档位时按尺寸自动选。

        像素参数的换算（÷压缩比、overlap 兜底）交给 ComfyUI 的
        VAEDecodeTiled 节点自己完成，我们只负责选档。
        """
        spatial, temporal = self.compression()
        key = str(tier or "auto")
        if key not in VIDEO_TIERS:
            key = pick_video_tier(samples, spatial, temporal)
        return key, VIDEO_TIERS[key]

    def _attempt_ladder(
        self, samples: "torch.Tensor", tier: str
    ) -> List[Tuple[str, Tuple[int, int, int, int]]]:
        """降档顺序：帧维度最省显存，所以先砍时间维，再砍空间分块。

        每一步都比上一步更省（temporal_size 与 tile 单调不增），
        避免出现「降档后反而更吃显存」的假降级。
        """
        key, (tile, ov, ts, tov) = self.resolve_pixel_tier(samples, tier)
        attempts: List[Tuple[str, Tuple[int, int, int, int]]] = []

        def add(label: str, params: Tuple[int, int, int, int]) -> None:
            if all(params != existing for _, existing in attempts):
                attempts.append((label, params))

        add(f"{key} 档", (tile, ov, ts, tov))
        ts2 = max(8, ts // 2)
        add(
            f"降低时间维（每块 {ts2} 帧）",
            (tile, ov, ts2, max(4, min(tov // 2 or 4, ts2 // 2))),
        )
        tile2 = max(128, tile // 2)
        add(
            f"缩小分块（{tile2}px）+ 最省时间维",
            (tile2, min(ov, max(16, tile2 // 4)), 8, 4),
        )
        add("最省显存档（128px / 8 帧）", (128, 32, 8, 4))
        return attempts

    def decode_video(
        self, samples: "torch.Tensor", tier: str = "auto", on_stage=None
    ) -> Tuple["Any", str, Tuple[int, int, int, int]]:
        """视频 latent（5 维，F>1）→ (帧数组, 档位说明, 实际生效的像素分块参数)。

        显存不够时沿阶梯降档重试，每次降档都通过 on_stage 告知用户，不静默变慢。
        """
        if self._vae is None:
            raise RuntimeError("VAE 尚未加载")
        if samples.dim() != 5:
            raise ValueError(
                f"decode_video 需要 5 维 latent，收到 {tuple(samples.shape)}"
            )
        if samples.shape[2] <= 1:
            raise ValueError(
                "这是 F=1 的图像 latent（qwen 类 3D 卷积 VAE 的形态），"
                "请走 decode_images。"
            )
        expected = self._latent_channels()
        if expected is not None and int(samples.shape[1]) != expected:
            raise RuntimeError(
                f"VAE 与 latent 不匹配：当前 VAE 期望 {expected} 通道，"
                f"latent 是 {int(samples.shape[1])} 通道。\n"
                "视频 latent 必须用对应模型的视频 VAE 解码"
                "（Wan 系列请用 wan_2.1_vae，不要用图片 VAE——"
                "图片 VAE 收到 5 维输入会静默只取第一帧，出一张图且不报错）。"
            )

        spatial, temporal = self.compression()
        latent_f = int(samples.shape[2])
        pixel_f = (latent_f - 1) * temporal + 1
        if on_stage:
            on_stage(self.t(
                "dec_video_latent",
                lf=latent_f, pf=pixel_f,
                w=int(samples.shape[-1]) * spatial,
                h=int(samples.shape[-2]) * spatial,
            ))

        attempts = self._attempt_ladder(samples, tier)
        last_error: Optional[BaseException] = None
        tiled_cls = _comfy_nodes()["tiled"]
        for idx, (label, params) in enumerate(attempts):
            tile, overlap, temporal_size, temporal_overlap = params
            if on_stage:
                spatial, temporal = self.compression()
                on_stage(self.t("dec_tiled", label=label,
                                tile=tile, ts=temporal_size))
            try:
                # 直接调用 ComfyUI 官方 VAEDecodeTiled：像素→latent 换算、
                # overlap 兜底、压缩比处理全部由节点完成，永不跟上游漂移
                with torch.inference_mode():
                    output = tiled_cls.decode(
                        vae=self._vae,
                        samples={"samples": samples},
                        tile_size=tile,
                        overlap=overlap,
                        temporal_size=temporal_size,
                        temporal_overlap=temporal_overlap,
                    )[0]
                return self._normalize_output(output), f"{label}", params
            except Exception as exc:  # noqa: BLE001 - 只为从中挑出 OOM
                if not _is_oom(exc):
                    raise
                last_error = exc
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                if on_stage and idx < len(attempts) - 1:
                    on_stage(self.t("dec_oom_downgrade"))
        raise RuntimeError(
            "视频解码显存不足（已降到最省显存的档位仍失败）。\n"
            "建议：① 把云端帧数或分辨率调小重跑；② 关闭正在占用显存的程序"
            "（ComfyUI 主程序、浏览器视频等）；③ 改用 CPU 解码"
            "（启动前设 CUDA_VISIBLE_DEVICES=\"\" 会慢很多，但一定能跑完）。\n"
            f"最后一次的错误：{last_error}"
        )

    @staticmethod
    def _normalize_output(output: torch.Tensor) -> "Any":
        """把解码输出统一成 [N,H,W,C] / 0..1 的 numpy。

        实测不同 VAE 的输出布局不一致：
          * 常规图像 VAE：[B,H,W,C]；
          * 视频/3D 卷积 VAE（qwen_image_vae 实测）：[B,F,H,W,C]；
        统一摊平到 [N,H,W,C]，帧数 N = B×F（图像时 F=1）。
        """
        arr = output.detach().cpu().float()
        if arr.ndim == 5:
            arr = arr.reshape(-1, *arr.shape[-3:])
        if arr.ndim == 4 and arr.shape[-1] not in (1, 3, 4) and arr.shape[1] in (1, 3, 4):
            arr = arr.permute(0, 2, 3, 1)  # NCHW 兜底转 NHWC
        if arr.ndim == 3:
            arr = arr[None]
        return arr.numpy()

    # ------------------------------------------------ H3 音视频复合支持
    @staticmethod
    def is_h3_video_vae_name(path: str) -> bool:
        """按文件名轻量判断是否 H3 视频 VAE（加载后还有类名级校验兜底）。"""
        name = os.path.basename(str(path or "")).lower()
        return "h3" in name and "video" in name

    def is_h3_video_vae(self) -> bool:
        """当前加载的视频 VAE 是否为 MiniMax H3 视频 VAE（24ch，内部自带分块）。"""
        try:
            return self._vae.first_stage_model.__class__.__name__ == "MiniMaxH3VideoVAE"
        except Exception:
            return False

    def decode_h3_video(self, latent: "torch.Tensor", on_stage=None) -> "Any":
        """H3 视频 latent → 帧数组 [N,H,W,C] / 0..1。

        与 Wan 类不同：H3 视频 VAE **内部自带分块流式解码**
        （256px 空间 / 17 帧时间块，sd.py 已核实），直接整段 decode，
        不走我们自己的分块档位。
        """
        if self._vae is None:
            raise RuntimeError("VAE 尚未加载")
        if not self.is_h3_video_vae():
            raise RuntimeError(
                "当前 VAE 不是 H3 视频 VAE，无法解码音视频复合 latent。"
                "请在 VAE 目录放置 minimax_h3_video_vae 并选择它。"
            )
        if on_stage:
            on_stage(self.t("dec_h3_stream"))
        # 直接调用 ComfyUI 官方 VAEDecode：nested 拆分、解码、布局全部由节点处理
        decode_cls = _comfy_nodes()["decode"]
        with torch.inference_mode():
            output = decode_cls.decode(vae=self._vae, samples={"samples": latent})[0]
        return self._normalize_output(output)

    def load_audio(self, vae_path: str, on_stage=None) -> None:
        """加载音频 VAE（与视频 VAE 并存，各自缓存，同款 inference_mode 契约）。"""
        vae_path = _resolve_vae_file(vae_path)
        if self._audio_vae is not None and self._audio_loaded_path == vae_path:
            return
        if on_stage:
            on_stage(self.t("dec_load_audio_vae", name=os.path.basename(vae_path)))
        with _LOCK:
            core_bridge.ensure_comfy()
            from comfy.sd import VAE as ComfyVAE  # type: ignore
            from comfy.utils import load_torch_file  # type: ignore

            with torch.inference_mode():
                sd = load_torch_file(vae_path)
                self._audio_vae = ComfyVAE(sd=sd)
            self._audio_loaded_path = vae_path

    def decode_audio(self, latent: "torch.Tensor", on_stage=None) -> Tuple["Any", int]:
        """音频 latent（[B,32,2,T]）→ (波形 [S,2] float32 numpy, 采样率)。

        直接复用 ComfyUI 的 vae_decode_audio：nested 取尾、解码、std 归一化、
        采样率契约全部由官方实现保证，这里只做取批与格式转换。
        """
        if self._audio_vae is None:
            raise RuntimeError("音频 VAE 尚未加载")
        model_name = ""
        try:
            model_name = self._audio_vae.first_stage_model.__class__.__name__
        except Exception:
            pass
        if model_name != "MiniMaxH3AudioVAE":
            raise RuntimeError(
                f"所选音频 VAE 不是 H3 音频模型（当前加载的是 {model_name or '未知'}）。"
                "请选择文件名含 audio 的音频 VAE（如 minimax_h3_audio_vae）。"
            )
        if on_stage:
            on_stage(self.t("stage_decode_audio"))
        # 直接调用 ComfyUI 官方 vae_decode_audio：nested 取尾、解码、movedim、
        # std 归一化、采样率契约全部与 ComfyUI 永远一致
        audio_fn = _comfy_nodes().get("audio")
        if audio_fn is None:
            raise RuntimeError("当前 ComfyUI 版本没有音频解码节点，无法解码音轨。")
        with torch.inference_mode():
            result = audio_fn(self._audio_vae, {"samples": latent})
        # ComfyUI AUDIO 契约：waveform = [通道, 采样数] → 转成 [S, 2]
        waveform = result["waveform"][0].t().float().cpu().numpy()
        sr = int(result.get("sample_rate") or 44100)
        return waveform, sr

    def close(self) -> None:
        self._vae = None
        self._loaded_path = ""
        self._audio_vae = None
        self._audio_loaded_path = ""
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def _latent_channels(self):
        """探测 VAE 期望的 latent 通道数（decoder 第一个卷积层的输入通道）。"""
        try:
            import torch.nn as nn

            conv = next(
                m for m in self._vae.first_stage_model.decoder.modules()
                if isinstance(m, (nn.Conv2d, nn.Conv3d))
            )
            return int(conv.in_channels)
        except Exception:
            return None
