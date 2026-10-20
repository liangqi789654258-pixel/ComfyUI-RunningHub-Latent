"""RH 出图器主窗口（P0）。

布局：工具栏（预设/生成预设）+ 左参数区（提示词/映射编辑器/种子/VAE）+
右作品画廊 + 底部状态栏。映射编辑器 = JSON 文本 + 生成模板 + 本地预校验。
"""

from __future__ import annotations

import json
import os
import random
from typing import List, Optional

from PySide6.QtCore import QSize, Qt, QPointF, QThread, QTimer, QUrl
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QIcon,
    QPainter,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QSystemTrayIcon,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import appconfig, i18n, selfcheck, thumbs
from ..engine import core_bridge, presets as presets_mod
from .dialogs import LedgerDialog, ResumeDialog, SelfCheckDialog, today_coins
from .gallery_thumbs import ThumbWorker
from .threads import GenerateThread, PresetCreateThread

_GALLERY_EXTS = (
    ".png", ".jpg", ".jpeg", ".webp", ".bmp",
    ".mp4", ".webm", ".mov", ".mkv",
)
_GALLERY_LIMIT = 200   # 作品栏最多列多少条（输出目录可能指向 Downloads 这种大目录）
_THUMB_BOX = 320       # 缩略图边长（列表 iconSize 是 160，给 2 倍供 hidpi）


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self._thread: Optional[GenerateThread] = None
        self._preset_thread: Optional[PresetCreateThread] = None
        self._current_preset: Optional[dict] = None
        # 作品栏：path → QListWidgetItem（视频首帧到货后按 path 回填）
        self._gallery_items: dict = {}
        self._thumb_worker = None
        self._thumb_thread: Optional[QThread] = None
        self.setWindowTitle(i18n.tr("app_title"))
        self.resize(1180, 720)
        self._build_ui()
        self._reload_presets()
        self._reload_gallery()
        self._reload_vae_list()
        self._restore_ui_state()
        # 首次使用的引导提示（Key 为空时）
        if not appconfig.api_key():
            self.status.setText(i18n.tr("first_run_hint"))
        self._setup_tray()
        # 自检放在窗口画完之后跑：先让用户看到界面，再告诉他哪里没配好
        QTimer.singleShot(400, lambda: self._show_selfcheck(force=False))

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # 工具栏行：预设选择 + 生成预设
        bar = QHBoxLayout()
        bar.addWidget(QLabel(i18n.tr("preset") + ":"))
        self.combo_preset = QComboBox()
        self.combo_preset.setMinimumWidth(260)
        self.combo_preset.currentIndexChanged.connect(self._on_preset_selected)
        bar.addWidget(self.combo_preset, 1)
        self.btn_preset_new = QPushButton(i18n.tr("preset_new"))
        self.btn_preset_new.clicked.connect(self._on_create_preset)
        bar.addWidget(self.btn_preset_new)
        self.btn_settings = QPushButton(i18n.tr("settings"))
        self.btn_settings.clicked.connect(self._on_settings)
        bar.addWidget(self.btn_settings)

        # 「更多」菜单：自检 / 台账 / 续取——都是低频功能，不占按钮位
        self.btn_more = QToolButton()
        self.btn_more.setText("⋯")
        self.btn_more.setToolTip(i18n.tr("menu_more"))
        self.btn_more.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(self.btn_more)
        self.act_selfcheck = QAction(i18n.tr("sc_title"), self)
        self.act_selfcheck.triggered.connect(lambda: self._show_selfcheck(force=True))
        self.act_ledger = QAction(i18n.tr("ledger_title"), self)
        self.act_ledger.triggered.connect(self._show_ledger)
        self.act_resume = QAction(i18n.tr("resume_title"), self)
        self.act_resume.triggered.connect(self._on_resume)
        menu.addAction(self.act_selfcheck)
        menu.addAction(self.act_ledger)
        menu.addAction(self.act_resume)
        self.btn_more.setMenu(menu)
        bar.addWidget(self.btn_more)
        root.addLayout(bar)

        splitter = QSplitter(Qt.Horizontal)

        # 左：参数区
        left = QWidget()
        form = QVBoxLayout(left)

        gb_prompt = QGroupBox(i18n.tr("positive") + " / " + i18n.tr("negative"))
        pv = QVBoxLayout(gb_prompt)
        self.txt_positive = QPlainTextEdit()
        self.txt_positive.setPlaceholderText("1girl, solo, ...")
        self.txt_positive.setFixedHeight(84)
        pv.addWidget(self.txt_positive)
        self.txt_negative = QPlainTextEdit()
        self.txt_negative.setPlaceholderText("worst quality, low quality, ...")
        self.txt_negative.setFixedHeight(56)
        pv.addWidget(self.txt_negative)
        form.addWidget(gb_prompt)

        gb_map = QGroupBox(i18n.tr("mapping"))
        mv = QVBoxLayout(gb_map)
        self.txt_mapping = QPlainTextEdit()
        self.txt_mapping.setFont(self._mono_font())
        mv.addWidget(self.txt_mapping)
        map_btns = QHBoxLayout()
        self.btn_map_template = QPushButton(i18n.tr("mapping_template_btn"))
        self.btn_map_template.clicked.connect(self._on_mapping_template)
        self.btn_map_validate = QPushButton(i18n.tr("mapping_validate_btn"))
        self.btn_map_validate.clicked.connect(self._on_mapping_validate)
        map_btns.addWidget(self.btn_map_template)
        map_btns.addWidget(self.btn_map_validate)
        map_btns.addStretch(1)
        mv.addLayout(map_btns)
        self.lbl_mapping_status = QLabel("")
        self.lbl_mapping_status.setWordWrap(True)
        mv.addWidget(self.lbl_mapping_status)
        form.addWidget(gb_map)

        gb_opts = QGroupBox()
        ov = QHBoxLayout(gb_opts)
        ov.addWidget(QLabel(i18n.tr("seed")))
        self.spin_seed = QSpinBox()
        self.spin_seed.setRange(0, 2**31 - 1)
        ov.addWidget(self.spin_seed, 1)
        self.chk_random_seed = QCheckBox(i18n.tr("seed_random"))
        self.chk_random_seed.setChecked(True)
        self.chk_random_seed.toggled.connect(
            lambda on: self.spin_seed.setEnabled(not on)
        )
        # setChecked 在 connect 之前，不会触发联动 → 手动同步一次初值，
        # 否则启动时「每次随机」是勾着的、种子框却还能编辑（视觉误导）
        self.spin_seed.setEnabled(not self.chk_random_seed.isChecked())
        ov.addWidget(self.chk_random_seed)
        ov.addWidget(QLabel(i18n.tr("vae")))
        self.combo_vae = QComboBox()
        self.combo_vae.setMinimumWidth(180)
        ov.addWidget(self.combo_vae, 2)
        form.addWidget(gb_opts)

        # 视频解码（仅当云端返回视频 latent 时生效；出图时忽略）
        # 模式开关：大按钮（不用勾选框）。开启 = 视频模式（显示视频设置、
        # 上方主 VAE 置灰避免三 VAE 并存）；关闭 = 图像模式（视频组收起）。
        # 解码路由始终按云端 latent 类型自动决定，收起时视频按已存设置生效。
        self.btn_video_mode = QPushButton(i18n.tr("video_mode_btn_on"))
        self.btn_video_mode.setObjectName("mode_toggle")
        self.btn_video_mode.setCheckable(True)
        self.btn_video_mode.setMinimumHeight(44)
        self.btn_video_mode.setCursor(Qt.PointingHandCursor)
        self.btn_video_mode.setToolTip(i18n.tr("video_mode_tooltip"))
        self.btn_video_mode.setChecked(bool(appconfig.get("video_mode", True)))
        self.btn_video_mode.toggled.connect(self._on_video_mode_toggled)
        form.addWidget(self.btn_video_mode)

        gb_video = QGroupBox(i18n.tr("video_decode"))
        self.gb_video = gb_video  # 供测试与模式开关引用
        vv = QVBoxLayout(gb_video)
        # 行 1：视频 VAE（覆盖用，默认跟随上方）+ 音频 VAE（H3 复合 latent 用）
        row1 = QHBoxLayout()
        row1.addWidget(QLabel(i18n.tr("video_vae")))
        self.combo_video_vae = QComboBox()
        self.combo_video_vae.setMinimumWidth(150)
        self.combo_video_vae.setToolTip(i18n.tr("video_vae_tooltip"))
        row1.addWidget(self.combo_video_vae, 2)
        row1.addWidget(QLabel(i18n.tr("audio_vae")))
        self.combo_audio_vae = QComboBox()
        self.combo_audio_vae.setMinimumWidth(140)
        self.combo_audio_vae.setToolTip(i18n.tr("audio_vae_tooltip"))
        row1.addWidget(self.combo_audio_vae, 2)
        vv.addLayout(row1)
        # 行 2：分块档位 + 帧率
        row2 = QHBoxLayout()
        row2.addWidget(QLabel(i18n.tr("video_tier")))
        self.combo_video_tier = QComboBox()
        for label, key in (
            (i18n.tr("video_tier_auto"), "auto"),
            (i18n.tr("video_tier_480p"), "480p"),
            (i18n.tr("video_tier_720p"), "720p"),
            (i18n.tr("video_tier_720p_long"), "720p_long"),
        ):
            self.combo_video_tier.addItem(label, key)
        stored_tier = str(appconfig.get("video_tier", "auto") or "auto")
        idx = self.combo_video_tier.findData(stored_tier)
        if idx >= 0:
            self.combo_video_tier.setCurrentIndex(idx)
        row2.addWidget(self.combo_video_tier, 2)
        row2.addWidget(QLabel(i18n.tr("video_fps")))
        self.spin_video_fps = QSpinBox()
        self.spin_video_fps.setRange(4, 60)
        self.spin_video_fps.setValue(int(appconfig.get("video_fps", 16) or 16))
        self.spin_video_fps.setToolTip(i18n.tr("video_fps_hint"))
        row2.addWidget(self.spin_video_fps, 1)
        vv.addLayout(row2)
        # 开关本身是上方的大按钮（不用勾选框），本组只负责承载设置项；
        # 可见性 / 主 VAE 置灰由 _on_video_mode_toggled 统一处理。
        form.addWidget(gb_video)

        # 应用一次初始状态（按钮 checked 已在上面恢复），确保启动后
        # 视频组可见性与「VAE 模型」启用状态跟保存的模式一致。
        self._on_video_mode_toggled(self.btn_video_mode.isChecked())

        gen_row = QHBoxLayout()
        self.btn_generate = QPushButton(i18n.tr("generate"))
        self.btn_generate.setMinimumHeight(40)
        self.btn_generate.setObjectName("primary")  # 主题里的琥珀金主按钮
        self.btn_generate.clicked.connect(self._on_generate)
        gen_row.addWidget(self.btn_generate, 3)
        self.btn_cancel = QPushButton(i18n.tr("cancel"))
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setObjectName("danger")  # 红描边幽灵按钮
        self.btn_cancel.clicked.connect(self._on_cancel)
        gen_row.addWidget(self.btn_cancel, 1)
        form.addLayout(gen_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # 转圈模式
        self.progress.setVisible(False)
        form.addWidget(self.progress)
        form.addStretch(1)
        splitter.addWidget(left)

        # 右：画廊
        right = QWidget()
        rv = QVBoxLayout(right)
        head = QHBoxLayout()
        self.lbl_gallery = QLabel(i18n.tr("gallery"))
        head.addWidget(self.lbl_gallery)
        head.addStretch(1)
        self.btn_gallery_refresh = QPushButton(i18n.tr("gallery_refresh"))
        self.btn_gallery_refresh.clicked.connect(self._reload_gallery)
        head.addWidget(self.btn_gallery_refresh)
        rv.addLayout(head)
        self.list_gallery = QListWidget()
        self.list_gallery.setViewMode(QListWidget.IconMode)
        self.list_gallery.setIconSize(QPixmap(160, 160).size())
        self.list_gallery.setResizeMode(QListWidget.Adjust)
        self.list_gallery.setSpacing(8)
        # 固定单元格尺寸：文件名长短不一，不设 gridSize 时每格高度按内容算，
        # 两行长文件名会把缩略图和下一行挤在一起（截图里就是叠着的）。
        self.list_gallery.setGridSize(QSize(186, 214))
        self.list_gallery.setUniformItemSizes(True)
        self.list_gallery.setWordWrap(True)
        self.list_gallery.itemDoubleClicked.connect(self._on_open_file)
        self.list_gallery.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_gallery.customContextMenuRequested.connect(self._on_gallery_menu)
        rv.addWidget(self.list_gallery, 1)
        hint = QLabel(i18n.tr("gallery_open"))
        hint.setProperty("role", "hint")
        hint.setWordWrap(True)
        rv.addWidget(hint)
        splitter.addWidget(right)

        splitter.setSizes([520, 620])
        root.addWidget(splitter, 1)

        # 状态栏
        self.status = QLabel(i18n.tr("status_idle"))
        self.statusBar().addWidget(self.status, 1)

    @staticmethod
    def _mono_font():
        from PySide6.QtGui import QFont

        return QFont("Consolas", 9)

    # --------------------------------------------- 自检 / 台账 / 续取 / 托盘
    def _setup_tray(self) -> None:
        """系统托盘：长视频跑几分钟，窗口最小化时也能知道结果。"""
        self.tray = None
        try:
            if not QSystemTrayIcon.isSystemTrayAvailable():
                return
            icon = self.windowIcon()
            if icon.isNull():
                icon = QIcon()
            self.tray = QSystemTrayIcon(icon, self)
            self.tray.setToolTip(i18n.tr("app_title"))
            menu = QMenu(self)
            act_show = QAction(i18n.tr("show_main"), self)
            act_show.triggered.connect(self._restore_from_tray)
            act_quit = QAction(i18n.tr("quit"), self)
            act_quit.triggered.connect(self.close)
            menu.addAction(act_show)
            menu.addAction(act_quit)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(
                lambda reason: self._restore_from_tray()
                if reason == QSystemTrayIcon.DoubleClick else None
            )
            self.tray.show()
        except Exception:
            self.tray = None  # 托盘不可用不影响主流程

    def _restore_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _notify(self, title: str, body: str, is_error: bool = False) -> None:
        """后台完成时弹系统通知；窗口在前台就不打扰（状态栏已经显示了）。"""
        if self.tray is None or self.isActiveWindow():
            return
        try:
            self.tray.showMessage(
                title, body[:200],
                QSystemTrayIcon.Warning if is_error else QSystemTrayIcon.Information,
                6000,
            )
        except Exception:
            pass

    def _show_selfcheck(self, force: bool = False) -> None:
        """启动时只在「有阻塞项」才弹；手动点菜单则总是显示。"""
        try:
            checks = selfcheck.run_checks()
        except Exception:
            return
        if force or selfcheck.blocking(checks):
            SelfCheckDialog(checks, self).show()
            return
        # 全部通过且不是手动触发：安静地把结论写到状态栏就行
        if not force:
            self.status.setText(i18n.tr("sc_all_ok"))

    def _show_ledger(self) -> None:
        try:
            ledger = core_bridge.core_ledger()
            records = ledger.read_records(limit=200)
            path = ledger.ledger_path()
        except Exception as exc:
            QMessageBox.warning(self, i18n.tr("err_title"), str(exc))
            return
        LedgerDialog(records, path, self).show()

    def _on_resume(self) -> None:
        """按 taskId 续取：复用轮询之后的全部阶段，不重新提交。"""
        if self._thread is not None:
            return
        dlg = ResumeDialog(str(appconfig.get("last_task_id", "") or ""), self)
        if not dlg.exec():
            return
        task_id = dlg.task_id()
        if not task_id:
            return
        params = self._build_params(require_preset=False)
        if params is None:
            return
        self._start_thread(params, resume_task_id=task_id)

    # ---------------------------------------------------------------- 成本护栏
    def _confirm_cost(self) -> bool:
        """超过每日积分上限时先确认；上限为 0 表示不限制。"""
        try:
            limit = float(appconfig.get("daily_coin_limit", 0) or 0)
        except (TypeError, ValueError):
            limit = 0.0
        if limit <= 0:
            return True
        try:
            spent = today_coins(core_bridge.core_ledger().read_records(limit=500))
        except Exception:
            return True  # 台账读不出来就别拦人
        if spent < limit:
            return True
        answer = QMessageBox.question(
            self,
            i18n.tr("cost_guard_title"),
            i18n.tr("cost_guard_text", spent=spent, limit=limit),
        )
        return answer == QMessageBox.Yes

    # ------------------------------------------------- 状态记忆（关开一致）
    def closeEvent(self, event) -> None:
        try:
            self._stop_thumb_worker(wait_ms=3000)  # 抽帧线程必须收干净再退
        except Exception:
            pass
        try:
            self._save_ui_state()
        except Exception:
            pass
        super().closeEvent(event)

    def _save_ui_state(self) -> None:
        appconfig.set_value(
            "ui_state",
            {
                "preset_file": self.combo_preset.currentData(),
                "positive": self.txt_positive.toPlainText(),
                "negative": self.txt_negative.toPlainText(),
                "mapping": self.txt_mapping.toPlainText(),
                "seed": self.spin_seed.value(),
                "random_seed": self.chk_random_seed.isChecked(),
                "vae": self.combo_vae.currentText(),
                "audio_vae": self.combo_audio_vae.currentData() or "",
                "video_vae": self.combo_video_vae.currentData() or "",
                "video_tier": self.combo_video_tier.currentData() or "auto",
                "video_fps": self.spin_video_fps.value(),
            },
        )

    def _restore_ui_state(self) -> None:
        state = appconfig.get("ui_state") or {}
        if not isinstance(state, dict):
            return
        # 预设：选中上次的（_on_preset_selected 会自动填映射与 VAE）
        preset_file = state.get("preset_file")
        if preset_file:
            idx = self.combo_preset.findData(preset_file)
            if idx >= 0:
                self.combo_preset.setCurrentIndex(idx)
        # 保存过的参数覆盖预设默认值（上次用的时候可能改过）
        if isinstance(state.get("positive"), str):
            self.txt_positive.setPlainText(state["positive"])
        if isinstance(state.get("negative"), str):
            self.txt_negative.setPlainText(state["negative"])
        if isinstance(state.get("mapping"), str) and state["mapping"].strip():
            self.txt_mapping.setPlainText(state["mapping"])
        if isinstance(state.get("seed"), int):
            self.spin_seed.setValue(state["seed"])
        if isinstance(state.get("random_seed"), bool):
            self.chk_random_seed.setChecked(state["random_seed"])
        vae = state.get("vae")
        if isinstance(vae, str) and vae:
            idx = self.combo_vae.findText(vae)
            if idx >= 0:
                self.combo_vae.setCurrentIndex(idx)
        # 视频解码偏好（先恢复默认档位再覆盖保存值，顺序与预设同理）
        tier = state.get("video_tier")
        if isinstance(tier, str) and tier:
            idx = self.combo_video_tier.findData(tier)
            if idx >= 0:
                self.combo_video_tier.setCurrentIndex(idx)
        if isinstance(state.get("video_fps"), int):
            self.spin_video_fps.setValue(state["video_fps"])
        audio_vae = state.get("audio_vae")
        if isinstance(audio_vae, str):
            idx = self.combo_audio_vae.findData(audio_vae)
            if idx >= 0:
                self.combo_audio_vae.setCurrentIndex(idx)
        video_vae = state.get("video_vae")
        if isinstance(video_vae, str):
            idx = self.combo_video_vae.findData(video_vae)
            if idx >= 0:
                self.combo_video_vae.setCurrentIndex(idx)

    # -------------------------------------------------------------- presets
    def _reload_presets(self) -> None:
        self.combo_preset.blockSignals(True)
        self.combo_preset.clear()
        items = presets_mod.list_presets()
        if not items:
            self.combo_preset.addItem(i18n.tr("preset_none"), None)
        for p in items:
            self.combo_preset.addItem(p.get("name") or p.get("workflow_id"), p.get("__file"))
        self.combo_preset.blockSignals(False)
        if items:
            self._on_preset_selected(0)

    def _on_preset_selected(self, index: int) -> None:
        fname = self.combo_preset.currentData()
        self._current_preset = None
        if not fname:
            return
        for p in presets_mod.list_presets():
            if p.get("__file") == fname:
                self._current_preset = p
                break
        if not self._current_preset:
            return
        import json

        self.txt_mapping.setPlainText(
            json.dumps(self._current_preset.get("injections") or [], ensure_ascii=False, indent=2)
        )
        vae_name = str(self._current_preset.get("vae_file") or "")
        if vae_name:
            idx = self.combo_vae.findText(vae_name)
            if idx >= 0:
                self.combo_vae.setCurrentIndex(idx)

    def _on_create_preset(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(i18n.tr("preset_pick_title"))
        lay = QVBoxLayout(dialog)
        lay.addWidget(QLabel(i18n.tr("preset_pick_hint")))
        edit = QLineEdit()
        lay.addWidget(edit)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText(i18n.tr("preset_pick_btn"))
        btns.accepted.connect(dialog.accept)
        btns.rejected.connect(dialog.reject)
        lay.addWidget(btns)
        if dialog.exec() != QDialog.Accepted:
            return
        wf_id = edit.text().strip()
        if not wf_id:
            return
        self.status.setText(i18n.tr("stage_submit"))
        self._preset_thread = PresetCreateThread(wf_id, self)
        self._preset_thread.done.connect(self._on_preset_created)
        self._preset_thread.failed.connect(self._on_failed)
        self._preset_thread.start()

    def _on_preset_created(self, preset: dict) -> None:
        self.status.setText(i18n.tr("status_idle"))
        self._reload_presets()
        QMessageBox.information(
            self, i18n.tr("app_title"),
            i18n.tr("preset_generated", name=preset.get("name", "")),
        )

    # ------------------------------------------------------------- mapping
    def _structure_for_validation(self):
        """拿当前工作流结构（无网络时返回 None，校验降级为只查 JSON 语法）。"""
        if not self._current_preset:
            return None
        try:
            client = core_bridge.core_client().RunningHubClient(appconfig.api_key())
            return client.get_workflow_structure(
                str(self._current_preset.get("workflow_id", ""))
            )
        except Exception:
            return None

    def _on_mapping_template(self) -> None:
        if not self._current_preset:
            QMessageBox.information(self, i18n.tr("app_title"), i18n.tr("preset_none"))
            return
        notes = str(self._current_preset.get("notes") or "")
        if not notes:
            QMessageBox.information(self, i18n.tr("app_title"), "当前预设没有参考清单，请重新生成预设。")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(i18n.tr("mapping_template_btn"))
        lay = QVBoxLayout(dialog)
        txt = QPlainTextEdit()
        txt.setPlainText(notes)
        txt.setReadOnly(True)
        txt.setFont(self._mono_font())
        txt.resize(640, 460)
        lay.addWidget(txt)
        dialog.resize(680, 500)
        dialog.exec()

    def _on_mapping_validate(self) -> None:
        try:
            items = self._collect_injections()
        except Exception as exc:
            self.lbl_mapping_status.setStyleSheet("color:#f87171;")
            self.lbl_mapping_status.setText(str(exc))
            return
        structure = self._structure_for_validation()
        if structure is None:
            self.lbl_mapping_status.setStyleSheet("color:#fbbf24;")
            self.lbl_mapping_status.setText(
                i18n.tr("mapping_warn", n=0, detail="（无法连接云端，仅校验了 JSON 语法）")
            )
            return
        wf = core_bridge.core_workflow()
        fatal, linked = wf.split_injection_problems(structure, items)
        if fatal:
            self.lbl_mapping_status.setStyleSheet("color:#f87171;")
            self.lbl_mapping_status.setText(i18n.tr("mapping_fail", detail="\n".join(fatal)))
        elif linked:
            self.lbl_mapping_status.setStyleSheet("color:#fbbf24;")
            self.lbl_mapping_status.setText(i18n.tr("mapping_warn", n=len(linked), detail="\n".join(linked)))
        else:
            self.lbl_mapping_status.setStyleSheet("color:#4ade80;")
            self.lbl_mapping_status.setText(i18n.tr("mapping_ok", n=len(items)))

    def _collect_injections(self) -> List[dict]:
        from ..engine.runner import parse_mapping

        return parse_mapping(self.txt_mapping.toPlainText())

    # ------------------------------------------------------------ generate
    def _build_params(self, require_preset: bool = True):
        """从界面收集一次生成的参数；任何必要条件缺失都弹窗并返回 None。"""
        if require_preset and not self._current_preset:
            QMessageBox.information(self, i18n.tr("app_title"), i18n.tr("preset_none"))
            return None
        if not appconfig.api_key():
            QMessageBox.warning(self, i18n.tr("err_title"), i18n.tr("no_key"))
            return None
        vae_path = self._selected_vae_path()
        if not vae_path:
            QMessageBox.warning(
                self, i18n.tr("err_title"),
                i18n.tr("no_vae_found"),
            )
            return None
        try:
            mapping = self.txt_mapping.toPlainText()
            if self.chk_random_seed.isChecked():
                self.spin_seed.setValue(random.randint(0, 2**31 - 1))
            return runner_params_from_ui(
                self._current_preset or {}, mapping,
                positive=self.txt_positive.toPlainText(),
                negative=self.txt_negative.toPlainText(),
                seed=self.spin_seed.value(),
                random_seed=self.chk_random_seed.isChecked(),
                vae_path=vae_path,
                video_tier=self.combo_video_tier.currentData() or "auto",
                video_fps=self.spin_video_fps.value(),
                audio_vae_path=self._selected_audio_vae_path(),
                video_vae_path=self._selected_video_vae_path(),
            )
        except Exception as exc:
            QMessageBox.warning(self, i18n.tr("err_title"), str(exc))
            return None

    def _start_thread(self, params, resume_task_id: str = "") -> None:
        self._set_busy(True)
        self._thread = GenerateThread(params, self, resume_task_id=resume_task_id)
        self._thread.progress.connect(self.status.setText)
        self._thread.finished_ok.connect(self._on_done)
        self._thread.failed.connect(self._on_failed)
        self._thread.start()

    def _on_generate(self) -> None:
        if self._thread is not None:
            return
        params = self._build_params(require_preset=True)
        if params is None:
            return
        if not self._confirm_cost():
            return
        self._start_thread(params)

    def _on_cancel(self) -> None:
        if self._thread is not None:
            self._thread.cancel()
            self.status.setText(i18n.tr("canceled"))

    def _on_done(self, files: list, summary: str) -> None:
        self._set_busy(False)
        self.status.setText(i18n.tr("done_summary", n=len(files), coins=summary.split("|")[1].split("≈")[1].strip() if "|" in summary else "?", elapsed=summary))
        self.status.setText(summary)
        # 先释放线程句柄再刷作品栏：万一刷新抛异常，也不能让 self._thread
        # 残留成「正在生成」，否则下一次点生成会被自己的忙判定挡掉。
        thread = self._thread
        self._thread = None
        if thread is not None and thread.task_id:
            appconfig.set_value("last_task_id", thread.task_id)
        self._notify(i18n.tr("notify_done_title"), summary)
        try:
            self._reload_gallery()
        except Exception:
            pass

    def _on_failed(self, message: str) -> None:
        self._notify(i18n.tr("notify_failed_title"), message, is_error=True)
        self._set_busy(False)
        self.status.setText(i18n.tr("status_idle"))
        self._thread = None
        QMessageBox.critical(self, i18n.tr("err_title"), message)

    def _set_busy(self, busy: bool) -> None:
        self.progress.setVisible(busy)
        self.btn_generate.setEnabled(not busy)
        self.btn_cancel.setEnabled(busy)
        self.btn_preset_new.setEnabled(not busy)

    # -------------------------------------------------------------- gallery
    def _reload_gallery(self) -> None:
        # 先收上一轮抽帧线程。这里必须防异常：一旦抛出去，下面的 clear()
        # 走不到，界面会永远停在旧列表上（现象就是「刷新按钮点了没反应」）。
        try:
            self._stop_thumb_worker()
        except Exception:
            self._thumb_worker = None
            self._thumb_thread = None
        self.list_gallery.clear()
        self._gallery_items = {}
        out_dir = appconfig.output_dir()
        # 标题栏提示当前扫描的目录（输出目录可能被指到很深的地方，用户有权知道在扫哪）
        self.lbl_gallery.setToolTip(i18n.tr("gallery_scan_dir", d=out_dir))
        if not os.path.isdir(out_dir):
            return
        files: List[str] = []
        for root, _dirs, names in os.walk(out_dir):
            for name in names:
                if name.lower().endswith(_GALLERY_EXTS):
                    files.append(os.path.join(root, name))
        # 按修改时间倒序（最新在前）。不能按文件名排——输出目录若指向
        # Downloads 这类「重灾区」，中文文件名的码位比 "RH_" 大、日期子目录
        # 名（"2026-10-16"，数字开头）又几乎排在所有字母/中文目录之后，
        # 新图会被排到队尾再被条数上限截掉，看起来就是「生成了但不显示」。
        def _mtime(path: str) -> float:
            try:
                return os.path.getmtime(path)
            except OSError:
                return 0.0

        files.sort(key=_mtime, reverse=True)
        video_placeholder = self._video_placeholder_icon()
        image_placeholder = self._image_placeholder_icon()
        pending: List[str] = []
        for path in files[:_GALLERY_LIMIT]:
            is_video = thumbs.is_video(path)
            # 图标一律后台解（见 gallery_thumbs.py）。注意这里必须给「占位图标」
            # 而不是空 QIcon：IconMode 的单元格尺寸是按图标算的，先空后补会导致
            # 回填后行高错乱、条目互相叠在一起（踩过）。
            item = QListWidgetItem(
                video_placeholder if is_video else image_placeholder,
                os.path.basename(path),
            )
            item.setData(Qt.UserRole, path)
            item.setToolTip(path)
            self.list_gallery.addItem(item)
            pending.append(path)
            self._gallery_items[path] = item
        if not files:
            self.list_gallery.addItem(QListWidgetItem(i18n.tr("gallery_empty")))
        # 列表先出来，缩略图在后台线程里补
        self._start_thumb_worker(pending)

    # -------------------------------------------- 视频首帧缩略图（后台补图）
    def _start_thumb_worker(self, paths: List[str]) -> None:
        try:
            self._stop_thumb_worker()
        except Exception:
            self._thumb_worker = None
            self._thumb_thread = None
        if not paths:
            return
        # 图片不需要 ffmpeg；只有存在视频时才依赖它
        if any(thumbs.is_video(p) for p in paths) and not thumbs.video_ffmpeg_ok():
            # 没有 ffmpeg 也得让图片照常出图，只把视频留成占位图
            paths = [p for p in paths if not thumbs.is_video(p)]
            if not paths:
                return
        # QThread 挂 parent 上：由 Qt 管生命周期，Python 侧引用可以放心丢，
        # 不会出现「线程还在跑但已被 GC」的崩溃。
        thread = QThread(self)
        worker = ThumbWorker(paths)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.ready.connect(self._on_thumb_ready)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(lambda t=thread: self._forget_thumb_thread(t))
        self._thumb_thread = thread
        self._thumb_worker = worker
        thread.start()

    def _forget_thumb_thread(self, thread: QThread) -> None:
        """线程收尾时把 Python 侧引用清掉。

        不这么干的话，thread.deleteLater() 之后 self._thumb_thread 只是个
        已销毁的 C++ 对象壳，下一次 _stop_thumb_worker 摸它就会
        「Internal C++ object already deleted」——异常从 _reload_gallery
        最开头抛出，后面的 clear() 永远走不到，作品栏就卡在旧内容上，
        用户看到的现象是「刷新点了没反应、新视频不出现」。
        """
        if self._thumb_thread is thread:
            self._thumb_thread = None
            self._thumb_worker = None

    def _stop_thumb_worker(self, wait_ms: int = 0) -> None:
        """中止上一轮抽帧（不传 wait_ms 时不阻塞界面）。"""
        worker, thread = self._thumb_worker, self._thumb_thread
        self._thumb_worker, self._thumb_thread = None, None
        if worker is not None:
            worker.stop()  # 会让抽帧循环 kill 掉正在跑的 ffmpeg（<1s 退出）
        if thread is None:
            return
        try:
            thread.quit()
            if wait_ms:
                thread.wait(wait_ms)  # 关闭窗口时真正等它收干净再退
        except RuntimeError:
            pass  # C++ 对象已销毁，没什么可等的

    def _on_thumb_ready(self, path: str, data: object) -> None:
        item = self._gallery_items.get(path)
        if item is None:  # 期间刷新过列表，这条已经没用了
            return
        pix = QPixmap()
        if isinstance(data, bytes) or isinstance(data, bytearray):
            if not pix.loadFromData(bytes(data)):
                return
        elif hasattr(data, "isNull"):  # QImage（图片缩略图）
            if data.isNull():
                return
            pix = QPixmap.fromImage(data)
        else:
            return
        if thumbs.is_video(path):
            pix = pix.scaled(
                _THUMB_BOX, _THUMB_BOX, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
            )
            pix = self._with_play_badge(pix)
        item.setIcon(QIcon(pix))

    @staticmethod
    def _with_play_badge(pix: QPixmap) -> QPixmap:
        """在缩略图左下角盖一个播放标记，一眼区分视频与图片。"""
        out = QPixmap(pix.size())
        out.fill(Qt.transparent)
        painter = QPainter(out)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.drawPixmap(0, 0, pix)
            d = max(22, out.width() // 7)
            x, y = 8, out.height() - d - 8
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 150))
            painter.drawEllipse(x, y, d, d)
            painter.setBrush(QColor(232, 179, 57))  # 主题琥珀金
            painter.drawPolygon(
                QPolygonF(
                    [
                        QPointF(x + d * 0.37, y + d * 0.28),
                        QPointF(x + d * 0.37, y + d * 0.72),
                        QPointF(x + d * 0.74, y + d * 0.50),
                    ]
                )
            )
        finally:
            painter.end()
        return out

    @staticmethod
    def _image_placeholder_icon() -> QIcon:
        """图片缩略图还没解出来时的占位（纯深色方块，像骨架屏）。

        必须有：IconMode 按图标算单元格尺寸，空图标会让回填后的行高错乱。
        """
        size = 160
        pix = QPixmap(size, size)
        pix.fill(QColor(30, 33, 39))  # 比 panel_raised 再暗一点，不抢眼
        return QIcon(pix)

    @staticmethod
    def _video_placeholder_icon() -> QIcon:
        """首帧还没抽出来时的占位图（深色方块 + 金色播放键）。"""
        size = 160
        pix = QPixmap(size, size)
        pix.fill(QColor(38, 41, 49))  # TOKENS["panel_raised"]
        painter = QPainter(pix)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            d = 46
            x = (size - d) // 2
            y = (size - d) // 2
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(232, 179, 57, 60))
            painter.drawEllipse(x, y, d, d)
            painter.setBrush(QColor(232, 179, 57))
            painter.drawPolygon(
                QPolygonF(
                    [
                        QPointF(x + d * 0.38, y + d * 0.30),
                        QPointF(x + d * 0.38, y + d * 0.70),
                        QPointF(x + d * 0.72, y + d * 0.50),
                    ]
                )
            )
        finally:
            painter.end()
        return QIcon(pix)

    def _on_open_file(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.UserRole)
        if path and os.path.isfile(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    # ------------------------------------------------- 画廊右键：复用参数
    def _on_gallery_menu(self, pos) -> None:
        item = self.list_gallery.itemAt(pos)
        if item is None:
            return
        path = item.data(Qt.UserRole)
        if not path or not os.path.isfile(path):
            return
        menu = QMenu(self)
        act_reuse = menu.addAction(i18n.tr("gallery_reuse"))
        menu.addSeparator()
        act_open = menu.addAction(i18n.tr("gallery_open_one"))
        act_folder = menu.addAction(i18n.tr("gallery_open_folder"))
        chosen = menu.exec(self.list_gallery.viewport().mapToGlobal(pos))
        if chosen is act_reuse:
            self._reuse_params(path)
        elif chosen is act_open:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        elif chosen is act_folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))

    def _reuse_params(self, path: str) -> None:
        """从媒体元数据回填参数（PNG tEXt / mp4 ©cmt；旧版 sidecar 文件兜底）。

        顺序与启动恢复一致：先选预设（会重置映射/VAE），再用记录值覆盖，
        否则预设默认值会盖掉要复用的参数。
        """
        from ..engine import embed as embed_mod

        rh = embed_mod.read_params(path)
        if not rh:
            QMessageBox.information(self, i18n.tr("app_title"), i18n.tr("reuse_none"))
            return

        preset_name = str(rh.get("preset_name") or "")
        if preset_name:
            idx = self.combo_preset.findText(preset_name)
            if idx >= 0:
                self.combo_preset.setCurrentIndex(idx)
        if isinstance(rh.get("positive"), str):
            self.txt_positive.setPlainText(rh["positive"])
        if isinstance(rh.get("negative"), str):
            self.txt_negative.setPlainText(rh["negative"])
        if isinstance(rh.get("mapping"), str) and rh["mapping"].strip():
            self.txt_mapping.setPlainText(rh["mapping"])
        if isinstance(rh.get("seed"), int):
            self.spin_seed.setValue(rh["seed"])
        if isinstance(rh.get("random_seed"), bool):
            self.chk_random_seed.setChecked(rh["random_seed"])
        vae_name = os.path.basename(str(rh.get("vae_path") or ""))
        if vae_name:
            idx = self.combo_vae.findText(vae_name)
            if idx >= 0:
                self.combo_vae.setCurrentIndex(idx)
        tier = str(rh.get("video_tier") or "")
        if tier:
            idx = self.combo_video_tier.findData(tier)
            if idx >= 0:
                self.combo_video_tier.setCurrentIndex(idx)
        if isinstance(rh.get("video_fps"), int):
            self.spin_video_fps.setValue(rh["video_fps"])

        self.chk_random_seed.setChecked(False)  # 复用参数 = 想复现，关掉随机种子
        self.spin_seed.setValue(int(rh.get("seed") or 0))
        self.status.setText(
            i18n.tr("reuse_done", name=os.path.basename(path))
        )

    # ----------------------------------------------------------------- vae
    def _reload_vae_list(self) -> None:
        self.combo_vae.clear()
        self.combo_video_vae.clear()
        self.combo_video_vae.addItem(i18n.tr("video_vae_follow"), "")
        self.combo_audio_vae.clear()
        self.combo_audio_vae.addItem(i18n.tr("audio_vae_auto"), "")
        vae_dir = appconfig.vae_dir()
        if os.path.isdir(vae_dir):
            for name in sorted(os.listdir(vae_dir)):
                if name.lower().endswith((".safetensors", ".pt", ".ckpt")):
                    self.combo_vae.addItem(name)
                    self.combo_video_vae.addItem(name, name)
                    self.combo_audio_vae.addItem(name, name)
        if self.combo_vae.count() == 0:
            self.combo_vae.addItem("（VAE 目录为空，请在设置里指定）")
        # 恢复上次的音频/视频 VAE 选择
        for attr, key in (("combo_video_vae", "video_vae"), ("combo_audio_vae", "audio_vae")):
            stored = str(appconfig.get(key, "") or "")
            idx = getattr(self, attr).findData(stored)
            if idx >= 0:
                getattr(self, attr).setCurrentIndex(idx)

    def _selected_audio_vae_path(self) -> str:
        """空 = 自动匹配（H3 音视频 latent 时按文件名在 VAE 目录找）。

        下拉存的是文件名，这里负责拼上 VAE 目录成完整路径。
        """
        name = str(self.combo_audio_vae.currentData() or "")
        if not name:
            return ""
        return os.path.join(appconfig.vae_dir(), name)

    def _selected_video_vae_path(self) -> str:
        """空 = 跟随上方「VAE 模型」（H3 音视频时还有自动匹配兜底）。"""
        name = str(self.combo_video_vae.currentData() or "")
        if not name:
            return ""
        return os.path.join(appconfig.vae_dir(), name)

    # ------------------------------------------------- 图像/视频模式开关
    def _on_video_mode_toggled(self, checked: bool) -> None:
        """大按钮切换：勾选 = 视频模式。

        视频模式下：显示视频设置组、上方「VAE 模型」置灰（由「视频 VAE」
        接管，避免三个 VAE 选项并存）。图像模式下反之。
        解码路由与这个开关无关，仍由云端返回的 latent 类型决定。
        """
        on = bool(checked)
        appconfig.set_value("video_mode", on)
        self.btn_video_mode.setText(
            i18n.tr("video_mode_btn_on") if on else i18n.tr("video_mode_btn_off")
        )
        if hasattr(self, "gb_video"):
            self.gb_video.setVisible(on)
        if hasattr(self, "combo_vae"):
            # 与左侧「每次随机」置灰 spin_seed 的表现一致
            self.combo_vae.setEnabled(not on)

    def _selected_vae_path(self) -> str:
        name = self.combo_vae.currentText()
        if not name or name.startswith("（"):
            return ""
        return os.path.join(appconfig.vae_dir(), name)

    # ------------------------------------------------------------ settings
    def _on_settings(self) -> None:
        dialog = SettingsDialog(self)
        if dialog.exec() == QDialog.Accepted:
            self._reload_vae_list()


class SettingsDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(i18n.tr("settings_title"))
        self.resize(560, 300)
        form = QFormLayout(self)

        self.ed_output = QLineEdit(appconfig.get("output_dir", ""))
        self.ed_vae = QLineEdit(appconfig.get("vae_dir", ""))
        self.ed_comfy = QLineEdit(appconfig.get("comfy_root", ""))
        self.ed_key = QLineEdit(appconfig.api_key())
        self.ed_key.setEchoMode(QLineEdit.Password)
        # 已保存的 Key 直接回填（密码模式显示为圆点）——空栏=真的没设置，
        # 有圆点=已生效；清空后保存=清除 Key。所见即所存，不搞占位符猜谜。
        self.ed_key.setPlaceholderText("粘贴 RunningHub API Key")
        self.cmb_lang = QComboBox()
        self.cmb_lang.addItem("简体中文", "zh")
        self.cmb_lang.addItem("English", "en")
        self.cmb_lang.setCurrentIndex(0 if i18n.lang() == "zh" else 1)
        self.spin_timeout = QSpinBox()
        self.spin_timeout.setRange(60, 100000)
        self.spin_timeout.setValue(int(float(appconfig.get("timeout", 600.0))))
        self.spin_poll = QSpinBox()
        self.spin_poll.setRange(1, 120)
        self.spin_poll.setValue(int(float(appconfig.get("poll_interval", 5.0))))
        self.spin_retries = QSpinBox()
        self.spin_retries.setRange(0, 10)
        self.spin_retries.setValue(int(float(appconfig.get("timeout_retries", 1.0))))
        self.spin_cost = QSpinBox()
        self.spin_cost.setRange(0, 100000)
        self.spin_cost.setValue(int(float(appconfig.get("daily_coin_limit", 0) or 0)))
        self.spin_cost.setToolTip(i18n.tr("cost_limit_hint"))
        # 从插件 config.ini 一次性导入（显式操作，不做隐式回落）
        self.btn_import_key = QPushButton(i18n.tr("settings_import_key"))
        self.btn_import_key.setToolTip(i18n.tr("settings_import_key_hint"))
        self.btn_import_key.clicked.connect(self._on_import_key)

        key_row = QHBoxLayout()
        key_row.setContentsMargins(0, 0, 0, 0)
        key_row.addWidget(self.ed_key, 1)
        key_row.addWidget(self.btn_import_key)
        key_wrap = QWidget()
        key_wrap.setLayout(key_row)
        form.addRow(QLabel("API Key 设置"), key_wrap)
        form.addRow(i18n.tr("settings_output"), self._with_browse(self.ed_output))
        self.ed_vae.setToolTip(
            "存放 VAE 模型的文件夹。点「浏览」后选中任意一个 .safetensors 文件，\n"
            "即自动定位到它所在的目录。"
        )
        form.addRow(i18n.tr("settings_vae"), self._with_vae_browse(self.ed_vae))
        form.addRow(i18n.tr("settings_comfy"), self._with_browse(self.ed_comfy))
        form.addRow(i18n.tr("settings_language"), self.cmb_lang)
        form.addRow(i18n.tr("settings_timeout"), self.spin_timeout)
        form.addRow(i18n.tr("settings_poll"), self.spin_poll)
        form.addRow(i18n.tr("settings_retries"), self.spin_retries)
        form.addRow(i18n.tr("cost_limit"), self.spin_cost)

        # 缩略图缓存：纯派生数据，删了只是下次重新抽首帧/重新解码，不影响任何产物
        self.btn_clear_thumbs = QPushButton(i18n.tr("settings_clear_thumbs"))
        self.btn_clear_thumbs.setToolTip(i18n.tr("settings_clear_thumbs_hint"))
        self.btn_clear_thumbs.clicked.connect(self._on_clear_thumbs)
        form.addRow(i18n.tr("settings_cache"), self.btn_clear_thumbs)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        form.addRow(btns)

    def _on_import_key(self) -> None:
        """从插件 config.ini 读一次 Key 填进本应用（只读，不写回插件）。"""
        try:
            key = str(core_bridge.core_settings().get("api_key") or "").strip()
        except Exception as exc:
            QMessageBox.warning(self, i18n.tr("err_title"), str(exc))
            return
        if not key:
            QMessageBox.information(self, i18n.tr("settings_title"),
                                    i18n.tr("settings_import_none"))
            return
        self.ed_key.setText(key)
        masked = f"{key[:4]}••••{key[-4:]}" if len(key) > 8 else "••••"
        QMessageBox.information(self, i18n.tr("settings_title"),
                                i18n.tr("settings_import_ok", masked=masked))

    def _on_clear_thumbs(self) -> None:
        removed = thumbs.clear_cache()
        QMessageBox.information(
            self,
            i18n.tr("settings_clear_thumbs"),
            i18n.tr("settings_thumbs_cleared", n=removed),
        )

    def _with_browse(self, edit: QLineEdit) -> QWidget:
        wrap = QWidget()
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(edit, 1)
        btn = QPushButton(i18n.tr("settings_pick_dir"))

        def browse() -> None:
            path = QFileDialog.getExistingDirectory(
                self, i18n.tr("settings_pick_dir"), edit.text()
            )
            if path:
                edit.setText(path)

        btn.clicked.connect(browse)
        lay.addWidget(btn)
        return wrap

    def _with_vae_browse(self, edit: QLineEdit) -> QWidget:
        """VAE 目录浏览：选任一 VAE 文件来定位目录（目录选择器看不到文件，容易误会）。"""
        wrap = QWidget()
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(edit, 1)
        btn = QPushButton(i18n.tr("settings_pick_dir"))

        def browse() -> None:
            start = edit.text() or appconfig.get("comfy_root", "")
            path, _filter = QFileDialog.getOpenFileName(
                self,
                "选择任意一个 VAE 文件（将使用它所在的目录）",
                start,
                "VAE 模型 (*.safetensors *.pt *.ckpt *.pth);;所有文件 (*.*)",
            )
            if path:
                edit.setText(os.path.dirname(path))

        btn.clicked.connect(browse)
        lay.addWidget(btn)
        return wrap

    def _save(self) -> None:
        # Key 无条件保存（回填过就是原值；用户清空 = 主动清除）
        appconfig.set_value("api_key", self.ed_key.text().strip())
        appconfig.set_value("output_dir", self.ed_output.text().strip())
        appconfig.set_value("vae_dir", self.ed_vae.text().strip())
        appconfig.set_value("comfy_root", self.ed_comfy.text().strip())
        appconfig.set_value("language", self.cmb_lang.currentData())
        appconfig.set_value("timeout", float(self.spin_timeout.value()))
        appconfig.set_value("poll_interval", float(self.spin_poll.value()))
        appconfig.set_value("timeout_retries", float(self.spin_retries.value()))
        appconfig.set_value("daily_coin_limit", int(self.spin_cost.value()))
        os.makedirs(appconfig.output_dir(), exist_ok=True)
        self.accept()


def runner_params_from_ui(
    preset: dict, mapping_json: str, *, positive: str, negative: str,
    seed: int, random_seed: bool, vae_path: str,
    video_tier: str = "auto", video_fps: int = 16,
    audio_vae_path: str = "", video_vae_path: str = "",
):
    from ..engine.runner import GenerationParams

    return GenerationParams(
        workflow_id=str(preset.get("workflow_id", "")),
        positive=positive,
        negative=negative,
        mapping_json=mapping_json,
        seed=seed,
        random_seed=random_seed,
        vae_path=vae_path,
        preset_name=str(preset.get("name", "")),
        video_tier=str(video_tier or "auto"),
        video_fps=int(video_fps),
        video_crf=int(appconfig.get("video_crf", 17) or 17),
        audio_vae_path=str(audio_vae_path or ""),
        video_vae_path=str(video_vae_path or ""),
    )
