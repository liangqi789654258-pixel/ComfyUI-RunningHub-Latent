"""生成任务编排：预校验 → 提交 → 轮询 → 下载 → 解析 → 解码 → 保存 → 台账。

这是插件 nodes.py `_run_sampling` 的桌面版移植：
  * 相同的规则：映射预校验（803 拦截在本地）、占位符整值替换保留类型、
    多尺寸 latent 直接报错、超时续等（已付费任务不判死）、积分前后查询、
    成功/失败都写台账；
  * 不同的出口：解码后保存 PNG / mp4 到用户设定的输出目录（按日期分组）。

结构说明：`run()` 只做编排，真正干活的是按阶段拆开的私有方法。
以前这五件事挤在一个 277 行的方法里，任何一处改动都得通读全文才能确认没伤到别处。
拆开之后，`resume()`（按 taskId 续取）能直接复用「轮询之后的全部阶段」，
不必把取回逻辑再抄一遍。

进度文案不写死中文：调用方可以传 `t`（翻译函数，默认原样返回 key），
GUI 传 `i18n.tr`，这样引擎层依旧不依赖界面语言。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import core_bridge, decoder as decoder_mod, embed as embed_mod, outputs as outputs_mod


@dataclass
class GenerationParams:
    workflow_id: str = ""
    positive: str = ""
    negative: str = ""
    mapping_json: str = ""
    seed: int = 0
    random_seed: bool = True
    vae_path: str = ""
    preset_name: str = ""
    # 视频解码（仅当云端返回 5 维视频 latent 时生效）
    video_tier: str = "auto"    # auto / 480p / 720p / 720p_long
    video_fps: int = 16         # Wan 2.1 原生 16fps；H3 是 24fps
    video_crf: int = 17
    audio_vae_path: str = ""    # H3 音频 VAE；空 = 按文件名自动匹配 VAE 目录
    video_vae_path: str = ""    # 视频 VAE 覆盖；空 = 跟随上方 vae_path


@dataclass
class GenerationResult:
    files: List[str] = field(default_factory=list)
    task_id: str = ""
    elapsed_s: float = 0.0
    coins_used: Optional[float] = None
    kind: str = "image"         # image / video，供界面显示
    video_tier: str = ""        # 视频实际用的解码档位（写进台账便于复盘）
    video_tile: tuple = ()      # 视频实际生效的分块参数（嵌进元数据供还原）


def parse_mapping(text: str) -> List[Dict[str, Any]]:
    """解析用户映射 JSON。

    实现下沉到插件 core/mapping.py（与节点共用一份），这里只做转调——
    以前是抄了一份，语义已经和插件悄悄分叉过（不支持 dict 简写 / value 别名）。
    """
    return core_bridge.core_mapping().normalize_overrides(text)


def apply_placeholders(
    node_info_list: List[Dict[str, Any]], values: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """替换 {seed} / {positive} 占位符（同样下沉到 core/mapping.py）。"""
    return core_bridge.core_mapping().apply_placeholders(node_info_list, values)


def _coins_of(status: Dict[str, Any]) -> Optional[float]:
    try:
        raw = status.get("remainCoins")
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _identity(key: str, **kwargs: Any) -> str:
    """默认翻译：直接用 key 本身（独立测试 / 无界面场景）。"""
    return key


class _Ctx:
    """一次运行的共享状态（避免十几个参数在方法间传来传去）。"""

    def __init__(
        self,
        params: GenerationParams,
        progress: Callable[[str], None],
        cancel_check: Callable[[], bool],
        t: Callable[..., str],
        client: Any,
        api_key: str,
        errors: Any,
    ) -> None:
        self.params = params
        self.progress = progress
        self.cancel_check = cancel_check
        self.t = t
        self.client = client
        self.api_key = api_key
        self.errors = errors
        self.result = GenerationResult()
        self.canceled = False
        self.started = time.time()
        self.task_id = ""
        self.coins_before: Optional[float] = None

    def stage(self, key: str, **kwargs: Any) -> None:
        """上报一条进度（key 由调用方翻译）。"""
        try:
            self.progress(self.t(key, **kwargs))
        except Exception:
            pass  # 进度上报失败绝不能影响生成

    def cancelled(self) -> bool:
        return bool(self.cancel_check())


class GenerationRunner:
    """一次生成的完整流水线。在专用线程里调用 run()。"""

    def __init__(self, t=None) -> None:
        # 解码器是长生命周期对象（缓存已加载的 VAE），翻译函数在构造时注入一次
        self._decoder = decoder_mod.Decoder(t=t)

    # ============================================================ 对外入口
    def run(
        self,
        params: GenerationParams,
        progress: Callable[[str], None],
        cancel_check: Callable[[], bool],
        t: Callable[..., str] = _identity,
    ) -> GenerationResult:
        """完整流程：提交 → 轮询 → 取回 → 解码 → 落盘。"""
        from .. import appconfig

        errors = core_bridge.core_errors()
        api_key = appconfig.api_key()
        if not api_key:
            raise errors.RHUserError(
                "没有配置 API Key。请点右上角「设置」，在 API Key 一栏填入后保存。"
            )
        ctx = _Ctx(params, progress, cancel_check, t,
                   core_bridge.core_client().RunningHubClient(api_key), api_key, errors)

        outcome, error_reason = "success", ""
        try:
            self._guard_cancel(ctx, before_start=True)
            resolved = self._prepare(ctx)
            self._submit(ctx, resolved)
            outputs = self._poll(ctx)
            samples = self._fetch_samples(ctx, outputs)
            self._decode_and_save(ctx, samples)
            self._finalize(ctx)
            return ctx.result
        except Exception as exc:
            outcome = "canceled" if ctx.canceled else "failed"
            error_reason = str(exc)[:300]
            raise
        finally:
            self._write_ledger(ctx, outcome, error_reason)

    def resume(
        self,
        task_id: str,
        params: GenerationParams,
        progress: Callable[[str], None],
        cancel_check: Callable[[], bool],
        t: Callable[..., str] = _identity,
    ) -> GenerationResult:
        """按 taskId 续取：不提交、不扣费，直接从轮询接上。

        用途：上次超时 / 关掉窗口 / 网络断了，但云端任务已经跑完并计费。
        复用「轮询之后」的全部阶段，所以取回、解码、落盘的规则与正常流程完全一致。
        """
        from .. import appconfig

        errors = core_bridge.core_errors()
        api_key = appconfig.api_key()
        if not api_key:
            raise errors.RHUserError(
                "没有配置 API Key。请点右上角「设置」，在 API Key 一栏填入后保存。"
            )
        task_id = str(task_id or "").strip()
        if not task_id:
            raise errors.RHUserError("请填写要续取的 taskId。")

        ctx = _Ctx(params, progress, cancel_check, t,
                   core_bridge.core_client().RunningHubClient(api_key), api_key, errors)
        ctx.task_id = task_id

        outcome, error_reason = "success", ""
        try:
            self._guard_cancel(ctx, before_start=True)
            outputs = self._poll(ctx)
            samples = self._fetch_samples(ctx, outputs)
            self._decode_and_save(ctx, samples)
            self._finalize(ctx)
            return ctx.result
        except Exception as exc:
            outcome = "canceled" if ctx.canceled else "failed"
            error_reason = str(exc)[:300]
            raise
        finally:
            self._write_ledger(ctx, outcome, error_reason, source="standalone_resume")

    # ============================================================ 各阶段
    def _guard_cancel(self, ctx: _Ctx, before_start: bool = False) -> None:
        if not ctx.cancelled():
            return
        ctx.canceled = True
        if before_start:
            raise ctx.errors.RHUserError(ctx.t("cancel_pre"))
        raise ctx.errors.RHUserError(
            ctx.t("cancel_mid", task_id=ctx.task_id)
        )

    def _prepare(self, ctx: _Ctx) -> List[Dict[str, Any]]:
        """拉云端结构 + 解析映射 + 本地预校验（把 803 拦在花钱之前）。"""
        params = ctx.params
        wf = core_bridge.core_workflow()

        structure = ctx.client.get_workflow_structure(params.workflow_id)
        injections = parse_mapping(params.mapping_json)
        seed = params.seed if not params.random_seed else _random_seed()
        values = {
            "positive": params.positive or "",
            "negative": params.negative or "",
            "seed": seed,
        }
        resolved = apply_placeholders(injections, values)
        fatal, linked = wf.split_injection_problems(structure, resolved)
        if fatal:
            raise ctx.errors.RHUserError.build(
                ctx.errors.CAT_MAPPING,
                f"参数映射里有 {len(fatal)} 条指向了云端工作流中不存在的落点，提交必被 803 拒绝。",
                stage="提交前本地预校验",
                context={"问题明细": "\n".join("    " + x for x in fatal)},
                actions=("用预设生成时附带的字段清单核对 nodeId 与 fieldName；",),
            )
        for warn in linked:
            ctx.stage("stage_warn", text=warn)
        return resolved

    def _submit(self, ctx: _Ctx, resolved: List[Dict[str, Any]]) -> None:
        ctx.stage("stage_submit")
        try:  # 积分查询失败不影响任务本身
            ctx.coins_before = _coins_of(ctx.client.get_account_status())
        except Exception:
            ctx.coins_before = None
        ctx.task_id = ctx.client.create_task(ctx.params.workflow_id, resolved)

    def _poll(self, ctx: _Ctx) -> List[Dict[str, Any]]:
        """轮询直到完成；超时按配置续等（已付费任务不该被本地计时器判死）。"""
        client_mod = core_bridge.core_client()
        settings = core_bridge.core_settings()

        timeout = float(settings.get("timeout", 600.0))
        poll_interval = float(settings.get("poll_interval", 5.0))
        rounds = int(float(settings.get("timeout_retries", 1.0)))
        attempts = max(1, rounds + 1)

        for attempt in range(attempts):
            self._guard_cancel(ctx)
            try:
                return ctx.client.wait_for_outputs(
                    ctx.task_id,
                    poll_interval=poll_interval,
                    timeout=timeout,
                    on_progress=lambda state, elapsed: ctx.stage(
                        "stage_poll",
                        state=ctx.t("state_running" if state == "running"
                                    else "state_queued"),
                        elapsed=elapsed,
                    ),
                )
            except client_mod.TaskTimeoutError:
                if attempt < attempts - 1:
                    ctx.stage("stage_retry", timeout=timeout,
                              attempt=attempt + 2, total=attempts)
                    continue
                raise
        return []  # 理论上不可达（最后一轮必然 raise 或 return）

    def _fetch_samples(self, ctx: _Ctx, outputs: List[Dict[str, Any]]) -> Any:
        """识别输出 → 下载 → 解析 → （多文件时）合并成一个 latent。"""
        latent = core_bridge.core_latent()
        cache = core_bridge.core_cache()

        ctx.stage("stage_parse")
        latents = outputs_mod.collect_latents(outputs)
        tensors = []
        for item in latents:
            url = outputs_mod.url_of(item)
            ctx.stage("stage_download")
            blob = cache.download(url, timeout=120.0)
            tensors.append(latent.parse(blob, url))
        return outputs_mod.merge_latents(
            [t["samples"] for t in tensors if isinstance(t, dict)]
        )

    def _decode_and_save(self, ctx: _Ctx, samples: Any) -> None:
        """按 latent 形态分三条路：H3 复合 / 普通视频 / 图像。"""
        latent = core_bridge.core_latent()
        if latent.is_nested(samples):
            self._decode_av(ctx, samples)
            return
        if samples.dim() not in (4, 5):
            raise ctx.errors.RHUserError(
                ctx.t("err_latent_shape", shape=tuple(samples.shape))
            )
        # qwen 等 3D 卷积 VAE 的**图像** latent 自带长度为 1 的时间轴
        # （[B,C,1,H,W]），所以时间维 F>1 才算视频。
        if samples.dim() == 5 and samples.shape[2] > 1:
            self._decode_video(ctx, samples)
        else:
            self._decode_image(ctx, samples)

    def _decode_av(self, ctx: _Ctx, samples: Any) -> None:
        """H3 音视频复合 latent：拆分 → 双 VAE 解码 → ffmpeg 合轨。"""
        from .. import appconfig
        from . import audio as audio_mod

        params = ctx.params
        video_latent, audio_latent = samples.unbind()
        ctx.result.kind = "video"

        # ---- 选视频 VAE：显式覆盖 > 主下拉（非 H3 名时自动切换）> 自动匹配
        vae_dir = appconfig.vae_dir()
        auto_video, auto_audio = decoder_mod.match_av_vaes(vae_dir)
        if params.video_vae_path:
            video_vae = params.video_vae_path  # 显式覆盖：用户自负（有通道守卫）
        else:
            video_vae = params.vae_path
            if not decoder_mod.Decoder.is_h3_video_vae_name(video_vae) and auto_video:
                video_vae = auto_video
                ctx.stage("av_vae_switch", name=os.path.basename(auto_video))
        if not video_vae:
            raise ctx.errors.RHUserError(ctx.t("err_h3_vae_needed"))
        self._decoder.load(video_vae, on_stage=ctx.progress)
        if not self._decoder.is_h3_video_vae():
            raise ctx.errors.RHUserError(ctx.t("err_h3_vae_mismatch"))

        # ---- 视频：H3 模型内部自带 17 帧分块流式解码，直接整段 decode
        frames = self._decoder.decode_h3_video(video_latent, on_stage=ctx.progress)
        ctx.result.video_tier = "H3 内部分块"
        fps = 24  # H3 原生帧率，音画同步的生命线
        meta = embed_mod.build_meta(
            params, task_id=ctx.task_id, kind="video",
            tier_label=ctx.result.video_tier, video_tile=None, fps_used=fps,
        )
        _assert_no_key_leak(meta, ctx.api_key)
        ctx.stage("stage_save_video", fps=fps, extra="")
        video_file = _save_video(
            frames,
            output_dir=_output_dir(),
            task_id=ctx.task_id,
            fps=fps,
            crf=params.video_crf,
            on_stage=ctx.progress,
            meta=meta,
        )[0]
        ctx.result.files = [video_file]

        # ---- 音频：自动匹配或用户指定；找不到则降级无声视频
        audio_vae = params.audio_vae_path or auto_audio
        if not audio_vae:
            ctx.stage("av_silent_note")
            return
        try:
            self._decoder.load_audio(audio_vae, on_stage=ctx.progress)
            waveform, sample_rate = self._decoder.decode_audio(
                audio_latent, on_stage=ctx.progress
            )
            wav_path = video_file + ".track.wav"
            audio_mod.write_wav(wav_path, waveform, sample_rate)
            audio_mod.mux_audio(video_file, wav_path, on_stage=ctx.progress)
        finally:
            if os.path.isfile(video_file + ".track.wav"):
                try:
                    os.remove(video_file + ".track.wav")
                except OSError:
                    pass

    def _decode_video(self, ctx: _Ctx, samples: Any) -> None:
        """Wan 类视频：分块解码 → mp4。档位按 latent 尺寸自动选，OOM 逐级降档。"""
        params = ctx.params
        self._decoder.load(params.video_vae_path or params.vae_path,
                           on_stage=ctx.progress)
        frames, tier_used, tile_used = self._decoder.decode_video(
            samples, tier=params.video_tier, on_stage=ctx.progress
        )
        ctx.result.kind = "video"
        ctx.result.video_tier = tier_used
        ctx.result.video_tile = tile_used
        meta = embed_mod.build_meta(
            params, task_id=ctx.task_id, kind="video",
            tier_label=tier_used, video_tile=tile_used,
        )
        _assert_no_key_leak(meta, ctx.api_key)
        ctx.stage("stage_save_video", fps=params.video_fps, extra=f" / {tier_used}")
        ctx.result.files = _save_video(
            frames,
            output_dir=_output_dir(),
            task_id=ctx.task_id,
            fps=params.video_fps,
            crf=params.video_crf,
            on_stage=ctx.progress,
            meta=meta,
        )

    def _decode_image(self, ctx: _Ctx, samples: Any) -> None:
        params = ctx.params
        self._decoder.load(params.vae_path, on_stage=ctx.progress)
        ctx.stage("stage_decode")
        frames = self._decoder.decode_images(samples, on_stage=ctx.progress)
        ctx.stage("stage_save")
        ctx.result.files = _save_frames(
            frames, output_dir=_output_dir(), task_id=ctx.task_id
        )
        # 元数据：拖回 ComfyUI 可还原工作流，右键可复用参数
        meta = embed_mod.build_meta(params, task_id=ctx.task_id, kind="image")
        _assert_no_key_leak(meta, ctx.api_key)
        for saved in ctx.result.files:
            embed_mod.attach_png(saved, meta)

    def _finalize(self, ctx: _Ctx) -> None:
        ctx.result.task_id = ctx.task_id
        ctx.result.elapsed_s = time.time() - ctx.started
        try:  # 积分后查询：失败不影响结果
            coins_after = _coins_of(ctx.client.get_account_status())
            if ctx.coins_before is not None and coins_after is not None:
                ctx.result.coins_used = ctx.coins_before - coins_after
        except Exception:
            pass

    def _write_ledger(self, ctx: _Ctx, outcome: str, error_reason: str,
                      source: str = "standalone") -> None:
        try:
            core_bridge.core_ledger().append_record(
                {
                    "source": source,
                    "preset": ctx.params.preset_name,
                    "task_id": ctx.task_id,
                    "workflow_id": ctx.params.workflow_id,
                    "outcome": outcome,
                    "kind": ctx.result.kind,
                    "video_tier": ctx.result.video_tier,
                    "video_fps": ctx.params.video_fps if ctx.result.kind == "video" else None,
                    "elapsed_s": round(time.time() - ctx.started, 2),
                    "coins_before": ctx.coins_before,
                    "coins_spent": ctx.result.coins_used,
                    "error_reason": error_reason,
                    "files": ctx.result.files,
                }
            )
        except Exception:
            pass  # 台账失败绝不能影响主流程

    def shutdown(self) -> None:
        self._decoder.close()


def _random_seed() -> int:
    import random

    return random.randint(0, 2**48)


def _assert_no_key_leak(meta: dict, api_key: str) -> None:
    """安全红线：嵌入媒体的元数据绝不能带 API Key（图片/视频会被分享）。"""
    if not api_key:
        return
    if api_key in json.dumps(meta, ensure_ascii=False):
        raise RuntimeError(
            "内部错误：待嵌入的元数据中检测到 API Key，已中止保存以保护你的密钥。"
            "请把此报错发给作者。"
        )


def _output_dir() -> str:
    from .. import appconfig

    return appconfig.output_dir()


def _save_frames(frames: List[Any], output_dir: str, task_id: str) -> List[str]:
    from PIL import Image

    from .frames import to_uint8_rgb

    day_dir = os.path.join(output_dir, time.strftime("%Y-%m-%d"))
    os.makedirs(day_dir, exist_ok=True)
    saved: List[str] = []
    stamp = time.strftime("%Y%m%d_%H%M%S")
    for i, frame in enumerate(frames):
        img = Image.fromarray(to_uint8_rgb(frame), mode="RGB")
        path = os.path.join(day_dir, f"RH_{stamp}_{task_id[-6:]}_{i + 1:02d}.png")
        img.save(path)
        saved.append(path)
    return saved


def _save_video(
    frames: List[Any],
    output_dir: str,
    task_id: str,
    fps: int,
    crf: int,
    on_stage=None,
    meta: Optional[dict] = None,
) -> List[str]:
    from .video import save_video

    day_dir = os.path.join(output_dir, time.strftime("%Y-%m-%d"))
    os.makedirs(day_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(day_dir, f"RH_{stamp}_{task_id[-6:]}_video.mp4")
    save_video(frames, path, fps=fps, crf=crf, on_stage=on_stage, meta=meta)
    return [path]
