"""
ComfyUI-RunningHub-Latent 冒烟测试

在没有 RunningHub 账号、没有真实云端环境的前提下，验证所有可以离线验证的核心逻辑：
  * latent 解析（新/旧格式、缩放因子、4D/5D/3D 维度）
  * 输出文件分类
  * 参数注入规范化与占位符
  * 缓存键稳定性
  * API 客户端状态机（用 mock session）
"""

import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.abspath(__file__))
# tests/ 的上一级就是插件根目录
PLUGIN = os.path.abspath(os.path.join(ROOT, ".."))

# 插件目录名含连字符，不能直接用真实目录名做包名。
# 这里注册一个虚拟包并把 __path__ 指向插件目录，从而让 nodes.py 里的
# 「from .core import ...」这类相对导入在测试环境下也能正常解析。
_pkg = types.ModuleType("rhplugin")
_pkg.__path__ = [PLUGIN]
sys.modules["rhplugin"] = _pkg

import torch
import safetensors.torch

from rhplugin.core import cache as cache_mod
from rhplugin.core import client as client_mod
from rhplugin.core import latent as latent_mod
from rhplugin.nodes import (
    _apply_placeholders,
    _classify,
    _collect_latents,
    _merge_latents,
    _normalize_overrides,
    RHUserError,
)

PASSED = []
FAILED = []


def check(name, cond, extra=""):
    if cond:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name} {extra}")


def save_latent(path, tensor, versioned=True, key="latent_tensor"):
    """完全复刻 ComfyUI 原生 SaveLatent 的存储结构。"""
    payload = {key: tensor.contiguous()}
    if versioned:
        payload["latent_format_version_0"] = torch.tensor([])
    safetensors.torch.save_file(payload, path, metadata={"prompt": "{}"})
    return path


# ---------------------------------------------------------------------------
print("\n=== 1. latent 解析：新版格式（带版本标记）不应缩放 ===")
tmp = tempfile.mkdtemp()
LEGACY = 1.0 / 0.18215

t4 = torch.ones(1, 4, 8, 8) * 2.0
p_new = save_latent(os.path.join(tmp, "ComfyUI_00001_.latent"), t4, versioned=True)
loaded = latent_mod.load_safetensors(p_new)
check("新版 latent 保持原始数值（不乘 1/0.18215）", torch.allclose(loaded, t4), f"got {loaded[0,0,0,0]}")

print("\n=== 2. latent 解析：旧版格式必须做缩放补偿 ===")
p_old = save_latent(os.path.join(tmp, "legacy.latent"), t4, versioned=False)
loaded_old = latent_mod.load_safetensors(p_old)
check("旧版 latent 正确乘以 1/0.18215", torch.allclose(loaded_old, t4 * LEGACY), f"got {loaded_old[0,0,0,0]}")

print("\n=== 3. 新版与旧版的倍数关系符合预期（约 5.49 倍） ===")
ratio = (loaded_old[0, 0, 0, 0] / loaded[0, 0, 0, 0]).item()
check("缩放倍率 ≈ 5.4922", abs(ratio - LEGACY) < 1e-4, f"ratio={ratio}")

print("\n=== 4. 视频 latent（5 维）不被误当成缺 batch 轴 ===")
t5 = torch.ones(1, 16, 5, 8, 8)
p_vid = save_latent(os.path.join(tmp, "video.latent"), t5, versioned=True)
loaded_vid = latent_mod.load_safetensors(p_vid)
res_vid = latent_mod.to_comfy_latent(loaded_vid, "video.latent")
check("5 维 latent 形状保持不变 [1,16,5,8,8]", tuple(res_vid["samples"].shape) == (1, 16, 5, 8, 8), str(tuple(res_vid['samples'].shape)))
check("LATENT 输出为标准 {'samples': tensor} 结构", set(res_vid.keys()) == {"samples"})

print("\n=== 5. 无 batch 轴的 3 维 latent 自动补维 ===")
t3 = torch.ones(4, 8, 8)
p3 = save_latent(os.path.join(tmp, "t3.latent"), t3, versioned=True)
res3 = latent_mod.to_comfy_latent(latent_mod.load_safetensors(p3), "t3.latent")
check("3 维补成 [1,4,8,8]", tuple(res3["samples"].shape) == (1, 4, 8, 8), str(tuple(res3['samples'].shape)))

print("\n=== 6. 旧版 key 名 'latents' 也能兼容 ===")
p_alt = save_latent(os.path.join(tmp, "alt.latent"), t4, versioned=True, key="latents")
try:
    loaded_alt = latent_mod.load_safetensors(p_alt)
    check("key='latents' 可被识别", torch.allclose(loaded_alt, t4))
except Exception as e:
    check("key='latents' 可被识别", False, str(e))

print("\n=== 7. 字节流解析 + safetensors 内容嗅探 ===")
with open(p_new, "rb") as f:
    blob = f.read()
check("looks_like_safetensors 识别真文件", latent_mod.looks_like_safetensors(blob))
check("looks_like_safetensors 拒绝随机字节", not latent_mod.looks_like_safetensors(b"not a latent at all!!"))
res_bytes = latent_mod.parse(blob, "https://rh-images.example.com/x/ComfyUI_00001_.latent")
check("从内存字节解析成功", tuple(res_bytes["samples"].shape) == (1, 4, 8, 8))

