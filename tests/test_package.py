"""
交付物一致性检查：确认「节点定义」与「示例工作流 JSON」完全对得上。

这类不一致是 ComfyUI 插件最常见的发布事故——节点改了 widget，
示例工作流没跟着改，用户导入后一片红色报错。
"""

import json
import os
import sys
import importlib.util

ROOT = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.abspath(os.path.join(ROOT, ".."))  # tests/ 的上一级
EXAMPLES = os.path.join(PLUGIN, "examples")

_pkg = None

# 注意：不能用「先塞一个空 ModuleType 进 sys.modules」的办法，
# 那样 __init__.py 会被 import 机制判定为已加载而直接跳过执行，
# NODE_CLASS_MAPPINGS 就永远是空的。必须走 spec + exec_module 真正执行它。
_spec = importlib.util.spec_from_file_location(
    "rhplugin", os.path.join(PLUGIN, "__init__.py"), submodule_search_locations=[PLUGIN]
)
_pkg = importlib.util.module_from_spec(_spec)
sys.modules["rhplugin"] = _pkg
_spec.loader.exec_module(_pkg)

ok = True


def fail(msg):
    global ok
    ok = False
    print(f"  [FAIL] {msg}")


def good(msg):
    print(f"  [OK]   {msg}")


print("=== 1. 插件包可正常加载 ===")
import rhplugin as plugin

mappings = getattr(plugin, "NODE_CLASS_MAPPINGS", {})
displays = getattr(plugin, "NODE_DISPLAY_NAME_MAPPINGS", {})
if not mappings:
    fail("NODE_CLASS_MAPPINGS 为空")
else:
    good(f"注册了 {len(mappings)} 个节点：{', '.join(sorted(mappings))}")
if len(mappings) != len(displays):
    fail("NODE_CLASS_MAPPINGS 与 NODE_DISPLAY_NAME_MAPPINGS 数量不一致")
else:
    good("显示名映射数量一致")

print("\n=== 2. 每个节点的结构完整性 ===")
for name, cls in mappings.items():
    for attr in ("INPUT_TYPES", "RETURN_TYPES", "FUNCTION", "CATEGORY"):
        if not hasattr(cls, attr):
            fail(f"{name} 缺少 {attr}")
    itypes = cls.INPUT_TYPES()
    if "required" not in itypes:
        fail(f"{name}.INPUT_TYPES 缺少 required")
    # FUNCTION 必须指向真实存在的方法
    if not hasattr(cls, cls.FUNCTION):
        fail(f"{name}.FUNCTION='{cls.FUNCTION}' 不是可调用方法")
    else:
        good(f"{name}: {len(itypes.get('required', {}))} 个输入, "
             f"{len(cls.RETURN_TYPES)} 个输出, 执行方法={cls.FUNCTION}")

print("\n=== 3. 示例工作流与节点定义一致性 ===")
from rhplugin.nodes import RunningHubLatentGenerator as Gen

# 所有 required 输入都会成为 widget（本插件没有复杂类型输入）
expected_widgets = list(Gen.INPUT_TYPES()["required"].keys())
n_returns = len(Gen.RETURN_TYPES)

for fname in sorted(os.listdir(EXAMPLES)):
    if not fname.endswith(".json"):
        continue
    path = os.path.join(EXAMPLES, fname)
    with open(path, "r", encoding="utf-8") as fh:
        wf = json.load(fh)

    nodes_by_id = {n["id"]: n for n in wf["nodes"]}
    gen_nodes = [n for n in wf["nodes"] if n["type"] == "RunningHubLatentGenerator"]
    if len(gen_nodes) != 1:
        fail(f"{fname}: 应恰好包含 1 个主节点，实际 {len(gen_nodes)}")
        continue
    g = gen_nodes[0]

    got = len(g.get("widgets_values", []))
    if got != len(expected_widgets):
        fail(f"{fname}: 主节点 widget 数量 {got} != 定义 {len(expected_widgets)}")
    else:
        good(f"{fname}: 主节点 widget 数量一致（{got}）")

    outs = g.get("outputs", [])
    if len(outs) != n_returns:
        fail(f"{fname}: 输出槽位 {len(outs)} != RETURN_TYPES {n_returns}")
    else:
        good(f"{fname}: 输出槽位数量一致（{n_returns}）")

    # link 引用完整性：每条 link 的两端节点都必须存在
    for lk in wf.get("links", []):
        lid, src_id, _src_slot, dst_id, _dst_slot, _t = lk
        if src_id not in nodes_by_id:
            fail(f"{fname}: link {lid} 的源节点 {src_id} 不存在")
        if dst_id not in nodes_by_id:
            fail(f"{fname}: link {lid} 的目标节点 {dst_id} 不存在")
    good(f"{fname}: {len(wf.get('links', []))} 条连线的端点均有效")

    # 主节点的 latent 输出必须真的连出去了，否则工作流毫无意义
    latent_slot = next((o for o in outs if o["name"] == "latent"), None)
    if latent_slot and not latent_slot.get("links"):
        fail(f"{fname}: 主节点的 latent 输出未连接下游")
    else:
        good(f"{fname}: latent 已连接下游")

print("\n=== 4. 必需交付文件齐备 ===")
required = ["README.md", "LICENSE", "CHANGELOG.md", "requirements.txt",
            "config.example.json", ".gitignore", "__init__.py", "nodes.py"]
for f in required:
    p = os.path.join(PLUGIN, f)
    if os.path.isfile(p) and os.path.getsize(p) > 0:
        good(f)
    else:
        fail(f"缺失或为空：{f}")

print("\n=== 5. 源码中不得出现硬编码密钥 ===")
import re

secretish = re.compile(r"(api_?key|apikey)\s*[=:]\s*['\"](?![\"']?\{?[A-Z_]*\}?['\"]?$)[A-Za-z0-9]{20,}['\"]", re.I)
found = False
for dirpath, dirnames, filenames in os.walk(PLUGIN):
    dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git")]
    for fn in filenames:
        if not fn.endswith(".py"):
            continue
        p = os.path.join(dirpath, fn)
        for i, line in enumerate(open(p, encoding="utf-8"), 1):
            # config.example.json 里的占位说明不算
            if secretish.search(line) and "config" not in line.lower():
                fail(f"{p}:{i} 疑似硬编码密钥")
                found = True
if not found:
    good("未发现硬编码密钥")

print("\n" + "=" * 60)
print("全部检查通过" if ok else "存在失败项，需修复")
sys.exit(0 if ok else 1)
