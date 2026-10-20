"""引擎冒烟测试（不花云端钱、不动 GPU）：

1. core 桥接：插件 settings/client/workflow/latent/cache/errors/ledger 全部可导入；
2. 映射解析 + 占位符替换（含类型保留与未知占位符报错）；
3. comfy 只读 import：CPU 模式加载 qwen_image_vae 解码一个小 latent 并存图。
"""

import os
import sys

APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP)

os.environ["CUDA_VISIBLE_DEVICES"] = ""  # 强制 CPU，避免打扰正在运行的 ComfyUI

from app.engine import core_bridge  # noqa: E402

failures = []


def check(name, fn):
    try:
        fn()
        print(f"[OK] {name}")
    except Exception as exc:
        failures.append(name)
        print(f"[FAIL] {name}: {exc}")


# 1) core 桥接
def t_bridge():
    settings = core_bridge.core_settings()
    assert hasattr(settings, "resolve")
    client = core_bridge.core_client()
    assert hasattr(client, "RunningHubClient")
    wf = core_bridge.core_workflow()
    assert hasattr(wf, "split_injection_problems")
    latent = core_bridge.core_latent()
    assert hasattr(latent, "parse")
    assert hasattr(core_bridge.core_cache(), "download")
    assert hasattr(core_bridge.core_errors(), "RHUserError")
    assert hasattr(core_bridge.core_ledger(), "append_record")


check("core 桥接（7 个模块）", t_bridge)


# 2) 映射解析 + 占位符
def t_mapping():
    from app.engine.runner import apply_placeholders, parse_mapping

    items = parse_mapping(
        '[{"nodeId":"139","fieldName":"prompt","fieldValue":"{positive}"},'
        '{"nodeId":"3","fieldName":"seed","fieldValue":"{seed}"}]'
    )
    assert len(items) == 2
    resolved = apply_placeholders(items, {"positive": "1girl", "negative": "bad", "seed": 42})
    assert resolved[0]["fieldValue"] == "1girl"
    assert resolved[1]["fieldValue"] == 42 and isinstance(resolved[1]["fieldValue"], int)

    try:
        apply_placeholders(items, {"positive": "x", "negative": "y"})
        raise AssertionError("未知占位符应当报错")
    except Exception as exc:
        assert "seed" in str(exc)

    try:
        parse_mapping('[{"nodeId":"1"} {"nodeId":"2"}]')
        raise AssertionError("坏 JSON 应当报错")
    except Exception as exc:
        assert "JSON" in str(exc)


check("映射解析 + 占位符替换", t_mapping)


# 2b) 云端输出识别（现在转调插件 core/output.py，与节点共用一份实现）
def t_outputs():
    from app.engine import outputs

    outs = [
        {"nodeId": "9", "fileType": "png", "fileUrl": "https://x/a.png?sign=1"},
        {"nodeId": "9", "fileType": "Latent", "fileUrl": "https://x/b.safetensors?sign=2"},
    ]
    assert outputs.classify(outs[0]) == "image"
    assert outputs.classify(outs[1]) == "latent", "大小写不同的 Latent 也要认出来"
    latents = outputs.collect_latents(outs)
    assert len(latents) == 1 and latents[0]["nodeId"] == "9"

    # 找不到 latent 时必须报错并带可操作指引，不能静默返回空
    try:
        outputs.collect_latents([outs[0]])
        raise AssertionError("没有 latent 时应当报错")
    except AssertionError:
        raise
    except Exception as exc:
        assert "没有识别出 latent" in str(exc)

    # dict 简写与 value 别名（插件一直在支持，出图器现在也是同一份实现）
    from app.engine.runner import parse_mapping

    shorthand = parse_mapping('{"3": {"seed": 7}}')
    assert shorthand == [{"nodeId": "3", "fieldName": "seed", "fieldValue": 7}]
    alias = parse_mapping('[{"nodeId":"3","fieldName":"seed","value":7}]')
    assert alias[0]["fieldValue"] == 7


check("云端输出识别 + 映射宽容解析", t_outputs)


# 3) comfy 只读 import + CPU 解码（两款 qwen VAE 布局不同，都要归一化到 [N,H,W,C]）
def t_comfy_decode():
    import glob

    core_bridge.ensure_comfy()
    import torch  # noqa: E402

    from app.engine.decoder import Decoder  # noqa: E402

    vae_dir = r"C:\AI\dongshan\ComfyUI\models\vae"
    candidates = [
        p for p in glob.glob(os.path.join(vae_dir, "*.safetensors"))
        if "qwen" in os.path.basename(p).lower()
    ]
    if not candidates:
        print("   （本地没有 qwen VAE，跳过解码测试）")
        return
    from PIL import Image  # noqa: E402

    for path in candidates:
        name = os.path.basename(path)
        dec = Decoder()
        dec.load(path, on_stage=print)
        import torch.nn as nn  # noqa: E402

        conv = next(
            m for m in dec._vae.first_stage_model.decoder.modules()
            if isinstance(m, (nn.Conv2d, nn.Conv3d))
        )
        in_ch = int(conv.in_channels)
        assert dec._latent_channels() == in_ch, "通道探测与实际卷积不一致"
        latent = (
            torch.randn(1, in_ch, 1, 60, 104)
            if isinstance(conv, nn.Conv3d)
            else torch.randn(1, in_ch, 64, 64)
        )
        frames = dec.decode_images(latent, on_stage=print)
        assert frames.ndim == 4, f"{name}: 归一化后应为 [N,H,W,C]，收到 {frames.shape}"
        assert frames.shape[-1] in (1, 3, 4), f"{name}: 通道数异常 {frames.shape}"
        out = os.path.join(APP, "output", "_smoke_test.png")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        Image.fromarray((frames[0] * 255.0).clip(0, 255).astype("uint8"), "RGB").save(out)
        print(f"   {name}: latent {tuple(latent.shape)} → 帧数组 {frames.shape}")
        dec.close()
        os.remove(out)


check("comfy 只读 import + VAE CPU 解码（多款布局归一化）", t_comfy_decode)

print()
if failures:
    print(f"冒烟测试失败 {len(failures)} 项：{failures}")
    sys.exit(1)
print("冒烟测试全部通过")
