#!/usr/bin/env python
"""分享前自检：确认要发给别人的这一份插件里没有你的凭证 / 私有数据。

用法（在插件目录下）：

    python tools/check_share_ready.py
    python tools/check_share_ready.py --fix   # 自动清掉本机的 config.ini / 缓存

判据只有一条：**别人拿到这份目录，能不能顺走你的 API Key 或历史记录。**
git 分享（clone）本来就安全，因为 .gitignore 排除了 config.ini；
但直接压缩目录发给别人时，config.ini 会被一起打进去——这是最常见的泄漏方式。
"""

from __future__ import annotations

import argparse
import os
import re
import sys

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 绝对不能出现在分享包里的东西（相对路径）—— --fix 会真的删除
FORBIDDEN_PATHS = [
    ("config.ini", "存有你的 API Key"),
    ("config.json", "旧格式配置，可能存有 API Key"),
    ("cache", "本地 latent 缓存（别人没用的私有产物）"),
    ("output", "任务台账 / 输出文件"),
]

# 只是体积噪音，提示一下即可（**绝不自动删除**：.git 删了就没了）
NOISY_PATHS = [
    ("__pycache__", "编译缓存，没必要带"),
    (".git", "仓库历史，压缩分享时建议排除（但绝不要删）"),
]

# 需要存在的发布文件
REQUIRED = ["README.md", "LICENSE", "requirements.txt", "nodes.py", "core/client.py"]

_KEY_LINE = re.compile(r"^\s*api_key\s*=\s*(\S+)\s*$", re.M)


def _dir_size_kb(path: str) -> float:
    total = 0
    for root, _dirs, names in os.walk(path):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(root, n))
            except OSError:
                pass
    return total / 1024.0


def main() -> int:
    ap = argparse.ArgumentParser(description="分享/发布前自检")
    ap.add_argument("--fix", action="store_true", help="直接删除本机专属文件（config.ini / 缓存）")
    args = ap.parse_args()

    problems: list[tuple[str, str, float]] = []

    for name, why in FORBIDDEN_PATHS:
        path = os.path.join(PLUGIN, name)
        if not os.path.exists(path):
            continue
        size = _dir_size_kb(path) if os.path.isdir(path) else os.path.getsize(path) / 1024.0
        problems.append((name, why, size))

    # 单独判断 config.ini 里到底有没有真 Key（空模板可以留着）
    ini = os.path.join(PLUGIN, "config.ini")
    has_key = False
    if os.path.isfile(ini):
        try:
            text = open(ini, encoding="utf-8-sig").read()
            for m in _KEY_LINE.finditer(text):
                if m.group(1).strip():
                    has_key = True
        except OSError:
            pass

    print(f"插件目录：{PLUGIN}\n")

    if problems:
        print("以下内容**不该**出现在分享包里（--fix 可清理）：")
        for name, why, size in problems:
            print(f"  ✗ {name:<14} {size:8.1f} KB   {why}")
        print()
    else:
        print("✓ 没有发现必须排除的文件")
        print()

    noisy = []
    for name, why in NOISY_PATHS:
        path = os.path.join(PLUGIN, name)
        if os.path.exists(path):
            noisy.append((name, why))
    if noisy:
        print("体积噪音（不影响安全，压缩时可排除）：")
        for name, why in noisy:
            print(f"  · {name:<14} {why}")
        print()

    if has_key:
        print("⚠ config.ini 里**确实存着**你的 API Key —— 直接压缩目录发出去就是凭证泄漏。")
        print("  压缩前请删除它（或运行本脚本加 --fix）。\n")

    missing = [f for f in REQUIRED if not os.path.isfile(os.path.join(PLUGIN, f))]
    if missing:
        print("✗ 缺少发布必需文件：" + "、".join(missing))
    else:
        print("✓ 发布必需文件齐备")

    # 顺带确认 .gitignore 仍然挡住了配置（防止有人手滑删掉）
    gi = os.path.join(PLUGIN, ".gitignore")
    if os.path.isfile(gi):
        text = open(gi, encoding="utf-8").read()
        guarded = [f for f in ("config.ini", "config.json") if f in text]
        print(("✓ .gitignore 已排除 " + "、".join(guarded)) if len(guarded) == 2
              else "✗ .gitignore 缺少 config.ini / config.json，用 git 分享也会泄漏")
    else:
        print("✗ 没有 .gitignore")

    if not problems and not has_key and not missing:
        print("\n可以分享了。")
        return 0

    if args.fix:
        removed = []
        for name, _why, _size in problems:
            path = os.path.join(PLUGIN, name)
            try:
                if os.path.isdir(path):
                    import shutil

                    shutil.rmtree(path)
                else:
                    os.remove(path)
                removed.append(name)
            except OSError as exc:
                print(f"  删除 {name} 失败：{exc}")
        if removed:
            print("\n已清理：" + "、".join(removed) + "（config.ini 里若有 Key，记得先备份）")
            return 0

    print("\n处理建议：")
    print("  * 用 git 分享：config.ini / config.json 已被 .gitignore 排除，安全。")
    print("  * 用 zip 分享：先执行 python tools/check_share_ready.py --fix。")
    print("  * 已经发出去过？去 RunningHub 控制台**吊销并重新生成** API Key。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
