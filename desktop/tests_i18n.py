"""i18n 完整性测试：代码里用到的每个 key 都必须在中文表和英文表里同时存在。

为什么要有：`tr()` 在缺键时会**原样返回 key**，界面直接显示 `video_mode_btn_on`
这种裸 key（真实事故）。肉眼很难发现，只有切到对应界面才看得到，所以用测试兜住。

顺带保证两张表 key 集合一致——避免英文表漏翻，切英文时悄悄回退中文。
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import i18n  # noqa: E402

APP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app")

# i18n.tr("key") / i18n.tr('key')，允许中间有空白
_TR_CALL = re.compile(r"""i18n\.tr\(\s*["']([A-Za-z0-9_]+)["']""")
# 引擎层不直接依赖 i18n：进度/错误文案都以 key 形式交给调用方翻译，
# 所以这两种调用也必须一起校验，否则 key 打错字只会静默显示成 key 本身。
_KEY_CALL = re.compile(r"""\.(?:stage|t)\(\s*["']([A-Za-z0-9_]+)["']""")

used = {}
for root, _dirs, names in os.walk(APP_DIR):
    for name in names:
        if not name.endswith(".py"):
            continue
        path = os.path.join(root, name)
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        for key in _TR_CALL.findall(text) + _KEY_CALL.findall(text):
            used.setdefault(key, []).append(os.path.basename(path))

print(f"[OK] 代码中引用的 i18n key 共 {len(used)} 个")

zh, en = i18n._ZH, i18n._EN

missing_zh = sorted(k for k in used if k not in zh)
missing_en = sorted(k for k in used if k not in en)

if missing_zh:
    print("[FAIL] 中文表缺少以下 key（界面会显示裸 key）：")
    for k in missing_zh:
        print(f"        {k}    ← 用于 {', '.join(sorted(set(used[k])))}")
if missing_en:
    print("[FAIL] 英文表缺少以下 key（切英文时会回退中文）：")
    for k in missing_en:
        print(f"        {k}    ← 用于 {', '.join(sorted(set(used[k])))}")

# 两张表键集合必须一致
only_zh = sorted(set(zh) - set(en))
only_en = sorted(set(en) - set(zh))
if only_zh:
    print(f"[FAIL] 只在中文表里的 key：{only_zh}")
if only_en:
    print(f"[FAIL] 只在英文表里的 key：{only_en}")

# 未被使用的 key（只提示，不算失败：可能有动态拼接或暂时留用）
unused = sorted(set(zh) - set(used))
if unused:
    print(f"[WARN] 以下 {len(unused)} 个 key 未被直接引用（可能是动态拼接，或已废弃）：")
    print(f"        {unused}")

ok = not (missing_zh or missing_en or only_zh or only_en)
print("i18n 完整性检查通过" if ok else "i18n 检查失败")
sys.exit(0 if ok else 1)
