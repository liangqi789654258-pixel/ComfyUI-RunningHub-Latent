#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
S0 · 基线快照工具：把「当前实现」在离线 fixture 上的表现固化成可回放的基线。

## 它是干什么用的

重构（S3–S9）改的是「怎么找注入落点」，但改之前必须先把**现在是怎么表现的**
钉死：哪些形态能命中、哪些形态 0 命中、0 命中时给出的诊断文案是什么。
没有这份基线，重构就无从证明「改好了」还是「改坏了」。

## 怎么用

    python tools/capture_baseline.py            # 默认即 --check：与基线比对，不一致 exit 1
    python tools/capture_baseline.py --check    # CI 模式，同上（显式写法）
    python tools/capture_baseline.py --update   # 用当前实现重写 baselines/

## 输入 / 输出

  * 输入：`tests/fixtures/workflows/<case_id>.json`
    —— 每个文件模拟一份 RunningHub `getJsonApiFormat` 的真实响应
    （`data.prompt` 是 JSON 字符串，节点带 `_meta.title`，连线是 `[源节点, 槽位]`）。
  * 输出：`tests/fixtures/baselines/<case_id>.json`
    —— 用固定哨兵值跑一遍当前实现，记录命中数、注入项、种子落点、诊断文案。

## 确定性约定（基线必须能进 git、能 diff）

  * 字典键排序写入；注入项按 `core.workflow._node_sort_key` 排序；
  * 不写入时间戳、随机值、绝对路径或任何平台相关信息；
  * `injections[].fieldValue` 记录**实际会被提交上去的值** —— 本项目的红线是
    「必须让用户看到真正送上去的是什么」，基线同样不能含糊。

不联网、不调用 RunningHub API、不消耗任何额度。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

# tools/ 的上一级就是插件根目录
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.workflow import (  # noqa: E402  （必须先注入 sys.path）
    _node_sort_key,
    build_prompt_injections,
    find_seed_targets,
    normalize,
    placeholder_hits,
    prompt_link_diagnosis,
)

WORKFLOWS_DIR = os.path.join(ROOT, "tests", "fixtures", "workflows")
BASELINES_DIR = os.path.join(ROOT, "tests", "fixtures", "baselines")

# 哨兵值：刻意做成不可能与 fixture 里的任何真实文本重复的字符串，
# 这样「命中/未命中」一眼可辨，也不会把 fixture 内容混进基线。
POSITIVE_SENTINEL = "<<POS_SENTINEL>>"
NEGATIVE_SENTINEL = "<<NEG_SENTINEL>>"

SCHEMA_VERSION = 1
TOKENS = ("positive", "negative")


# ---------------------------------------------------------------------------
# 读取 fixture
# ---------------------------------------------------------------------------


def load_fixtures(fixtures_dir: str = WORKFLOWS_DIR) -> List[Dict[str, Any]]:
    """按文件名顺序加载全部 fixture（顺序固定，保证输出可复现）。"""
    out: List[Dict[str, Any]] = []
    if not os.path.isdir(fixtures_dir):
        return out
    for name in sorted(os.listdir(fixtures_dir)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(fixtures_dir, name), "r", encoding="utf-8") as fh:
            out.append(json.load(fh))
    return out


def raw_prompt(fixture: Dict[str, Any]) -> Dict[str, Any]:
    """取出 fixture 里的原始 API-format 节点字典（未归一化）。

    用途：基线记录的是「归一化后实现看到的结构」，但校验时需要拿原始结构
    核对节点数与字段形态（比如判断某个注入目标是不是 `[]` 连线）。
    """
    data = (fixture.get("api_response") or {}).get("data") or {}
    text = data.get("prompt")
    if isinstance(text, str):
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return text if isinstance(text, dict) else {}


# ---------------------------------------------------------------------------
# 回放当前实现
# ---------------------------------------------------------------------------


