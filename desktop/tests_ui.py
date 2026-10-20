"""新增界面/逻辑的功能测试：自检、台账、成本护栏、设置项。

不测外观，只测「这些功能到底有没有真的接上」——
比如成本护栏超限时必须真的拦（返回 False），而不是弹个框就继续跑。
"""

import faulthandler
faulthandler.enable()
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP)

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from app import appconfig, selfcheck  # noqa: E402
from app.gui.dialogs import (  # noqa: E402
    LedgerDialog,
    ResumeDialog,
    SelfCheckDialog,
    today_coins,
)

app = QApplication.instance() or QApplication(sys.argv)
failures = []


def check(name, fn):
    try:
        fn()
        print(f"[OK] {name}")
    except Exception as exc:
        failures.append((name, exc))
        print(f"[FAIL] {name}: {exc}")


# ---------------------------------------------------------------- 自检
def t_selfcheck_shape():
    checks = selfcheck.run_checks()
    assert len(checks) == 6, f"检查项数量异常：{len(checks)}"
    keys = {c.key for c in checks}
    for need in ("sc_api_key", "sc_comfy_root", "sc_plugin", "sc_vae",
                 "sc_ffmpeg", "sc_output"):
        assert need in keys, f"缺少检查项 {need}"
    for c in checks:
        assert c.level in (selfcheck.BLOCK, selfcheck.WARN)
        if not c.ok:
            assert c.fix, f"{c.key} 失败却没给修复建议"


def t_selfcheck_structure():
    """自检必须能跑、能给修复建议。

    注意**不要**断言「全部通过」：CI 机器上没有 ComfyUI、没有 VAE 模型，
    那正是自检该报的项，不是 bug。真正要守住的是两条：
      1. 任何失败项都必须带修复建议（否则用户看到红叉不知道怎么办）；
      2. 仓库布局下的插件目录探测必须成功——desktop/ 就在插件仓库里，
         这一项要是都探不到，说明路径推导逻辑写错了。
    """
    checks = selfcheck.run_checks()
    assert len(checks) == 6, f"检查项数量异常：{len(checks)}"
    for c in checks:
        if not c.ok:
            assert c.fix, f"{c.key} 判定失败却没给修复建议"
    plugin = next(c for c in checks if c.key == "sc_plugin")
    assert plugin.ok, f"插件目录探测失败：{plugin.detail}"


def t_selfcheck_dialog_builds():
    dlg = SelfCheckDialog(selfcheck.run_checks())
    assert dlg.windowTitle()
    assert dlg.btn_settings.text() and dlg.btn_later.text()
    dlg.close()


# ---------------------------------------------------------------- 台账
def t_today_coins():
    today = time.strftime("%Y-%m-%d")
    records = [
        {"ts": f"{today} 10:00:00", "coins_spent": 12},
        {"ts": f"{today} 11:00:00", "coins_spent": 8.5},
        {"ts": f"{today} 12:00:00", "coins_spent": None},
        {"ts": "2000-01-01 10:00:00", "coins_spent": 999},   # 不是今天，不算
        {"ts": f"{today} 13:00:00", "coins_spent": -3},      # 并发导致的负值，不算
    ]
    total = today_coins(records)
    assert abs(total - 20.5) < 1e-6, f"今日积分统计错误：{total}"


def t_ledger_dialog_builds():
    today = time.strftime("%Y-%m-%d")
    records = [
        {"ts": f"{today} 10:00:00", "task_id": "T1", "outcome": "success",
         "coins_spent": 5, "elapsed_s": 42.0},
        {"ts": f"{today} 10:05:00", "task_id": "T2", "outcome": "failed",
         "error_reason": "803 映射不合法"},
        {"ts": f"{today} 10:09:00", "task_id": "", "outcome": "canceled"},
    ]
    dlg = LedgerDialog(records, os.path.join(APP, "nope.jsonl"))
    assert dlg.table.rowCount() == 3, f"行数异常：{dlg.table.rowCount()}"
    assert dlg.table.item(0, 1).text() == "T1"
    assert dlg.table.item(1, 2).text() != "failed", "结果列应该是翻译后的文案"
    dlg.close()


