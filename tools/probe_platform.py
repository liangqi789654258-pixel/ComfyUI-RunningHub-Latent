#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""S1 平台契约探测器（RunningHub）

用途
----
用**最小代价**核实 `docs/platform-contract.md` 中尚未定论的平台行为，
输出结构化报告，供 S0（fixture）、S2（Snapshot）、S5（求解器不变量）使用。

分层（默认只跑零成本层；涉及计费的层必须显式加 --confirm-cost）
----------------------------------------------------------------
  L0  拉取工作流结构                    成本 0      统计节点数 / _meta 覆盖率 / 字面量与连线字段
  L1  注入必然不存在的 nodeId           成本 0*     期望 803 硬失败且不返回 taskId
  L2  注入真实但被连线接管的字段        成本 0*     观察是硬失败还是被静默接受
  L3  注入字面量文本字段 + addMetadata  成本 1 次任务  事后读图片元数据验证是否真生效
  L4  用 workflow 参数提交整份 JSON     成本 1 次任务  验证「整图改写」候选是否可行

  成本 0*：判据是「响应中是否出现 taskId」。没有 taskId 即未创建任务、不计费。
  脚本每一步都会标注是否返回了 taskId；一旦某步返回了 taskId，
  后续探测会**自动中止**并提示，避免意外扣费。

用法
----
  # 只跑零成本层（推荐先跑这个）
  python tools/probe_platform.py --workflow-id <ID>

  # 指定要跑的层
  python tools/probe_platform.py --workflow-id <ID> --levels 0,1,2

  # 跑计费层（会先打印风险提示并要求确认）
  python tools/probe_platform.py --workflow-id <ID> --levels 3 --confirm-cost

API Key 通过环境变量 RUNNINGHUB_API_KEY 传入（避免写进命令行历史），
或用 --api-key 传入。任何输出中的密钥都会被打码。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except ImportError:
    print("缺少 requests：pip install requests")
    sys.exit(1)

BASE_URL = os.environ.get("RUNNINGHUB_BASE_URL", "https://www.runninghub.cn").rstrip("/")
HOST = re.sub(r"^https?://", "", BASE_URL)

# 已知错误码（官方 apifox 表 https://qpsnl2kplc.apifox.cn/doc-8417467）
KNOWN_CODES = {
    0: "成功",
    301: "PARAMS_INVALID 参数错误",
    380: "WORKFLOW_NOT_EXISTS 工作流不存在",
    412: "TOKEN_INVALID（官方定义为 API 路径拼写错误）",
    415: "TASK_INSTANCE_MAXED 独占型机器不足",
    416: "钱包余额不足",
    421: "TASK_QUEUE_MAXED 并发上限",
    423: "未找到指定任务",
    433: "VALIDATE_PROMPT_FAILED 工作流校验未通过",
    435: "未找到任务用户 API 实例（48G 需 instanceType=plus）",
    436: "独占会员到期",
    500: "UNKNOWN_ERROR",
    801: "免费用户不支持 API Key",
    802: "APIKEY_UNAUTHORIZED Key 未授权/失效",
    803: "APIKEY_INVALID_NODE_INFO nodeInfoList 不匹配（nodeId/fieldName 与定义不一致）",
    804: "任务运行中",
    805: "任务状态异常",
    806: "未找到对应用户",
    807: "未找到对应任务",
    810: "WORKFLOW_NOT_SAVED_OR_NOT_RUNNING 需先保存并手动运行一次",
    813: "任务已排队",
    1101: "Node info error 工作流节点数据解析错误",
}


def mask(text: Any) -> str:
    """打码所有 >=24 位的连续字母数字串，防止密钥泄漏到日志/终端。"""
    return re.sub(r"[A-Za-z0-9]{24,}", lambda m: m.group()[:4] + "****", str(text))


