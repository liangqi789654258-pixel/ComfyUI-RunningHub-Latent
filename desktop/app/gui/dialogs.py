"""几个独立对话框：启动自检 / 任务台账 / 按 taskId 续取。

都放在一个文件里而不是塞进 main_window：主窗口已经 1000 行，
再往里加三个对话框就真的没法读了。
"""

from __future__ import annotations

import os
import time
from typing import Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .. import i18n, selfcheck


class SelfCheckDialog(QDialog):
    """启动自检结果。

    设计成**非模态**（show 而不是 exec）：自检不该拦住用户，
    他想先去改配置还是先看看界面都行。
    """

    def __init__(self, checks: List[selfcheck.Check], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(i18n.tr("sc_title"))
        self.setMinimumWidth(620)
        self.setAttribute(Qt.WA_DeleteOnClose, True)

        root = QVBoxLayout(self)
        bad = [c for c in checks if not c.ok]
        intro = i18n.tr("sc_intro") if bad else i18n.tr("sc_all_ok")
        title = QLabel(intro)
        title.setWordWrap(True)
        root.addWidget(title)

        for c in checks:
            row = QHBoxLayout()
            mark = QLabel("✓" if c.ok else "✗")
            mark.setFixedWidth(18)
            mark.setStyleSheet(
                "color:#5fbf7f" if c.ok else "color:#e0533d; font-weight:600"
            )
            name = QLabel(i18n.tr(c.key))
            name.setFixedWidth(120)
            detail = QLabel(c.detail or "")
            detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
            detail.setStyleSheet("color:#9aa4b2")
            row.addWidget(mark)
            row.addWidget(name)
            row.addWidget(detail, 1)
            root.addLayout(row)
            if not c.ok and c.fix:
                hint = QLabel("    " + i18n.tr(c.fix))
                hint.setWordWrap(True)
                hint.setStyleSheet("color:#e8b339")
                root.addWidget(hint)

        btns = QHBoxLayout()
        btns.addStretch(1)
        self.btn_settings = QPushButton(i18n.tr("sc_open_settings"))
        self.btn_settings.clicked.connect(self._open_settings)
        self.btn_later = QPushButton(i18n.tr("sc_later"))
        self.btn_later.clicked.connect(self.close)
        btns.addWidget(self.btn_settings)
        btns.addWidget(self.btn_later)
        root.addLayout(btns)

    def _open_settings(self) -> None:
        parent = self.parent()
        self.close()
        if parent is not None and hasattr(parent, "_on_settings"):
            parent._on_settings()


class LedgerDialog(QDialog):
    """任务台账：最近若干次云端调用（成功/失败/取消都在里面）。"""

    COLUMNS = ("ledger_col_time", "ledger_col_task", "ledger_col_outcome",
               "ledger_col_coins", "ledger_col_elapsed", "ledger_col_note")

    def __init__(self, records: List[Dict], ledger_path: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(i18n.tr("ledger_title"))
        self.resize(760, 420)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self._path = ledger_path

        root = QVBoxLayout(self)
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels([i18n.tr(k) for k in self.COLUMNS])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(5, QHeaderView.Stretch)
        root.addWidget(self.table, 1)

        for rec in records:
            self._add_row(rec)
        if not records:
            self.table.setRowCount(1)
            self.table.setItem(0, 0, QTableWidgetItem(i18n.tr("ledger_empty")))

        btns = QHBoxLayout()
        hint = QLabel(i18n.tr("ledger_count", n=len(records)))
        hint.setStyleSheet("color:#9aa4b2")
        btns.addWidget(hint, 1)
        self.btn_open = QPushButton(i18n.tr("ledger_open"))
        self.btn_open.clicked.connect(self._open_file)
        self.btn_close = QPushButton(i18n.tr("close"))
        self.btn_close.clicked.connect(self.close)
        btns.addWidget(self.btn_open)
        btns.addWidget(self.btn_close)
        root.addLayout(btns)

    def _add_row(self, rec: Dict) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        outcome = str(rec.get("outcome", ""))
        coins = rec.get("coins_spent")
        elapsed = rec.get("elapsed_s")
        note = str(rec.get("error_reason") or rec.get("preset") or "")
        cells = [
            str(rec.get("ts", "")),
            str(rec.get("task_id", "")),
            i18n.tr(f"outcome_{outcome}") if outcome else "",
            f"{coins:g}" if isinstance(coins, (int, float)) else "",
            f"{elapsed:.0f}s" if isinstance(elapsed, (int, float)) else "",
            note,
        ]
        for col, text in enumerate(cells):
            item = QTableWidgetItem(text)
            if col == 2 and outcome == "failed":
                # 深色主题下 Qt.red 对比度太低，用偏亮的红
                item.setForeground(QColor("#e0533d"))
            self.table.setItem(row, col, item)
        self.table.resizeColumnsToContents()

    def _open_file(self) -> None:
        if os.path.isfile(self._path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._path))


class ResumeDialog(QDialog):
    """按 taskId 续取：不重新提交、不重复扣费。"""

    def __init__(self, last_task_id: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(i18n.tr("resume_title"))
        self.setMinimumWidth(520)

        root = QVBoxLayout(self)
        tip = QLabel(i18n.tr("resume_hint"))
        tip.setWordWrap(True)
        root.addWidget(tip)

        self.edit = QLineEdit(last_task_id)
        self.edit.setPlaceholderText(i18n.tr("resume_placeholder"))
        root.addWidget(self.edit)

        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Ok).setText(i18n.tr("resume_go"))
        box.button(QDialogButtonBox.Cancel).setText(i18n.tr("cancel"))
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        root.addWidget(box)

    def task_id(self) -> str:
        return self.edit.text().strip()


def today_coins(records: List[Dict]) -> float:
    """统计「今天」消耗的积分（按台账 ts 的日期部分比）。"""
    today = time.strftime("%Y-%m-%d")
    total = 0.0
    for rec in records:
        ts = str(rec.get("ts", ""))
        if not ts.startswith(today):
            continue
        coins = rec.get("coins_spent")
        if isinstance(coins, (int, float)) and coins > 0:
            total += float(coins)
    return total
