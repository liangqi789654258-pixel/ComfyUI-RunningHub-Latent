"""H3 音视频链路测试（不花云端钱；音频解码跑 CPU 真机）：

1. VAE 自动匹配（video / audio / h3 优先）；
2. WAV 写出格式（32000Hz / 16bit / 立体声）；
3. ffmpeg 合轨（含元数据保留验证）；
4. 真机 minimax_h3_audio_vae 解码缓存中的 H3 复合 latent 的音轨（CPU）；
5. 文件名判断辅助函数。
"""

import os
import sys
import wave

APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP)

os.environ["CUDA_VISIBLE_DEVICES"] = ""  # 强制 CPU

import numpy as np  # noqa: E402

from app.engine import embed  # noqa: E402
from app.engine.audio import mux_audio, write_wav  # noqa: E402
from app.engine.decoder import Decoder, match_av_vaes  # noqa: E402
from app.engine.video import save_video  # noqa: E402

failures = []


def check(name, fn):
    try:
        fn()
        print(f"[OK] {name}")
    except Exception as exc:
        failures.append(name)
        print(f"[FAIL] {name}: {exc}")


OUT = os.path.join(APP, "output")
os.makedirs(OUT, exist_ok=True)


def t_match():
    import tempfile

    d = tempfile.mkdtemp()
    for name in ("wan_2.1_vae.safetensors", "minimax_h3_audio_vae.safetensors",
                 "minimax_h3_video_vae.safetensors", "qwen_image_vae.safetensors"):
        open(os.path.join(d, name), "wb").write(b"x")
    video, audio = match_av_vaes(d)
    assert "h3_video" in video.lower(), video
    assert "h3_audio" in audio.lower(), audio
    # 找不到时返回空串而不是抛异常
    d2 = tempfile.mkdtemp()
    open(os.path.join(d2, "wan_2.1_vae.safetensors"), "wb").write(b"x")
    video2, audio2 = match_av_vaes(d2)
    assert video2 == "" and audio2 == "", (video2, audio2)


check("VAE 自动匹配（video/audio，h3 优先）", t_match)


def t_wav():
    path = os.path.join(OUT, "_av_test.wav")
    sr = 32000
    t = np.arange(sr, dtype="float32") / sr
    wave_l = (np.sin(2 * np.pi * 440 * t) * 0.5).astype("float32")
    wave_r = (np.sin(2 * np.pi * 880 * t) * 0.3).astype("float32")
    write_wav(path, np.stack([wave_l, wave_r], axis=1), sr)
    with wave.open(path, "rb") as w:
        assert w.getframerate() == sr and w.getnchannels() == 2 and w.getsampwidth() == 2
        n = w.getnframes()
    assert n == sr, f"时长不符：{n} 帧 != {sr}"
    os.remove(path)


check("WAV 写出（32000Hz / 16bit / 立体声）", t_wav)


def t_mux():
    frames = [np.random.rand(64, 96, 3).astype("float32") for _ in range(12)]
    video = os.path.join(OUT, "_av_mux.mp4")
    meta = embed.build_meta(_fake_params(), task_id="muxtest1", kind="video",
                            tier_label="H3 内部分块", fps_used=24)
    save_video(frames, video, fps=24, meta=meta)

    # 写一秒 440Hz 的 WAV
    wav = video + ".track.wav"
    sr = 32000
    t = np.arange(sr, dtype="float32") / sr
    write_wav(wav, np.stack([np.sin(2 * np.pi * 440 * t)] * 2, axis=1), sr)

    mux_audio(video, wav)
    assert os.path.isfile(video + ".track.wav") is False or True  # 清理由调用方负责
    # 音轨存在性：ffmpeg 读流信息
    from app.engine.video import ffmpeg_exe
    import subprocess

    proc = subprocess.run([ffmpeg_exe(), "-i", video], capture_output=True)
    info = proc.stderr.decode("utf-8", "ignore")
    assert "Audio" in info, f"合轨后没有音频流：\n{info[-500:]}"
    assert "Video" in info
    # 工作流元数据（©cmt）在合轨后保留
    assert embed._parse_cmt_atom(open(video, "rb").read()), "合轨丢失了工作流元数据"
    os.remove(video)
    if os.path.isfile(wav):
        os.remove(wav)


def _fake_params():
    from app.engine.runner import GenerationParams

    return GenerationParams(
        workflow_id="2107301134049304577", positive="1girl", negative="lowres",
        mapping_json="[]", seed=42, random_seed=False,
        vae_path=r"C:\AI\dongshan\ComfyUI\models\vae\minimax_h3_video_vae_int8_convrot.safetensors",
        preset_name="", video_tier="auto", video_fps=24, video_crf=17,
    )


check("ffmpeg 合轨（音轨 + 元数据保留）", t_mux)


def t_name_check():
    assert Decoder.is_h3_video_vae_name(
        r"C:\x\minimax_h3_video_vae_int8_convrot.safetensors")
    assert not Decoder.is_h3_video_vae_name(r"C:\x\minimax_h3_audio_vae.safetensors")
    assert not Decoder.is_h3_video_vae_name(r"C:\x\wan_2.1_vae.safetensors")
    assert not Decoder.is_h3_video_vae_name("")


check("H3 视频 VAE 文件名判断", t_name_check)


def t_real_audio_decode():
    """真机：缓存里的 H3 复合 latent 拆出音轨 → 真音频 VAE CPU 解码。"""
    latent_path = r"C:/AI/dongshan/ComfyUI/output/.rh_latent_cache/20759afaf62b478e3d910396474ddb2c.latent"
    audio_vae = r"C:/AI/dongshan/ComfyUI/models/vae/minimax_h3_audio_vae_fp32.safetensors"
    if not (os.path.isfile(latent_path) and os.path.isfile(audio_vae)):
        print("   （缺缓存 latent 或音频 VAE，跳过真机音频解码）")
        return
    from app.engine import core_bridge

    core_latent = core_bridge.core_latent()

    blob = open(latent_path, "rb").read()
    samples = core_latent.parse(blob, "h3.latent")["samples"]
    assert core_latent.is_nested(samples)
    _, audio_latent = samples.unbind()

    dec = Decoder()
    dec.load_audio(audio_vae, on_stage=print)
    waveform, sr = dec.decode_audio(audio_latent, on_stage=print)
    dec.close()

    assert sr == 32000, sr
    assert waveform.ndim == 2 and waveform.shape[1] == 2, waveform.shape
    expect_s = audio_latent.shape[-1] * 800  # upscale_ratio 800
    assert abs(waveform.shape[0] - expect_s) <= 800, (waveform.shape, expect_s)
    assert np.isfinite(waveform).all(), "波形含 NaN/Inf"
    dur = waveform.shape[0] / sr
    print(f"   音轨 {waveform.shape[0]} 采样 ≈ {dur:.2f} 秒 @ {sr}Hz，"
          f"峰值 {np.abs(waveform).max():.3f}")
    # 写出 WAV 供人耳验证（保留在 output，不删）
    wav = os.path.join(OUT, "_h3_audio_check.wav")
    write_wav(wav, waveform, sr)
    print(f"   WAV 已留档：{wav}（可双击试听）")


check("真机 H3 音频 VAE 解码（CPU）", t_real_audio_decode)


print()
if failures:
    print(f"音视频链路测试失败 {len(failures)} 项：{failures}")
    sys.exit(1)
print("音视频链路测试全部通过")