class Probe:
    def __init__(self, api_key: str, timeout: float = 60.0) -> None:
        self.api_key = (api_key or "").strip()
        self.timeout = timeout
        self.session = requests.Session()
        self.report: Dict[str, Any] = {
            "base_url": BASE_URL,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "levels": {},
        }
        self.billed = False  # 一旦出现 taskId 置 True

    # ---------------- 基础请求 ----------------

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Host": HOST,
            "Authorization": f"Bearer {self.api_key}",
        }

    def _post(self, path: str, payload: Dict[str, Any]) -> Tuple[Optional[int], Any, str]:
        """返回 (code, data, 原始 msg)。网络异常时 code=None。"""
        url = f"{BASE_URL}{path}"
        try:
            resp = self.session.post(
                url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=self._headers(),
                timeout=self.timeout,
            )
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:
            return None, None, mask(exc)
        if not isinstance(body, dict):
            return None, body, "响应不是 JSON 对象"
        return body.get("code"), body.get("data"), mask(body.get("msg", ""))

    def create(self, workflow_id: str, node_info_list: Optional[List[Dict]] = None,
               extra: Optional[Dict[str, Any]] = None) -> Tuple[Optional[int], Any, str]:
        payload: Dict[str, Any] = {"apiKey": self.api_key, "workflowId": workflow_id}
        if node_info_list is not None:
            payload["nodeInfoList"] = node_info_list
        if extra:
            payload.update(extra)
        return self._post("/task/openapi/create", payload)

    # ---------------- 结构解析 ----------------

    @staticmethod
    def extract_prompt(data: Any) -> Dict[str, Any]:
        """从 getJsonApiFormat 的多种响应形态里取出 {nodeId: {...}}。"""
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                return {}
        if not isinstance(data, dict):
            return {}
        if "prompt" in data:
            inner = data["prompt"]
            if isinstance(inner, str):
                try:
                    inner = json.loads(inner)
                except Exception:
                    return {}
            if isinstance(inner, dict):
                return inner
        # 已经是主体
        for v in data.values():
            if isinstance(v, dict) and ("class_type" in v or "inputs" in v):
                return data
        return {}

    @staticmethod
    def classify_fields(prompt: Dict[str, Any]) -> Dict[str, Any]:
        """统计字面量字段 / 连线字段 / _meta.title 覆盖率。"""
        literal: List[Tuple[str, str, str]] = []
        linked: List[Tuple[str, str, List]] = []
        titled = 0
        for node_id, meta in prompt.items():
            if not isinstance(meta, dict):
                continue
            if (meta.get("_meta") or {}).get("title"):
                titled += 1
            inputs = meta.get("inputs")
            if not isinstance(inputs, dict):
                continue
            for field, value in inputs.items():
                if isinstance(value, list):
                    linked.append((str(node_id), str(field), value))
                elif isinstance(value, str):
                    literal.append((str(node_id), str(field), value))
        return {
            "node_count": len(prompt),
            "title_coverage": f"{titled}/{len(prompt)}" if prompt else "0/0",
            "literal_count": len(literal),
            "linked_count": len(linked),
            "literal_sample": [
                {"nodeId": n, "fieldName": f, "value": (v[:60] + "...") if len(v) > 60 else v}
                for n, f, v in literal[:8]
            ],
            "linked_sample": [
                {"nodeId": n, "fieldName": f, "link": v} for n, f, v in linked[:8]
            ],
        }

    # ---------------- 各层探测 ----------------

    def level0(self, workflow_id: str) -> None:
        print("\n=== L0 拉取工作流结构（成本 0）===")
        code, data, msg = self._post(
            "/api/openapi/getJsonApiFormat",
            {"apiKey": self.api_key, "workflowId": workflow_id},
        )
        if code != 0:
            print(f"  拉取失败 code={code} ({KNOWN_CODES.get(code, '未知')}) msg={msg}")
            self.report["levels"]["L0"] = {"ok": False, "code": code, "msg": msg}
            return
        prompt = self.extract_prompt(data)
        stats = self.classify_fields(prompt)
        print(f"  节点数：{stats['node_count']}")
        print(f"  _meta.title 覆盖率：{stats['title_coverage']}")
        print(f"  字面量字段：{stats['literal_count']}   连线字段：{stats['linked_count']}")
        print("  字面量样例（可作为注入落点）：")
        for s in stats["literal_sample"][:5]:
            print(f"    nodeId={s['nodeId']:<6} field={s['fieldName']:<24} value={s['value'][:50]!r}")
        print("  连线样例（不应注入）：")
        for s in stats["linked_sample"][:5]:
            print(f"    nodeId={s['nodeId']:<6} field={s['fieldName']:<24} link={s['link']}")
        self.report["levels"]["L0"] = {"ok": True, "stats": stats, "prompt": prompt}

    def _guard(self) -> bool:
        if self.billed:
            print("  ⛔ 上一探测已产生 taskId（可能已计费），按策略中止后续探测。")
            return False
        return True

    def _report_create(self, label: str, code: Optional[int], data: Any, msg: str) -> Optional[str]:
        task_id = None
        if isinstance(data, dict):
            task_id = data.get("taskId")
        prompt_tips = None
        if isinstance(data, dict) and data.get("promptTips"):
            try:
                prompt_tips = json.loads(data["promptTips"]) if isinstance(data["promptTips"], str) else data["promptTips"]
            except Exception:
                prompt_tips = {"_raw": mask(data["promptTips"])[:300]}
        if task_id:
            self.billed = True

        print(f"  code={code} ({KNOWN_CODES.get(code, '未知/未见文档')})")
        print(f"  msg={msg[:160]}")
        print(f"  是否返回 taskId：{'是 ⚠️ 已创建任务，可能已计费' if task_id else '否（未创建任务，未计费）'}")
        if prompt_tips is not None:
            print(f"  promptTips.result = {prompt_tips.get('result')}")
            print(f"  promptTips.node_errors = {json.dumps(prompt_tips.get('node_errors'), ensure_ascii=False)[:300]}")
            print(f"  promptTips.outputs_to_execute = {prompt_tips.get('outputs_to_execute')}")
        self.report["levels"][label] = {
            "code": code,
            "code_meaning": KNOWN_CODES.get(code, "未知"),
            "msg": msg,
            "has_task_id": bool(task_id),
            "task_id": task_id,
            "prompt_tips": prompt_tips,
        }
        return task_id

    def level1(self, workflow_id: str) -> None:
        print("\n=== L1 注入不存在的 nodeId（成本 0，期望 803 硬失败）===")
        if not self._guard():
            return
        node_info = [{"nodeId": "__probe_nonexistent__", "fieldName": "text", "fieldValue": "probe"}]
        code, data, msg = self.create(workflow_id, node_info)
        tid = self._report_create("L1", code, data, msg)
        if code == 803 and not tid:
            print("  ✅ 结论：提交阶段即硬失败且不创建任务 → 显式绑定路线可做到零静默失效")
        elif code == 0 and tid:
            print("  ⚠️ 意外：无效 nodeId 被接受并创建了任务 → 平台不做存在性校验，")
            print("     这对架构影响很大（无法依赖 803 做负向验证），请记录并复核")
        else:
            print(f"  ℹ️ 其他返回：code={code}，请结合错误码表判读")

    def level2(self, workflow_id: str) -> None:
        print("\n=== L2 注入被连线接管的字段（关键未知项）===")
        if not self._guard():
            return
        l0 = self.report.get("levels", {}).get("L0") or {}
        prompt = l0.get("prompt") or {}
        linked = self.classify_fields(prompt).get("linked_sample") or []
        if not linked:
            print("  跳过：L0 未取到结构或没有连线字段。请先跑 L0。")
            return
        pick = linked[0]
        print(f"  选取连线字段：nodeId={pick['nodeId']} field={pick['fieldName']} link={pick['link']}")
        node_info = [{"nodeId": pick["nodeId"], "fieldName": pick["fieldName"], "fieldValue": "__probe_linked__"}]
        code, data, msg = self.create(workflow_id, node_info)
        tid = self._report_create("L2", code, data, msg)
        print("  判读：")
        if code in (803, 433, 1101) and not tid:
            print("    → 平台**拒绝**向连线字段注入（硬失败）。不变量 A 应永久强制。")
        elif code == 0 and not tid:
            print("    → 异常：成功但没有 taskId，请复核响应体。")
        elif tid:
            print("    → 平台**接受**了注入并创建任务。此时需 L3 确认运行时是否真的生效：")
            print("       若生效 → 可放宽不变量 A（但语义上是覆盖上游，需谨慎）；")
            print("       若不生效 → 属静默失效，不变量 A 必须永久强制。")
            print(f"    → 记下 taskId 以便查询结果：{tid}")

    def level3(self, workflow_id: str, marker: str = "") -> None:
        print("\n=== L3 字面量字段注入 + addMetadata（成本：1 次任务）===")
        if not self._guard():
            return
        l0 = self.report.get("levels", {}).get("L0") or {}
        prompt = l0.get("prompt") or {}
        literal = self.classify_fields(prompt).get("literal_sample") or []
        if not literal:
            print("  跳过：L0 未取到字面量字段。")
            return
        pick = literal[0]
        marker = marker or f"__probe_{int(time.time())}__"
        print(f"  注入目标：nodeId={pick['nodeId']} field={pick['fieldName']}")
        print(f"  特征串：{marker}")
        node_info = [{"nodeId": pick["nodeId"], "fieldName": pick["fieldName"], "fieldValue": marker}]
        code, data, msg = self.create(workflow_id, node_info, extra={"addMetadata": True})
        tid = self._report_create("L3", code, data, msg)
        if tid:
            print("  下一步：轮询 /task/openapi/outputs 取结果，下载图片后用")
            print("  PIL 或 exiftool 读取 PNG 元数据，确认特征串是否出现在提示词字段中。")
            print(f"  特征串：{marker}")
            self.report["levels"]["L3"]["marker"] = marker

    def level4(self, workflow_id: str) -> None:
        print("\n=== L4 workflow 参数（整图改写）可行性（成本：1 次任务）===")
        if not self._guard():
            return
        l0 = self.report.get("levels", {}).get("L0") or {}
        prompt = l0.get("prompt") or {}
        if not prompt:
            print("  跳过：L0 未取到结构。")
            return
        print(f"  提交原样 JSON（{len(prompt)} 个节点）作为 workflow 参数")
        code, data, msg = self.create(
            workflow_id, None,
            extra={"workflow": json.dumps(prompt, ensure_ascii=False)},
        )
        tid = self._report_create("L4", code, data, msg)
        if tid:
            print("  → workflow 参数被接受。架构候选 X（整图改写）可行，")
            print("    但需评估「本地改写版本与云端版本漂移」的新风险。")


