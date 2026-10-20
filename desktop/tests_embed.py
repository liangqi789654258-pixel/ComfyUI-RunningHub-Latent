"""元数据嵌入测试（不花云端钱、不动 GPU）：

1. PNG：attach_png 写 prompt/workflow/rh_params 三块 tEXt → read_params 无损读回；
2. mp4：save_video 带 meta → ©cmt 落位（模拟 VHS videoinfo.js 前端解析）→ 读回；
3. 反推兜底：剥离 rh_params 后仍能从 prompt/workflow 还原复用参数；
4. 旧版 sidecar 兼容读取；
5. API Key 泄漏守卫。
"""

import json
import os
import sys

APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP)

import numpy as np  # noqa: E402

from app.engine import embed  # noqa: E402
from app.engine.runner import GenerationParams, _assert_no_key_leak  # noqa: E402
from app.engine.video import save_video  # noqa: E402

failures = []


def check(name, fn):
    try:
        fn()
        print(f"[OK] {name}")
    except Exception as exc:
        failures.append(name)
        print(f"[FAIL] {name}: {exc}")


def _fake_params(**kw):
    defaults = dict(
        workflow_id="2107301134049304577",
        positive="1girl, solo",
        negative="lowres",
        mapping_json='[{"nodeId":"557","fieldName":"width","fieldValue":768}]',
        seed=42,
        random_seed=False,
        vae_path=r"C:\AI\dongshan\ComfyUI\models\vae\wan_2.1_vae.safetensors",
        preset_name="工作流 2107",
        video_tier="720p",
        video_fps=16,
        video_crf=17,
    )
    defaults.update(kw)
    return GenerationParams(**defaults)


OUT = os.path.join(APP, "output")
os.makedirs(OUT, exist_ok=True)


def t_png_roundtrip():
    from PIL import Image

    path = os.path.join(OUT, "_embed_test.png")
    Image.fromarray(np.zeros((16, 24, 3), dtype="uint8")).save(path)
    meta = embed.build_meta(_fake_params(), task_id="test123456", kind="image")
    embed.attach_png(path, meta)

    rh = embed.read_params(path)
    assert rh and rh["positive"] == "1girl, solo", rh
    assert rh["seed"] == 42 and rh["video_fps"] == 16
    assert rh["workflow_id"] == "2107301134049304577"
    assert rh["mapping"].startswith("[")
    # API / workflow 结构
    prompt = embed.read_png_params(path)["prompt"]
    assert prompt["1"]["class_type"] == "RunningHubLatentGenerator"
    assert prompt["1"]["inputs"]["api_key"] == "", "API Key 必须为空"
    wf = embed.read_png_params(path)["workflow"]
    assert wf["version"] == 0.4 and len(wf["nodes"]) == 4
    assert wf["nodes"][0]["type"] == "RunningHubLatentGenerator"
    # 专业版有 3 个输出
    assert len(wf["nodes"][0]["outputs"]) == 3
    # seed 后面必须跟 control widget 值（UI 还原不错位）
    assert "fixed" in wf["nodes"][0]["widgets_values"]
    # 链路连线
    assert wf["links"] == [
        [1, 1, 0, 3, 0, "LATENT"],
        [2, 2, 0, 3, 1, "VAE"],
        [3, 3, 0, 4, 0, "IMAGE"],
    ]
    os.remove(path)


check("PNG 元数据写入/读回 + workflow 结构", t_png_roundtrip)


def t_mp4_roundtrip():
    frames = [np.random.rand(32, 48, 3).astype("float32") for _ in range(6)]
    path = os.path.join(OUT, "_embed_test.mp4")
    meta = embed.build_meta(
        _fake_params(), task_id="test123456", kind="video",
        tier_label="720p 档", video_tile=(320, 64, 16, 4),
    )
    save_video(frames, path, fps=16, meta=meta)

    # 模拟 VHS videoinfo.js 前端解析（必须命中 workflow）
    blob = embed._parse_cmt_atom(open(path, "rb").read())
    assert blob, "©cmt 原子未落位"
    parsed = json.loads(blob)
    assert "workflow" in parsed and "rh_params" in parsed

    rh = embed.read_params(path)
    assert rh and rh["kind"] == "video"
    assert rh["video_tile"] == [320, 64, 16, 4]
    assert rh["positive"] == "1girl, solo"
    os.remove(path)


check("mp4 ©cmt 写入/前端同款解析读回", t_mp4_roundtrip)


