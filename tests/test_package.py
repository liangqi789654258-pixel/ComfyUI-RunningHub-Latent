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
from rhplugin.nodes import RunningHubLatentGenerator as GenPro
from rhplugin.nodes import RunningHubLatentGeneratorSimple as GenSimple

# 所有 required 输入都会成为 widget（本插件没有复杂类型输入）
MAIN_NODES = {
    "RunningHubLatentGenerator": GenPro,
    "RunningHubLatentGeneratorSimple": GenSimple,
}

if not os.path.isdir(EXAMPLES):
    fail(f"缺少 examples 目录：{EXAMPLES}")
    example_files = []
else:
    example_files = sorted(f for f in os.listdir(EXAMPLES) if f.endswith(".json"))

if not example_files:
    fail("examples 目录下没有任何工作流")

for fname in example_files:
    path = os.path.join(EXAMPLES, fname)
    with open(path, "r", encoding="utf-8") as fh:
        wf = json.load(fh)

    nodes_by_id = {n["id"]: n for n in wf["nodes"]}
    gen_nodes = [n for n in wf["nodes"] if n["type"] in MAIN_NODES]
    if len(gen_nodes) != 1:
        fail(f"{fname}: 应恰好包含 1 个主节点（极简版或专业版），实际 {len(gen_nodes)}")
        continue
    g = gen_nodes[0]

    cls = MAIN_NODES[g["type"]]
    expected_widgets = list(cls.INPUT_TYPES()["required"].keys())
    n_returns = len(cls.RETURN_TYPES)

    got = len(g.get("widgets_values", []))
    if got != len(expected_widgets):
        fail(
            f"{fname}: {g['type']} widget 数量 {got} != 定义 {len(expected_widgets)}"
            f"（定义顺序：{expected_widgets}）"
        )
    else:
        good(f"{fname}: {g['type']} widget 数量一致（{got}）")

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
            "config.example.json", "config.example.ini", ".gitignore",
            "__init__.py", "nodes.py", "web/rh_warning.js"]
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

print("\n=== 6. 示例覆盖度与前后端契约 ===")
covered = {n["type"] for f in example_files
           for n in json.load(open(os.path.join(EXAMPLES, f), encoding="utf-8"))["nodes"]
           if n["type"] in MAIN_NODES}
for node_type in MAIN_NODES:
    if node_type in covered:
        good(f"示例已覆盖 {node_type}")
    else:
        fail(f"没有任何示例工作流使用 {node_type}")

# Python 发警告用的事件名，必须与 web/rh_warning.js 监听的名字完全一致，
# 否则「节点变黄」会静默失效——这正是本次改造要消灭的那类问题。
from rhplugin import nodes as nodes_mod

event_name = getattr(nodes_mod, "_WARNING_EVENT", "")
js_path = os.path.join(PLUGIN, "web", "rh_warning.js")
if event_name and os.path.isfile(js_path):
    js = open(js_path, encoding="utf-8").read()
    if event_name in js:
        good(f"前后端警告事件名一致（{event_name}）")
    else:
        fail(f"web/rh_warning.js 未监听事件名 {event_name}")
else:
    fail("缺少警告事件名或 web/rh_warning.js")

# WEB_DIRECTORY 必须声明，否则前端脚本根本不会被注入
if getattr(plugin, "WEB_DIRECTORY", None) == "web":
    good("WEB_DIRECTORY 已声明")
else:
    fail("WEB_DIRECTORY 未声明，前端扩展不会被加载")

# 配置文件必须被 .gitignore 排除
gi = open(os.path.join(PLUGIN, ".gitignore"), encoding="utf-8").read()
for entry in ("config.json", "config.ini"):
    if entry in gi:
        good(f".gitignore 已排除 {entry}")
    else:
        fail(f".gitignore 未排除 {entry}，密钥有泄漏风险")

print("\n" + "=" * 60)
print("全部检查通过" if ok else "存在失败项，需修复")
sys.exit(0 if ok else 1)