def t_ledger_dialog_empty():
    dlg = LedgerDialog([], os.path.join(APP, "nope.jsonl"))
    assert dlg.table.rowCount() == 1  # 空台账也要有一行提示
    dlg.close()


def t_resume_dialog():
    dlg = ResumeDialog("TASK-123")
    assert dlg.task_id() == "TASK-123"
    dlg.edit.setText("  TASK-9  ")
    assert dlg.task_id() == "TASK-9", "taskId 应该去掉首尾空格"
    dlg.close()


# ---------------------------------------------------------------- 成本护栏
def t_cost_guard():
    from app.gui.main_window import MainWindow

    original_question = QMessageBox.question
    original_ledger = appconfig.get
    win = MainWindow()
    try:
        # 上限 0 = 不限制
        appconfig.set_value("daily_coin_limit", 0)
        assert win._confirm_cost() is True, "上限为 0 时不该拦"

        # 上限 5，今日已花远超（用假台账覆盖）
        appconfig.set_value("daily_coin_limit", 5)
        today = time.strftime("%Y-%m-%d")
        fake_records = [{"ts": f"{today} 10:00:00", "coins_spent": 99}]
        from app.engine import core_bridge

        class FakeLedger:
            def read_records(self, limit=200):
                return fake_records

        original = core_bridge.core_ledger
        core_bridge.core_ledger = lambda: FakeLedger()
        try:
            QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
            assert win._confirm_cost() is False, "超限且用户选「否」时必须拦住"
            QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
            assert win._confirm_cost() is True, "用户选「是」时应当放行"
        finally:
            core_bridge.core_ledger = original
    finally:
        QMessageBox.question = original_question
        appconfig.get = original_ledger
        appconfig.set_value("daily_coin_limit", 0)
        win.close()


# ---------------------------------------------------------------- 设置项
def t_settings_widgets():
    from app.gui.main_window import SettingsDialog

    dlg = SettingsDialog()
    assert hasattr(dlg, "btn_import_key"), "设置里缺少「从插件导入 Key」按钮"
    assert hasattr(dlg, "spin_cost"), "设置里缺少「每日积分上限」"
    assert hasattr(dlg, "btn_clear_thumbs"), "设置里缺少缩略图缓存清理"
    dlg.close()


def t_import_key_reads_plugin_config():
    """导入按钮只读插件配置；没配 Key 时给提示而不是崩。"""
    from app.engine import core_bridge
    from app.gui.main_window import SettingsDialog

    dlg = SettingsDialog()
    original = core_bridge.core_settings
    shown = []
    original_info = QMessageBox.information
    try:
        core_bridge.core_settings = lambda: type("S", (), {"get": staticmethod(lambda k, d=0: "")})
        QMessageBox.information = staticmethod(lambda *a, **k: shown.append(a[2]))
        dlg._on_import_key()
        assert shown and "config.ini" in shown[-1], "没配 Key 时应提示去插件里设置"

        core_bridge.core_settings = lambda: type(
            "S", (), {"get": staticmethod(lambda k, d=0: "abcdefgh12345678")}
        )
        dlg._on_import_key()
        assert dlg.ed_key.text() == "abcdefgh12345678", "应当把插件 Key 填进输入框"
    finally:
        core_bridge.core_settings = original
        QMessageBox.information = original_info
        dlg.close()


for name, fn in [
    ("自检项结构与修复建议", t_selfcheck_shape),
    ("自检结构与本仓库布局探测", t_selfcheck_structure),
    ("自检对话框可构建", t_selfcheck_dialog_builds),
    ("今日积分统计", t_today_coins),
    ("台账对话框可构建", t_ledger_dialog_builds),
    ("空台账也有提示行", t_ledger_dialog_empty),
    ("续取对话框", t_resume_dialog),
    ("成本护栏拦截逻辑", t_cost_guard),
    ("设置新增控件", t_settings_widgets),
    ("导入插件 Key", t_import_key_reads_plugin_config),
]:
    check(name, fn)

print()
if failures:
    print(f"界面功能测试失败 {len(failures)} 项：{[n for n, _ in failures]}")
    sys.exit(1)
print("界面功能测试全部通过")

# 收尾：同上，确保没有残留窗口/线程在解释器退出时被回收
for _w in QApplication.topLevelWidgets():
    _w.close()
app.quit()
sys.stdout.flush()