print("\n=== 8. 损坏文件给出中文可读错误而非崩溃 ===")
try:
    latent_mod.parse(b"\x99" * 64, "https://x/broken.latent")
    check("损坏文件抛出中文异常", False, "未抛异常")
except latent_mod.LatentParseError as e:
    check("损坏文件抛出中文异常", True)
    print(f"         消息示例：{str(e)[:80]}...")
except Exception as e:
    check("损坏文件抛出中文异常", False, type(e).__name__)

# ---------------------------------------------------------------------------
print("\n=== 9. 输出文件分类 ===")
cases = [
    ({"fileType": "latent", "fileUrl": "https://x/ComfyUI_00001_.latent"}, "latent"),
    ({"fileType": "png", "fileUrl": "https://x/a.png"}, "image"),
    ({"fileType": "mp4", "fileUrl": "https://x/a.mp4"}, "video"),
    ({"fileUrl": "https://x/ComfyUI_00002_.safetensors"}, "latent"),
    # 无扩展名但 fileType 是 latent —— RunningHub 真实返回里出现过
    ({"fileType": "latent", "fileUrl": "https://rh-images.example.com/abc123?x=1"}, "latent"),
]
for item, want in cases:
    got = _classify(item)
    check(f"分类 {item.get('fileUrl','')[:40]} -> {want}", got == want, f"got {got}")

print("\n=== 10. 多 latent 合并 / 缺 latent 报错 ===")
outs = [
    {"fileType": "latent", "fileUrl": "https://x/a.latent", "nodeId": "10"},
    {"fileType": "latent", "fileUrl": "https://x/b.latent", "nodeId": "11"},
    {"fileType": "png", "fileUrl": "https://x/c.png", "nodeId": "9"},
]
picked = _collect_latents(outs)
check("正确筛出 2 个 latent", len(picked) == 2, str(len(picked)))
check("按 nodeId 稳定排序", picked[0]["nodeId"] == "10")
merged = _merge_latents([torch.ones(1, 4, 8, 8), torch.ones(1, 4, 8, 8)])
check("同形状沿 batch 合并为 [2,4,8,8]", tuple(merged.shape) == (2, 4, 8, 8))
mism = _merge_latents([torch.ones(1, 4, 8, 8), torch.ones(1, 4, 16, 16)])
check("形状不一致时回退取第一个", tuple(mism.shape) == (1, 4, 8, 8))

try:
    _collect_latents([{"fileType": "png", "fileUrl": "https://x/only.png", "nodeId": "9"}])
    check("无 latent 输出时抛出指引性错误", False)
except RHUserError as e:
    check("无 latent 输出时抛出指引性错误", "保存Latent" in str(e))
    print(f"         消息示例：{str(e).splitlines()[0]}")

# ---------------------------------------------------------------------------
print("\n=== 11. 参数注入规范化 ===")
check("空字符串 -> 空列表", _normalize_overrides("") == [])
check("'[]' -> 空列表", _normalize_overrides("[]") == [])
norm = _normalize_overrides(
    '[{"nodeId": "6", "fieldName": "text", "fieldValue": "1 girl"}]'
)
check("标准 JSON 解析", norm[0]["nodeId"] == "6" and norm[0]["fieldValue"] == "1 girl")

# 用户误写成 value
norm2 = _normalize_overrides('[{"nodeId": "6", "fieldName": "text", "value": "hi"}]')
check("自动纠正 value -> fieldValue", norm2[0]["fieldValue"] == "hi")

# 字典简写
norm3 = _normalize_overrides({"3": {"seed": 123, "steps": 20}, "6": "prompt text"})
check("字典简写展开为 3 项", len(norm3) == 3, str(norm3))
check("字典简写保留 nodeId", all("nodeId" in x and "fieldName" in x for x in norm3))

try:
    _normalize_overrides('[{"nodeId": "6",]')
    check("非法 JSON 抛中文错误", False)
except RHUserError as e:
    check("非法 JSON 抛中文错误", "行" in str(e) or "列" in str(e))

print("\n=== 12. 占位符绑定 ===")
resolved = _apply_placeholders(
    [
        {"nodeId": "3", "fieldName": "seed", "fieldValue": "{seed}"},
        {"nodeId": "6", "fieldName": "text", "fieldValue": "{positive}"},
        {"nodeId": "7", "fieldName": "text", "fieldValue": "raw text"},
    ],
    {"seed": 42, "positive": "a cat"},
)
check("{seed} 被替换为整数值", resolved[0]["fieldValue"] == 42, str(resolved[0]["fieldValue"]))
check("{positive} 被替换", resolved[1]["fieldValue"] == "a cat")
check("无占位符原样保留", resolved[2]["fieldValue"] == "raw text")
try:
    _apply_placeholders(
        [{"nodeId": "3", "fieldName": "seed", "fieldValue": "{unknown_thing}"}], {"seed": 1}
    )
    check("未知占位符报错", False)
