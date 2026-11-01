"""Unified desktop entry point; game workspaces retain their own state and storage."""
from pathlib import Path

from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QToolBar
from victoria3_ui import text, section, button


class GameLauncher(QMainWindow):
    def __init__(self, data_dir: Path | None = None):
        super().__init__()
        from app import DATA_DIR
        self.data_dir = Path(data_dir) if data_dir is not None else DATA_DIR
        self.windows = {}
        self._closing = False
        self.setWindowTitle('GameTune · 选择游戏')
        self.resize(1060, 740)
        self.setMinimumSize(760, 600)
        root = QWidget()
        root.setObjectName('root')
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(52, 40, 52, 40)
        layout.setSpacing(18)
        layout.addWidget(text('GameTune', 'brandName'))
        layout.addStretch()
        layout.addWidget(text('今天想调校哪款游戏？', 'pageTitle'))
        layout.addWidget(text('选择游戏，进入对应的设置、性能测试与结果工作区。'))
        layout.addSpacing(12)
        self.buttons = {}
        for game, title, description, detail in (
            ('cs2', 'Counter-Strike 2', '从帧率与流畅度出发，逐项验证设置调整。', '基线录入  /  单项诊断  /  复测与回滚'),
            ('victoria3', '维多利亚 3', '同时关注画面表现与游戏日期推进速度。', '配置管理  /  自动性能采集  /  配置对比'),
        ):
            card, content = section(title, description)
            content.addWidget(text(detail, 'sectionMeta'))
            row = QHBoxLayout()
            row.addStretch()
            self.buttons[game] = button('进入调校' if game == 'cs2' else '进入工作区', lambda checked=False, key=game: self.open_game(key))
            row.addWidget(self.buttons[game])
            content.addLayout(row)
            layout.addWidget(card)
        layout.addStretch()
        layout.addWidget(text('测试记录保存在本机。切换游戏后，已打开的工作区会保留。', 'sectionMeta'))

    def open_game(self, game):
        if game not in ('cs2', 'victoria3'):
            raise ValueError('未知游戏')
        if game not in self.windows:
            from app import MainWindow, load_saved_api_config, provider_from_env, OpenAICompatibleProvider
            if game == 'cs2':
                window = MainWindow(self.data_dir / 'gametune.db', include_victoria3=False)
                window.setWindowTitle('GameTune · Counter-Strike 2')
            else:
                from victoria3_workspace import ConfigurationWindow
                try:
                    provider = provider_from_env()
                except ValueError:
                    provider = None
                if provider is None:
                    config = load_saved_api_config()
                    provider = OpenAICompatibleProvider(config) if config else None
                window = ConfigurationWindow(self.data_dir / 'victoria3.db', provider=provider)
            toolbar = QToolBar('游戏导航', window)
            toolbar.setMovable(False)
            toolbar.setFloatable(False)
            toolbar.addWidget(button('返回游戏选择', lambda: self.return_to_selection(game)))
            window.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)
            self.windows[game] = window
            window.installEventFilter(self)
        window = self.windows[game]
        window.show()
        window.raise_()
        self.hide()

    def return_to_selection(self, game):
        window = self.windows[game]
        capture = getattr(window, 'capture_page', None)
        if capture is not None and (capture.job or capture.dates.worker or capture.handoff or capture.dates.hotkey or (capture.dates.picker and capture.dates.picker.isVisible())):
            window.statusBar().showMessage('请先结束采集、日期识别或权限切换，再返回游戏选择。', 5000)
            return
        self.show()
        window.hide()
        self.raise_()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Close and not self._closing:
            for game, window in self.windows.items():
                if watched is window:
                    self.return_to_selection(game)
                    event.ignore()
                    return True
        return super().eventFilter(watched, event)

    def closeEvent(self, event):
        self._closing = True
        for window in self.windows.values():
            if not window.close():
                self._closing = False
                window.show()
                self.hide()
                event.ignore()
                return
        super().closeEvent(event)
