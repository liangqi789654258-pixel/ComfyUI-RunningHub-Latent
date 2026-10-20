#!/usr/bin/env python
"""一条命令打出可发布的开源包。

产出结构（单仓库，插件 + 桌面出图器）：

    dist/RH-Latent-v1.4.0/
    ├── __init__.py / nodes.py / core/ / web/ ...   ← ComfyUI 插件
    └── desktop/                                     ← RH 出图器（独立启动）

用法：

    python tools/build_release.py                    # 打包 + 压缩
    python tools/build_release.py --no-zip           # 只准备目录，不压缩
    python tools/build_release.py --desktop-src D:\\other\\RH-Standalone

脚本会做三件事，任何一项不满足就**直接失败**，不会产出一个"差不多能用"的包：
  1. 拒绝把本机凭证 / 私有产物带进去（config.ini、cache、output、data）
  2. 扫描所有文本文件，确认里面没有你本机配置的 API Key
  3. 校验必需文件齐备
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import zipfile

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 仓库自带的 desktop/ 是发布源；没有时才回退到作者的开发目录
IN_REPO_DESKTOP = os.path.join(PLUGIN, "desktop")
DEFAULT_DESKTOP = IN_REPO_DESKTOP if os.path.isdir(IN_REPO_DESKTOP)     else r"C:\AI\dongshan\RH-Standalone"

# 插件侧要排除的（相对插件根的路径 / 目录名）
PLUGIN_IGNORE_DIRS = {
    ".git", "__pycache__", "dist", "cache", "output", ".venv", "venv",
    ".idea", ".vscode", "desktop",   # desktop 单独复制，要走自己的排除规则
}
PLUGIN_IGNORE_FILES = {"config.ini", "config.json", "*.pyc", "*.pyo", "*.log"}

# 桌面侧要排除的（本地运行产物）
DESKTOP_IGNORE_DIRS = {"__pycache__", "data", "output", "dist", ".venv", "venv",
                       ".git", ".idea", ".vscode"}
DESKTOP_IGNORE_FILES = {"*.pyc", "*.pyo", "*.log"}

REQUIRED_PLUGIN = ["__init__.py", "nodes.py", "README.md", "LICENSE",
                   "requirements.txt", "config.example.ini", "core/client.py",
                   "web/rh_key_guard.js", "docs/key-handling.md"]
REQUIRED_DESKTOP = ["main.py", "app/gui/main_window.py", "install.bat",
                    "requirements.txt", "README.md", "LICENSE"]

# 绝不接受出现在发布包里的东西（命中即失败）
FORBIDDEN_ANYWHERE = ["config.ini", "config.json", "runninghub_ledger.jsonl"]
FORBIDDEN_DIRS = {"__pycache__", "cache", "data"}

TEXT_EXT = {".py", ".js", ".json", ".md", ".ini", ".txt", ".bat", ".yaml", ".yml", ".css"}


def _should_skip_dir(name: str, ignore: set[str]) -> bool:
    return name in ignore


def _matches(name: str, patterns: set[str]) -> bool:
    return any(
        name == p or (p.startswith("*") and name.endswith(p[1:])) for p in patterns
    )


def _copy_tree(src: str, dst: str, ignore_dirs: set[str], ignore_files: set[str]) -> int:
    count = 0
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if not _should_skip_dir(d, ignore_dirs)]
        rel = os.path.relpath(root, src)
        target = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target, exist_ok=True)
        for fn in files:
            if _matches(fn, ignore_files):
                continue
            shutil.copy2(os.path.join(root, fn), os.path.join(target, fn))
            count += 1
    return count


def read_version() -> str:
    text = open(os.path.join(PLUGIN, "__init__.py"), encoding="utf-8").read()
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not m:
        raise SystemExit("✗ 读不到 __version__，先修 __init__.py")
    return m.group(1)


def configured_key() -> str:
    """读本机配置的 Key（用于泄漏扫描）；没有就返回空串。"""
    sys.path.insert(0, PLUGIN)
    try:
        from core import settings

        return str(settings.get("api_key") or "").strip()
    except Exception:
        return ""


def scan_for_key(root: str, key: str) -> list[str]:
    """扫描所有文本文件，返回含该 Key 的相对路径。"""
    if len(key) < 16:
        return []
    hits = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
        for fn in files:
            if os.path.splitext(fn)[1].lower() not in TEXT_EXT:
                continue
            p = os.path.join(dirpath, fn)
            try:
                if key in open(p, encoding="utf-8", errors="ignore").read():
                    hits.append(os.path.relpath(p, root))
            except OSError:
                pass
    return hits


def assert_clean(root: str) -> None:
    """发布包里不允许出现凭证与私有产物。"""
    problems = []
    for dirpath, dirs, files in os.walk(root):
        for d in list(dirs):
            if d in FORBIDDEN_DIRS:
                problems.append(os.path.relpath(os.path.join(dirpath, d), root) + "/")
        for fn in files:
            if fn in FORBIDDEN_ANYWHERE:
                problems.append(os.path.relpath(os.path.join(dirpath, fn), root))
    if problems:
        raise SystemExit("✗ 发布包里混进了不该有的东西：\n    " + "\n    ".join(problems))


def assert_required(root: str, items: list[str], label: str) -> None:
    missing = [i for i in items if not os.path.exists(os.path.join(root, i))]
    if missing:
        raise SystemExit(f"✗ {label} 缺少必需文件：{', '.join(missing)}")
    print(f"  ✓ {label} 必需文件齐备（{len(items)} 项）")


def main() -> int:
    ap = argparse.ArgumentParser(description="打开源发布包")
    ap.add_argument("--desktop-src", default=DEFAULT_DESKTOP,
                    help="出图器源码目录（默认用仓库里的 desktop/）")
    ap.add_argument("--out", default=os.path.join(PLUGIN, "dist"), help="产物目录")
    ap.add_argument("--no-zip", action="store_true", help="只准备目录，不压缩")
    args = ap.parse_args()

    version = read_version()
    name = f"RH-Latent-v{version}"
    staging = os.path.join(args.out, name)

    print(f"== 打包 {name} ==\n插件：{PLUGIN}")
    if not os.path.isdir(args.desktop_src):
        raise SystemExit(f"✗ 找不到出图器源码目录：{args.desktop_src}")
    print(f"出图器：{args.desktop_src}\n")

    # 每次从干净目录开始，避免上一轮的残留混进来
    if os.path.isdir(staging):
        shutil.rmtree(staging)
    os.makedirs(staging)

    print("[1/4] 复制插件")
    n_plugin = _copy_tree(PLUGIN, staging, PLUGIN_IGNORE_DIRS, PLUGIN_IGNORE_FILES)
    print(f"  ✓ {n_plugin} 个文件")

    print("[2/4] 复制出图器 → desktop/")
    desktop_dst = os.path.join(staging, "desktop")
    n_desk = _copy_tree(args.desktop_src, desktop_dst,
                        DESKTOP_IGNORE_DIRS, DESKTOP_IGNORE_FILES)
    print(f"  ✓ {n_desk} 个文件")

    print("[3/4] 安全与完整性校验")
    assert_required(staging, REQUIRED_PLUGIN, "插件")
    assert_required(desktop_dst, REQUIRED_DESKTOP, "出图器")
    assert_clean(staging)
    print("  ✓ 无 config.ini / cache / data / __pycache__")
    key = configured_key()
    leaks = scan_for_key(staging, key)
    if leaks:
        raise SystemExit("✗ 本机 API Key 出现在发布包里：\n    " + "\n    ".join(leaks))
    print("  ✓ 本机 API Key 未出现在任何文件中")

    if args.no_zip:
        print(f"\n目录已就绪：{staging}")
        return 0

    print("[4/4] 压缩")
    os.makedirs(args.out, exist_ok=True)
    zip_path = os.path.join(args.out, f"{name}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for root, dirs, files in os.walk(staging):
            dirs.sort()
            for fn in sorted(files):
                full = os.path.join(root, fn)
                zf.write(full, os.path.relpath(full, args.out))
    size_mb = os.path.getsize(zip_path) / 1024 / 1024
    print(f"  ✓ {zip_path}（{size_mb:.1f} MB，{n_plugin + n_desk} 个文件）")

    print(f"\n包内顶层结构：")
    for entry in sorted(os.listdir(staging)):
        print(f"  {entry}")
    print("\n发布前还剩两件事（脚本不会替你做）：")
    print("  1. git add/commit 并打 tag：" f"git tag -a v{version} -m 'v{version}'")
    print("  2. 把 zip 传到 Release，正文用 CHANGELOG 的 [1.4.0] 段落")
    return 0


if __name__ == "__main__":
    sys.exit(main())