except RHUserError as e:
    check("未知占位符报错", "unknown_thing" in str(e))

# ---------------------------------------------------------------------------
print("\n=== 13. 缓存键稳定性 ===")
k1 = cache_mod.compute_cache_key("wf1", [{"nodeId": "3", "fieldName": "seed", "fieldValue": 1}])
k2 = cache_mod.compute_cache_key("wf1", [{"nodeId": "3", "fieldName": "seed", "fieldValue": 1}])
k3 = cache_mod.compute_cache_key("wf1", [{"nodeId": "3", "fieldName": "seed", "fieldValue": 2}])
# 顺序不同但语义相同
k4 = cache_mod.compute_cache_key(
    "wf1",
    [
        {"nodeId": "3", "fieldName": "seed", "fieldValue": 1},
        {"nodeId": "6", "fieldName": "text", "fieldValue": "a"},
    ],
)
k5 = cache_mod.compute_cache_key(
    "wf1",
    [
        {"nodeId": "6", "fieldName": "text", "fieldValue": "a"},
        {"nodeId": "3", "fieldName": "seed", "fieldValue": 1},
    ],
)
k6 = cache_mod.compute_cache_key("wf2", [{"nodeId": "3", "fieldName": "seed", "fieldValue": 1}])
check("相同参数 -> 相同键", k1 == k2)
check("参数变化 -> 不同键", k1 != k3)
check("参数顺序无关（已归一化）", k4 == k5)
check("工作流 ID 参与哈希", k1 != k6)
check("键为 32 位 md5", len(k1) == 32)

# ---------------------------------------------------------------------------
print("\n=== 14. API 客户端状态机（mock） ===")


class FakeResponse:
    def __init__(self, body):
        self._body = body
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class FakeSession:
    """按脚本顺序返回预设响应，模拟 排队->运行->成功 的完整生命周期。"""

    def __init__(self, script):
        self.script = list(script)
        self.payloads = []

    def post(self, url, data=None, headers=None, timeout=None):
        import json as _json

        self.payloads.append((url, data))
        return FakeResponse(self.script.pop(0))

    def get(self, url, params=None, timeout=None):
        return FakeResponse({"code": 0, "data": {}})


script = [
    # 第 1 次调用是 create_task
    {"code": 0, "msg": "success", "data": {"taskId": "1980471280073846785"}},
    # 之后依次是 排队 -> 运行 -> 成功
    {"code": 813, "msg": "queued", "data": None},
    {"code": 804, "msg": "running", "data": None},
    {
        "code": 0,
        "msg": "success",
        "data": [{"fileUrl": "https://x/ComfyUI_00001_.latent", "fileType": "latent", "nodeId": "17"}],
    },
]
sess = FakeSession(script)
client = client_mod.RunningHubClient(api_key="fake-key-0123456789abcdef", session=sess)
task_id = client.create_task("1111", [{"nodeId": "3", "fieldName": "seed", "fieldValue": 1}])
check("create_task 返回 taskId", task_id == "1980471280073846785", str(task_id))

# create_task 必须走 POST /task/openapi/create，且 body 用 fieldValue 字段
sent_url, sent_body = sess.payloads[0]
import json as _json_check

sent = _json_check.loads(sent_body.decode("utf-8"))
check("请求端点正确", sent_url.endswith("/task/openapi/create"), sent_url)
check("载荷含 workflowId", sent.get("workflowId") == "1111")
check("注入字段使用 fieldValue", sent["nodeInfoList"][0].get("fieldValue") == 1, str(sent))

state = client.fetch_outputs("t1")
check("813 -> queued", state["state"] == "queued")
state = client.fetch_outputs("t1")
check("804 -> running", state["state"] == "running")
state = client.fetch_outputs("t1")
check("code 0 -> success 且返回结果列表", state["state"] == "success" and len(state["outputs"]) == 1)

fail_sess = FakeSession(
    [
        {
            "code": 805,
            "msg": "failed",
            "data": {
                "failedReason": {
                    "node_name": "KSampler",
                    "exception_message": "Allocation on device",
                    "traceback": "File x",
                }
            },
        }
    ]
)
fail_client = client_mod.RunningHubClient(api_key="k", session=fail_sess)
try:
    fail_client.fetch_outputs("t2")
    check("805 抛出 TaskFailedError", False)
except client_mod.TaskFailedError as e:
    check("805 抛出 TaskFailedError", "KSampler" in str(e) and "Allocation" in str(e))
    print(f"         消息示例：{e}")
    check("错误信息包含云端节点名", e.node_name == "KSampler")

# API Key 泄漏检查
print("\n=== 15. 密钥脱敏 ===")
masked = client_mod.RunningHubClient._mask("key abcdefghij0123456789abcdef leaked")
check("长串密钥被打码", "****" in masked, masked)
check("脱敏后不再包含完整密钥", "abcdefghij0123456789" not in masked, masked)
check("短串不会被误伤", client_mod.RunningHubClient._mask("shortword") == "shortword")

# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
if FAILED:
    print("失败项：" + ", ".join(FAILED))
    sys.exit(1)
print("全部通过")
