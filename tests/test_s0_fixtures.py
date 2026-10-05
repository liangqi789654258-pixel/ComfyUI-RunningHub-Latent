#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
S0 · fixture 回归测试：把「提示词注入失效」的根因钉死在可执行的形式上。

## 这份测试守的是什么

1. **fixture 自身是合法的**：模拟的是平台真实响应形态，字段齐全、truth 可核对；
2. **回放 == 基线**：当前实现在每个形态上的表现被逐字段固化，后续重构一旦
   悄悄改变行为（包括「误打误撞改好了」）都会在这里暴露；
3. **形态与预期一致**：`expect_current.status` 声明的形态必须真的表现成那样；
4. **不变量 A**：绝不向 `[]` 连线形态的字段注入（平台明确不建议修改）；
5. **禁止静默**：0 命中的形态必须给出带 nodeId 的中文诊断，而不是悄悄跑完。

测试全程离线：不联网、不调用 RunningHub API、不消耗任何额度。
"""

import importlib.util
import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.abspath(os.path.join(ROOT, ".."))  # tests/ 的上一级

# 复用 tools/capture_baseline.py 的回放逻辑，保证「测试看到的」与
# 「基线记录的」是同一套计算——否则两边各写一遍迟早漂移。
_spec = importlib.util.spec_from_file_location(
    "s0_capture_baseline", os.path.join(PLUGIN, "tools", "capture_baseline.py")
)
cap = importlib.util.module_from_spec(_spec)
sys.modules["s0_capture_baseline"] = cap
_spec.loader.exec_module(cap)

FIXTURES = cap.load_fixtures()
BASELINES_DIR = cap.BASELINES_DIR

STATUS_ALREADY_OK = "already_ok"
STATUS_SUSPECT = "injected_suspect"
STATUS_REGRESSION = "regression_open"
STATUS_AMBIGUOUS = "ambiguous_reject"
VALID_STATUS = (STATUS_ALREADY_OK, STATUS_SUSPECT, STATUS_REGRESSION, STATUS_AMBIGUOUS)

# injected_suspect 的点名清单：每条对应一条本项目的不变量 / 架构文档禁止项。
#
# 为什么用「显式字段 violation + 必须回现在 why 里」而不是「有注入就算 suspect」：
#   后者等于没加约束 —— 只要产生了注入就能标 suspect，那么这档状态和 already_ok
#   没有区别，S10 消费 fixture 时仍然分不清「真的对了」和「注入了但不成立」。
# 为什么同时要求 violation 落在本清单内、且必须是 why 的子串：
#   只校验「violation 非空」会退化成随手写一个词；只做关键词扫描 why 又会把
#   规则名散落在正文里、机器不可读（S10 要按违反项分组跑矩阵）。
#   两者结合后，fixture 必须**指名**一条已知规则，并且这条规则必须在人类可读的
#   why 里出现——机器可读与人工可读互为校验。新增形态时请同步扩充本清单。
KNOWN_VIOLATIONS = (
    "不变量 B",              # 可达性：不得向不会执行的节点（bypass/mute）注入
    "§3.5 唯一即默认采用",    # 架构文档 §3.5 禁止项：不得把「唯一」当默认落点
    "部分覆盖",              # 多源汇聚只替换了一路，且不上报「仅覆盖部分源」
    "同址重复注入",          # 同一 (nodeId, fieldName) 在 nodeInfoList 里重复出现
)

MIN_ASSERTIONS_PER_CASE = 4

# 断言计数：unittest 只统计用例数，本仓库的习惯是「打印断言数」，因此自己数。
ASSERTIONS = {"total": 0, "failed": 0, "by_case": {}}


def _count(case_id=None, failed=False):
    ASSERTIONS["total"] += 1
    if failed:
        ASSERTIONS["failed"] += 1
    if case_id:
        ASSERTIONS["by_case"][case_id] = ASSERTIONS["by_case"].get(case_id, 0) + 1


class FixtureTestCase(unittest.TestCase):
    """带断言计数的基类。"""

    def expect(self, cond, msg, case_id=None):
        _count(case_id, not cond)
        self.assertTrue(cond, msg)

    def expect_equal(self, got, want, msg, case_id=None):
        _count(case_id, got != want)
        self.assertEqual(got, want, msg)


def load_baseline(case_id):
    path = os.path.join(BASELINES_DIR, case_id + ".json")
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


class TestS0(FixtureTestCase):
    def test_01_fixture_integrity(self):
        """fixture 文件自身的完整性：schema、唯一性、truth 可核对、平台形态真实。"""
        print("\n=== 1. fixture 完整性 ===")
        seen = {}
        self.expect(len(FIXTURES) >= 8, f"fixture 数量应 ≥ 8，实际 {len(FIXTURES)}")

        for fx in FIXTURES:
            case_id = str(fx.get("case_id", ""))
            with self.subTest(case=case_id):
                self.expect(fx.get("schema_version") == 1,
                            f"{case_id}: schema_version 必须为 1", case_id)
                self.expect(bool(case_id), "case_id 不能为空", case_id)
                self.expect(case_id not in seen,
                            f"case_id 重复：{case_id}", case_id)
                seen[case_id] = True
                self.expect(bool(str(fx.get("form", "")).strip()),
                            f"{case_id}: form 不能为空", case_id)
                self.expect(isinstance(fx.get("tags", None), list) and bool(fx["tags"]),
                            f"{case_id}: tags 必须是非空数组", case_id)
                self.expect(len(str(fx.get("description", ""))) >= 30,
                            f"{case_id}: description 必须说明它代表哪一类形态", case_id)

                # --- 平台响应形态 ---
                api = fx.get("api_response") or {}
                prompt = ((api.get("data") or {}).get("prompt"))
                self.expect(isinstance(prompt, str),
                            f"{case_id}: api_response.data.prompt 必须是 JSON 字符串"
                            "（平台 getJsonApiFormat 的真实返回形态）", case_id)
                raw = cap.raw_prompt(fx)
                self.expect(bool(raw), f"{case_id}: prompt 解析不出任何节点", case_id)
                self.expect(api.get("code") == 0,
                            f"{case_id}: api_response.code 应为 0", case_id)

                # 每个节点都要有 _meta.title（平台真实形态，S2 会据此保留标题）
                missing_title = [
                    nid for nid, meta in raw.items()
                    if not isinstance((meta.get("_meta") or {}).get("title"), str)
                    or not (meta.get("_meta") or {}).get("title")
                ]
                self.expect(not missing_title,
                            f"{case_id}: 这些节点缺少 _meta.title：{missing_title}", case_id)

                # 连线必须是 [源nodeId, 出槽序号] 的二元素数组
                bad_links = []
                for nid, meta in raw.items():
                    for field, value in (meta.get("inputs") or {}).items():
                        if isinstance(value, list) and not (
                            len(value) == 2 and isinstance(value[0], (str, int))
                            and isinstance(value[1], int)
                        ):
                            bad_links.append(f"{nid}.{field}")
                self.expect(not bad_links,
                            f"{case_id}: 这些字段的连线形态不合法：{bad_links}", case_id)

                # --- truth 可核对 ---
                truth = fx.get("truth") or {}
                for port in ("positive", "negative", "seed"):
                    slots = truth.get(port) or []
                    for slot in slots:
                        nid = str(slot.get("node", ""))
                        self.expect(nid in raw,
                                    f"{case_id}: truth.{port} 引用的 nodeId={nid} "
                                    "不在 api_response 里", case_id)
                        field = str(slot.get("field", ""))
                        node = raw.get(nid) or {}
                        self.expect(field in (node.get("inputs") or {}),
                                    f"{case_id}: truth.{port} 的 {nid}.{field} "
                                    "不是该节点的输入字段", case_id)
                self.expect(bool(str(truth.get("note", "")).strip()),
                            f"{case_id}: truth.note 必须写清人工判断依据", case_id)

                # --- expect_current ---
                exp = fx.get("expect_current") or {}
                self.expect(exp.get("status") in VALID_STATUS,
                            f"{case_id}: expect_current.status 必须是 "
                            f"{VALID_STATUS} 之一，实际 {exp.get('status')!r}", case_id)
                self.expect(len(str(exp.get("why", ""))) >= 20,
                            f"{case_id}: expect_current.why 必须写清机制层面的原因", case_id)
                if exp.get("status") == STATUS_SUSPECT:
                    # 必须点名一条已知规则，且这条规则要在 why 里被写出来
                    self.expect(exp.get("violation") in KNOWN_VIOLATIONS,
                                f"{case_id}: injected_suspect 必须用 violation 点名 "
                                f"{KNOWN_VIOLATIONS} 之一，实际 {exp.get('violation')!r}",
                                case_id)
                    self.expect(str(exp.get("violation", "")) in str(exp.get("why", "")),
                                f"{case_id}: why 必须写明违反的 {exp.get('violation')!r}",
                                case_id)

                # 基线文件必须存在（否则回放无从比对）
                self.expect(load_baseline(case_id) is not None,
                            f"{case_id}: 缺少基线文件（先跑 tools/capture_baseline.py --update）",
                            case_id)

    def test_02_normalize_keeps_structure(self):
        """normalize 能正确解析 fixture：节点数一致、class_type 保留、_meta 不污染 inputs。"""
        print("\n=== 2. 结构解析（_meta 不得破坏解析） ===")
        for fx in FIXTURES:
            case_id = fx["case_id"]
            raw = cap.raw_prompt(fx)
            structure, _ = cap.replay(fx)
            with self.subTest(case=case_id):
                self.expect_equal(len(structure), len(raw),
                                  f"{case_id}: 归一化后节点数应等于原始节点数", case_id)
                self.expect(set(structure) == set(raw),
                            f"{case_id}: 归一化后 nodeId 集合应与原始一致", case_id)
                for nid in sorted(raw, key=cap._node_sort_key):
                    self.expect_equal(structure.get(nid, {}).get("class_type"),
                                      raw[nid].get("class_type"),
                                      f"{case_id}: nodeId={nid} 的 class_type 被改了", case_id)
                    inputs = structure.get(nid, {}).get("inputs") or {}
                    self.expect("_meta" not in inputs and "meta" not in inputs,
                                f"{case_id}: nodeId={nid} 的 inputs 里混进了 meta 字段",
                                case_id)
                    self.expect_equal(sorted(inputs), sorted(raw[nid].get("inputs") or {}),
                                      f"{case_id}: nodeId={nid} 的输入字段集合被改了", case_id)

    def test_03_replay_equals_baseline(self):
        """回放结果必须与基线逐字段一致（防后续重构悄悄改行为）。"""
        print("\n=== 3. 回放 vs 基线 ===")
        for fx in FIXTURES:
            case_id = fx["case_id"]
            baseline = load_baseline(case_id)
            _, actual = cap.replay(fx)
            with self.subTest(case=case_id):
                self.expect(baseline is not None, f"{case_id}: 缺少基线", case_id)
                if baseline is None:
                    continue
                self.expect_equal(actual["node_count"], baseline["node_count"],
                                  f"{case_id}: node_count 与基线不一致", case_id)
                self.expect_equal(actual["string_field_count"],
                                  baseline["string_field_count"],
                                  f"{case_id}: string_field_count 与基线不一致", case_id)
                self.expect_equal(actual["link_field_count"], baseline["link_field_count"],
                                  f"{case_id}: link_field_count 与基线不一致", case_id)
                self.expect_equal(actual["placeholder_hits"], baseline["placeholder_hits"],
                                  f"{case_id}: placeholder_hits 与基线不一致", case_id)
                self.expect_equal(actual["hit_stats"], baseline["hit_stats"],
                                  f"{case_id}: hit_stats 与基线不一致", case_id)
                self.expect_equal(actual["injections"], baseline["injections"],
                                  f"{case_id}: 注入项与基线不一致（落点或提交值变了）", case_id)
                self.expect_equal(actual["seed_targets"], baseline["seed_targets"],
                                  f"{case_id}: seed_targets 与基线不一致", case_id)
                self.expect_equal(actual["diagnosis"], baseline["diagnosis"],
                                  f"{case_id}: 诊断文案与基线不一致", case_id)
                self.expect_equal(actual["expect_current"], baseline["expect_current"],
                                  f"{case_id}: expect_current 与基线不一致", case_id)

    def test_04_status_matches_behaviour(self):
        """expect_current.status 必须与基线实际表现一致。"""
        print("\n=== 4. 形态预期 vs 实际表现 ===")
        for fx in FIXTURES:
            case_id = fx["case_id"]
            status = (fx.get("expect_current") or {}).get("status")
            with self.subTest(case=case_id):
                # 0 命中形态的第一要求是「不抛异常」：宁可明确失败，也不能崩
                error = None
                try:
                    _, record = cap.replay(fx)
                except Exception as exc:  # noqa: BLE001
                    error = exc
                    record = None
                self.expect(error is None,
                            f"{case_id}: 回放抛异常 {error!r}（失效也必须优雅失败）", case_id)
                if record is None:
                    continue

                hits = record["hit_stats"]
                values = [str(i.get("fieldValue")) for i in record["injections"]]
                pos_ok = any(cap.POSITIVE_SENTINEL in v for v in values)
                neg_ok = any(cap.NEGATIVE_SENTINEL in v for v in values)

                if status == STATUS_ALREADY_OK:
                    self.expect(pos_ok,
                                f"{case_id}: 标为 already_ok 但正向没有产生注入", case_id)
                    self.expect(neg_ok,
                                f"{case_id}: 标为 already_ok 但负向没有产生注入", case_id)
                elif status == STATUS_SUSPECT:
                    # 「有注入」只是入场券；真正让它是 suspect 的是**注入在不变量下不成立**，
                    # 因此这里只校验前半段，违反项是否点名由 test_01 的一致性检查守住。
                    self.expect(pos_ok,
                                f"{case_id}: 标为 injected_suspect 但正向没有产生注入",
                                case_id)
                    self.expect(neg_ok,
                                f"{case_id}: 标为 injected_suspect 但负向没有产生注入",
                                case_id)
                elif status == STATUS_REGRESSION:
                    self.expect(hits["positive"] == 0 and hits["negative"] == 0,
                                f"{case_id}: 标为 regression_open 但实际命中 {hits}", case_id)
                    self.expect(not record["injections"],
                                f"{case_id}: 标为 regression_open 但产生了注入", case_id)
                elif status == STATUS_AMBIGUOUS:
                    self.expect(hits["positive"] == 0 and hits["negative"] == 0,
                                f"{case_id}: 标为 ambiguous_reject 但实际命中 {hits}", case_id)
                    self.expect(not record["injections"],
                                f"{case_id}: 标为 ambiguous_reject 但产生了注入", case_id)
                    diag = record["diagnosis"]["positive"]
                    self.expect("多个文本字段" in diag or "无法判断" in diag,
                                f"{case_id}: 歧义形态的诊断应说明「多个候选」，实际：{diag}",
                                case_id)

    def test_05_invariant_a_no_link_injection(self):
        """不变量 A：注入目标字段在原始结构里不得是 [] 连线形态。"""
        print("\n=== 5. 不变量 A：绝不向连线字段注入 ===")
        for fx in FIXTURES:
            case_id = fx["case_id"]
            raw = cap.raw_prompt(fx)
            _, record = cap.replay(fx)
            with self.subTest(case=case_id):
                for item in record["injections"]:
                    nid = str(item.get("nodeId"))
                    field = str(item.get("fieldName"))
                    value = (raw.get(nid, {}).get("inputs") or {}).get(field)
                    self.expect(not isinstance(value, list),
                                f"{case_id}: 注入目标 {nid}.{field} 在原始结构里是连线 {value!r}"
                                " —— 平台明确不建议修改连线字段", case_id)
                    self.expect(field in (raw.get(nid, {}).get("inputs") or {}),
                                f"{case_id}: 注入目标 {nid}.{field} 不存在于原始结构", case_id)
                # 兜底：没有任何注入时也应满足（形如不会崩）
                self.expect(isinstance(record["injections"], list),
                            f"{case_id}: injections 必须是列表", case_id)

    def test_06_no_silent_failure(self):
        """禁止静默：0 命中的形态必须给出带 nodeId 数字的中文诊断。"""
        print("\n=== 6. 禁止静默失效 ===")
        for fx in FIXTURES:
            case_id = fx["case_id"]
            status = (fx.get("expect_current") or {}).get("status")
            if status not in (STATUS_REGRESSION, STATUS_AMBIGUOUS):
                continue
            _, record = cap.replay(fx)
            with self.subTest(case=case_id):
                for token in ("positive", "negative"):
                    diag = record["diagnosis"][token]
                    self.expect(bool(str(diag).strip()),
                                f"{case_id}: {token} 端 0 命中但诊断为空（静默失效）", case_id)
                    self.expect(bool(re.search(r"\d", str(diag))),
                                f"{case_id}: {token} 端诊断必须落到具体 nodeId，实际：{diag}",
                                case_id)
                    self.expect(bool(re.search(r"[\u4e00-\u9fff]", str(diag))),
                                f"{case_id}: {token} 端诊断必须是中文说明，实际：{diag}", case_id)

    def test_07_baseline_is_deterministic(self):
        """基线必须确定：同一份 fixture 连续两次回放必须完全一致。"""
        print("\n=== 7. 基线确定性 ===")
        for fx in FIXTURES:
            case_id = fx["case_id"]
            first = cap.build_record(fx)
            second = cap.build_record(fx)
            with self.subTest(case=case_id):
                self.expect_equal(first, second,
                                  f"{case_id}: 两次回放结果不一致（基线不可复现）", case_id)
                text = cap.dumps(first)
                self.expect(not re.search(r"[A-Za-z]:\\|/home/|/Users/", text),
                            f"{case_id}: 基线里混入了本机绝对路径", case_id)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    # 结果也写 stdout，保证与用例里的章节标题顺序一致（默认写 stderr 会串行错位）
    result = unittest.TextTestRunner(verbosity=1, stream=sys.stdout).run(suite)

    total = ASSERTIONS["total"]
    failed = ASSERTIONS["failed"]
    by_case = ASSERTIONS["by_case"]
    weakest = sorted(by_case.items(), key=lambda kv: kv[1])[:3]

    print("\n" + "=" * 60)
    print(f"断言总数 {total}，通过 {total - failed}，失败 {failed}")
    print(f"覆盖 case {len(by_case)} 个，单 case 断言数最少的前 3 个："
          + ", ".join(f"{k}={v}" for k, v in weakest))
    if min(by_case.values()) < MIN_ASSERTIONS_PER_CASE:
        print(f"断言预算不足：每个 case 至少需要 {MIN_ASSERTIONS_PER_CASE} 条断言")
        sys.exit(1)
    sys.exit(0 if result.wasSuccessful() else 1)