def main() -> int:
    ap = argparse.ArgumentParser(description="RunningHub 平台契约探测器（S1）")
    ap.add_argument("--workflow-id", default=os.environ.get("RUNNINGHUB_WORKFLOW_ID", ""),
                    help="要探测的工作流 ID（也可用环境变量 RUNNINGHUB_WORKFLOW_ID）")
    ap.add_argument("--api-key", default=os.environ.get("RUNNINGHUB_API_KEY", ""),
                    help="API Key（建议用环境变量 RUNNINGHUB_API_KEY，避免留在命令历史）")
    ap.add_argument("--levels", default="0,1,2", help="要跑的层，逗号分隔，默认 0,1,2（零成本层）")
    ap.add_argument("--confirm-cost", action="store_true",
                    help="确认执行会产生计费的层（L3/L4）")
    ap.add_argument("--out", default="", help="报告输出 JSON 路径（默认只打印到终端）")
    args = ap.parse_args()

    if not args.api_key:
        print("缺少 API Key。请设置环境变量 RUNNINGHUB_API_KEY，或用 --api-key 传入。")
        return 2
    if not args.workflow_id:
        print("缺少 workflow_id。请用 --workflow-id 传入，或设置环境变量 RUNNINGHUB_WORKFLOW_ID。")
        return 2

    levels = [x.strip() for x in args.levels.split(",") if x.strip()]
    costly = [lv for lv in levels if lv in ("3", "4")]
    if costly and not args.confirm_cost:
        print(f"⚠️ L{','.join(costly)} 会产生真实计费（各 1 次任务）。")
        print("   确认继续请加 --confirm-cost；只想跑零成本层请用 --levels 0,1,2")
        return 2

    probe = Probe(args.api_key)
    print(f"目标：{BASE_URL}   工作流：{args.workflow_id}   层：{','.join(levels)}")
    if costly:
        print(f"⚠️ 已确认：L{','.join(costly)} 将产生计费")

    if "0" in levels:
        probe.level0(args.workflow_id)
    if "1" in levels:
        probe.level1(args.workflow_id)
    if "2" in levels:
        probe.level2(args.workflow_id)
    if "3" in levels:
        probe.level3(args.workflow_id)
    if "4" in levels:
        probe.level4(args.workflow_id)

    probe.report["billed"] = probe.billed
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(probe.report, fh, ensure_ascii=False, indent=1)
        print(f"\n报告已写出：{args.out}")

    print("\n=== 结论速记 ===")
    for lv in levels:
        r = probe.report["levels"].get(f"L{lv}")
        if not r:
            continue
        print(f"  L{lv}: code={r.get('code')} ({r.get('code_meaning')}) "
              f"has_task_id={r.get('has_task_id')}")
    print("\n把本输出贴回 docs/platform-contract.md 的「残余未知」章节，")
    print("并据此确定 S5 是否必须永久强制不变量 A。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