def _canonical_injections(injections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把注入项整理成稳定顺序：节点序 → 字段名 → 值。

    提交顺序对平台没有语义影响，但基线要能 diff，所以必须规整。
    """
    items = [
        {
            "nodeId": str(item.get("nodeId", "")),
            "fieldName": str(item.get("fieldName", "")),
            "fieldValue": item.get("fieldValue"),
        }
        for item in injections
    ]
    return sorted(
        items,
        key=lambda d: (
            _node_sort_key(d["nodeId"]),
            d["fieldName"],
            json.dumps(d["fieldValue"], sort_keys=True, ensure_ascii=False, default=str),
        ),
    )


def build_record(fixture: Dict[str, Any]) -> Dict[str, Any]:
    """对单个 fixture 跑一遍当前实现，产出基线记录。"""
    structure = normalize(fixture.get("api_response"))
    injections, stats = build_prompt_injections(
        structure, POSITIVE_SENTINEL, NEGATIVE_SENTINEL
    )

    string_fields = 0
    link_fields = 0
    for meta in structure.values():
        inputs = meta.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for value in inputs.values():
            if isinstance(value, str):
                string_fields += 1
            elif isinstance(value, list):
                link_fields += 1

    return {
        "case_id": str(fixture.get("case_id", "")),
        "schema_version": SCHEMA_VERSION,
        "node_count": len(structure),
        "string_field_count": string_fields,
        "link_field_count": link_fields,
        "placeholder_hits": {
            token: int(placeholder_hits(structure).get(token, 0)) for token in TOKENS
        },
        "hit_stats": {token: int(stats.get(token, 0)) for token in TOKENS},
        "injections": _canonical_injections(injections),
        "seed_targets": [[str(n), str(f)] for n, f in find_seed_targets(structure)],
        "diagnosis": {
            token: prompt_link_diagnosis(structure, token) for token in TOKENS
        },
        "expect_current": dict(fixture.get("expect_current") or {}),
    }


def replay(fixture: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """返回 (归一化结构, 基线记录)。不变量 A 之类的检查需要原始结构。"""
    return normalize(fixture.get("api_response")), build_record(fixture)


def dumps(record: Dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# 比对
# ---------------------------------------------------------------------------


def diff_records(
    expected: Any, actual: Any, path: str = "", out: Optional[List[str]] = None
) -> List[str]:
    """逐字段比对两条记录，返回人类可读的差异行（无差异则为空列表）。"""
    if out is None:
        out = []

    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            if key not in expected:
                out.append(f"{path}.{key}：基线缺失，实际为 {actual[key]!r}")
            elif key not in actual:
                out.append(f"{path}.{key}：实际缺失，基线为 {expected[key]!r}")
            else:
                diff_records(expected[key], actual[key], f"{path}.{key}", out)
        return out

    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            out.append(f"{path}：元素个数 {len(expected)} → {len(actual)}")
        for idx in range(min(len(expected), len(actual))):
            diff_records(expected[idx], actual[idx], f"{path}[{idx}]", out)
        return out

    if expected != actual:
        out.append(f"{path}：{expected!r} → {actual!r}")
    return out


def run_update(fixtures: List[Dict[str, Any]]) -> int:
    os.makedirs(BASELINES_DIR, exist_ok=True)
    for fixture in fixtures:
        case_id = str(fixture.get("case_id", ""))
        path = os.path.join(BASELINES_DIR, case_id + ".json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(dumps(build_record(fixture)))
        record = build_record(fixture)
        print(
            f"  [WRITE] {case_id}.json  节点 {record['node_count']}  "
            f"命中 正{record['hit_stats']['positive']}/负{record['hit_stats']['negative']}  "
            f"注入 {len(record['injections'])} 条"
        )
    print(f"\n已写入 {len(fixtures)} 份基线到 {os.path.relpath(BASELINES_DIR, ROOT)}")
    return 0


def run_check(fixtures: List[Dict[str, Any]]) -> int:
    failed = 0
    for fixture in fixtures:
        case_id = str(fixture.get("case_id", ""))
        path = os.path.join(BASELINES_DIR, case_id + ".json")
        actual = build_record(fixture)
        if not os.path.isfile(path):
            failed += 1
            print(f"  [FAIL] {case_id}：缺少基线文件（先跑 --update）")
            continue

        with open(path, "r", encoding="utf-8") as fh:
            expected = json.load(fh)
        lines = diff_records(expected, actual, case_id)
        if lines:
            failed += 1
            print(f"  [FAIL] {case_id}：与基线不一致")
            for line in lines:
                print(f"         {line}")
        else:
            print(
                f"  [OK]   {case_id}  命中 正{actual['hit_stats']['positive']}"
                f"/负{actual['hit_stats']['negative']}  注入 {len(actual['injections'])} 条"
            )

    print("\n" + "=" * 60)
    total = len(fixtures)
    if failed:
        print(f"基线比对失败：{total} 个 case 中 {failed} 个与基线不一致")
        print("若本次改动是有意为之，请重跑 --update 并把新的基线一并提交")
        return 1
    print(f"基线比对通过：{total} 个 case 与基线完全一致")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="S0 基线快照：固化当前实现在离线 fixture 上的表现"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="CI 模式：与 baselines/ 比对，不一致则 exit 1（默认行为）",
    )
    parser.add_argument(
        "--update", action="store_true", help="用当前实现重写 baselines/"
    )
    args = parser.parse_args(argv)

    fixtures = load_fixtures()
    if not fixtures:
        print(f"没有找到任何 fixture：{WORKFLOWS_DIR}")
        return 1

    print(f"共 {len(fixtures)} 个 fixture，哨兵值 positive={POSITIVE_SENTINEL} "
          f"negative={NEGATIVE_SENTINEL}\n")
    if args.update:
        return run_update(fixtures)
    return run_check(fixtures)


if __name__ == "__main__":
    sys.exit(main())