def t_reconstruct_fallback():
    from PIL import Image

    path = os.path.join(OUT, "_embed_recon.png")
    Image.fromarray(np.zeros((8, 8, 3), dtype="uint8")).save(path)
    meta = embed.build_meta(_fake_params(), task_id="x", kind="video",
                            tier_label="720p 档", video_tile=(320, 64, 16, 4))
    meta.pop("rh_params")  # 模拟没有 rh_params 的老图
    embed.attach_png(path, meta)
    rh = embed.read_params(path)
    assert rh, "反推兜底不应返回 None"
    assert rh["kind"] == "video", rh["kind"]
    assert rh["video_tile"] == [320, 64, 16, 4]
    assert rh["positive"] == "1girl, solo"
    assert rh["workflow_id"] == "2107301134049304577"
    os.remove(path)


check("无 rh_params 时从 workflow 反推", t_reconstruct_fallback)


def t_sidecar_fallback():
    path = os.path.join(OUT, "_embed_legacy.png")
    from PIL import Image

    Image.fromarray(np.zeros((8, 8, 3), dtype="uint8")).save(path)
    legacy = {
        "rh_standalone_params": 1, "kind": "image", "task_id": "old",
        "workflow_id": "123", "preset_name": "", "positive": "old prompt",
        "negative": "", "mapping": "[]", "seed": 7, "random_seed": True,
        "vae_path": "x.safetensors", "video_tier": "", "video_fps": 16,
        "video_crf": 17,
    }
    with open(path + ".json", "w", encoding="utf-8") as fh:
        json.dump(legacy, fh)
    rh = embed.read_params(path)
    assert rh and rh["positive"] == "old prompt", rh
    os.remove(path)
    os.remove(path + ".json")


check("旧版 sidecar 兼容读取", t_sidecar_fallback)


def t_leak_guard():
    try:
        _assert_no_key_leak({"prompt": {"k": "sk-abc123xyz"}}, "sk-abc123xyz")
        raise AssertionError("泄漏未拦截")
    except RuntimeError as exc:
        assert "API Key" in str(exc)
    # 空 Key 不报错
    _assert_no_key_leak({"a": 1}, "")


check("API Key 泄漏守卫", t_leak_guard)


def t_widget_order_in_sync():
    """`_RH_WIDGET_ORDER` 必须与插件专业版节点的 INPUT_TYPES 顺序一致。

    这是跨两个代码库的硬耦合：顺序错了，拖进 ComfyUI 的工作流画布上
    字段会整体错位——但只影响显示，不影响执行，所以极难被肉眼发现。
    用 AST 静态读取 nodes.py，不 import（避免拉 torch / ComfyUI）。
    """
    import ast

    from app.engine import core_bridge

    nodes_py = os.path.join(core_bridge.plugin_dir(), "nodes.py")
    assert os.path.isfile(nodes_py), f"找不到插件 nodes.py：{nodes_py}"

    tree = ast.parse(open(nodes_py, encoding="utf-8").read())
    cls = next(
        (n for n in tree.body
         if isinstance(n, ast.ClassDef) and n.name == "RunningHubLatentGenerator"),
        None,
    )
    assert cls is not None, "插件里找不到 RunningHubLatentGenerator"

    func = next(
        (n for n in cls.body
         if isinstance(n, ast.FunctionDef) and n.name == "INPUT_TYPES"),
        None,
    )
    assert func is not None, "RunningHubLatentGenerator 缺 INPUT_TYPES"

    # 取 return {...} 里 required 这个 dict 的键顺序
    ret = next(n for n in ast.walk(func) if isinstance(n, ast.Return))
    outer = ret.value
    assert isinstance(outer, ast.Dict), "INPUT_TYPES 返回值不是字面量 dict"
    required = None
    for k, v in zip(outer.keys, outer.values):
        if isinstance(k, ast.Constant) and k.value == "required":
            required = v
            break
    assert isinstance(required, ast.Dict), "INPUT_TYPES 缺 required"

    declared = [k.value for k in required.keys if isinstance(k, ast.Constant)]

    # UI 会自动在 seed 后面插一个 control_after_generate 控件
    expected = []
    for name in declared:
        expected.append(name)
        if name == "seed":
            expected.append("__seed_control__")

    actual = list(embed._RH_WIDGET_ORDER)
    assert actual == expected, (
        "embed._RH_WIDGET_ORDER 与插件节点字段顺序不一致：\n"
        f"  出图器: {actual}\n"
        f"  插件  : {expected}"
    )


check("embed 的节点字段顺序与插件 INPUT_TYPES 一致", t_widget_order_in_sync)


print()
if failures:
    print(f"元数据嵌入测试失败 {len(failures)} 项：{failures}")
    sys.exit(1)
print("元数据嵌入测试全部通过")
