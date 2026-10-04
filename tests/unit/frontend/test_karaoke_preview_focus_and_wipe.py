"""卡拉OK 预览 focus 快路径 / 右键 current 域 / 换行快照置脏回归测试。

- B1：set_focus_position 快路径行号/字符号错位且漏写 _focus_line_idx
- B4：右键菜单直写 _current_line_idx/_current_char_idx 绕过 timing_service
- B8：暂停编辑后 _line_switch_points 不重建 → 局部重绘按过期快照计算
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QObject, QPoint, QRect

from strange_uta_game.backend.domain import Character, Project, Sentence, Singer
from strange_uta_game.frontend.editor.timing import karaoke_preview as preview_module

# 持有 QApplication 强引用：模块夹具结束时若销毁 QApplication，其析构会
# 级联删除 theme 单例，导致后续测试模块构造控件时崩溃（既有测试同型问题）。
_APP_HOLDER = []


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    _APP_HOLDER.append(app)
    yield app


def _sentence(text: str, timestamps, singer_id="s") -> Sentence:
    s = Sentence.from_text(text, singer_id)
    for i, ts in enumerate(timestamps):
        if i < len(s.characters):
            s.characters[i].timestamps = [ts]
            s.characters[i]._update_offset_timestamps()
    return s


def _two_line_project() -> Project:
    project = Project()
    project.singers.clear()
    singer = Singer(name="Default", is_default=True)
    project.singers.append(singer)
    project.add_sentence(_sentence("あいう", [1000, 2000, 3000], singer.id))
    project.add_sentence(_sentence("かきく", [5000, 6000, 7000], singer.id))
    return project


# ═══════════════════════════════════════════════
# B1：set_focus_position 快路径
# ═══════════════════════════════════════════════


class TestFocusPositionFastPath:
    def test_fast_path_updates_focus_line_idx(self, qapp):
        """行==字前置条件满足但行号不同 → 不走快路径；走快路径时必须补写行号。

        旧实现把行号与字符号比较（line_idx == _current_char_idx），第 0 行
        第 0 字等场景误入快路径且不更新 _focus_line_idx，focus 行域残留旧值。
        """
        preview = preview_module.KaraokePreview()
        project = _two_line_project()
        preview.set_project(project)

        # 模拟 focus 行域残留旧值（第 5 行），current 在第 0 行第 0 字
        preview._current_line_idx = 0
        preview._current_char_idx = 0
        preview._scroll_center_line = 0.0
        preview._focus_line_idx = 5
        preview._focus_char_idx = 0
        preview._focus_line_range_end = 5
        preview._focus_char_range_end = 0

        preview.set_focus_position(0, 0)

        assert preview._focus_line_idx == 0
        assert preview._focus_char_idx == 0
        assert preview._focus_line_range_end == 0
        assert preview._focus_char_range_end == 0
        preview.close()

    def test_line_char_mismatch_takes_slow_path(self, qapp):
        """行号 == 当前字符号（旧比较式）不再误入快路径。"""
        preview = preview_module.KaraokePreview()
        preview.set_project(_two_line_project())

        # current 在第 5 行第 0 字；请求 focus 到第 0 行第 0 字
        # （scroll_center 已在 0.0，旧实现因 line_idx(0)==_current_char_idx(0) 误判）
        preview._current_line_idx = 5
        preview._current_char_idx = 0
        preview._scroll_center_line = 0.0
        preview._focus_line_idx = 5

        preview.set_focus_position(0, 0)

        assert preview._focus_line_idx == 0
        preview.close()

    def test_same_position_uses_fast_path_without_cache_warm(self, qapp, monkeypatch):
        """视口已居中且行/字符均未变化 → 走快路径（不触发就近预热）。"""
        preview = preview_module.KaraokePreview()
        preview.set_project(_two_line_project())
        preview._is_playing = True
        preview._current_line_idx = 1
        preview._current_char_idx = 2
        preview._scroll_center_line = 1.0
        warm_calls = []
        monkeypatch.setattr(
            preview, "_warm_nearby_cache", lambda budget: warm_calls.append(budget)
        )

        preview.set_focus_position(1, 2)

        assert warm_calls == []  # 快路径：不预热
        assert preview._focus_line_idx == 1
        assert preview._focus_char_idx == 2
        preview.close()

    def test_changed_position_warms_cache_when_playing(self, qapp, monkeypatch):
        preview = preview_module.KaraokePreview()
        preview.set_project(_two_line_project())
        preview._is_playing = True
        preview._current_line_idx = 1
        preview._current_char_idx = 2
        preview._scroll_center_line = 1.0
        warm_calls = []
        monkeypatch.setattr(
            preview, "_warm_nearby_cache", lambda budget: warm_calls.append(budget)
        )

        preview.set_focus_position(1, 3)  # 字符变化 → 慢路径

        assert warm_calls == [2]
        preview.close()


# ═══════════════════════════════════════════════
# B4：右键菜单不再直写 current 域
# ═══════════════════════════════════════════════


class TestContextMenuUsesCharSelected:
    def _make_preview_with_hitbox(self, qapp):
        preview = preview_module.KaraokePreview()
        preview.set_project(_two_line_project())
        # 伪造一个命中的字符 hitbox：第 1 行第 2 字
        preview._char_hitboxes = [(QRect(0, 0, 10, 10), 1, 2)]
        return preview

    def test_hit_char_emits_char_selected_instead_of_direct_write(self, qapp):
        preview = self._make_preview_with_hitbox(qapp)
        selected = []
        lines = []
        preview.char_selected.connect(lambda l, c: selected.append((l, c)))
        preview.line_clicked.connect(lambda l: lines.append(l))
        # 记录直写前的 current 域
        before = (preview._current_line_idx, preview._current_char_idx)

        preview._show_context_menu(QPoint(100, 100), 5, 5)

        # current 域不再被直写（由 char_selected 链路同步）
        assert (preview._current_line_idx, preview._current_char_idx) == before
        # 走 char_selected / line_clicked 链路
        assert selected == [(1, 2)]
        assert lines == [1]
        preview.close()

    def test_menu_target_still_uses_hit_char(self, qapp, monkeypatch):
        """菜单项（删除等）仍以命中的字符为目标，不受链路改造影响。"""
        preview = self._make_preview_with_hitbox(qapp)
        emitted = []
        preview.delete_chars_requested.connect(
            lambda l, a, b: emitted.append((l, a, b))
        )
        factory = _ExecSpyMenuFactory(preview)
        monkeypatch.setattr(preview_module, "RoundMenu", factory)

        preview._show_context_menu(QPoint(100, 100), 5, 5)
        # 第一个创建的菜单即主菜单，actions[0] = 「删除字符」
        main_menu = factory.menus[0]
        main_menu.actions[0].trigger()

        assert emitted == [(1, 2, 3)]  # 命中字符及其后一个
        preview.close()


class _ExecSpyMenu(QObject):
    """RoundMenu 替身：记录构造与 exec，收集 actions 供触发。

    需为 QObject——Action(text, parent) 要求父对象是 QObject。
    """

    def __init__(self, *args, **kwargs):
        super().__init__()
        self.actions = []

    def addAction(self, action):
        self.actions.append(action)

    def addMenu(self, menu):
        self.actions.append(menu)

    def addSeparator(self):
        pass

    def exec(self, *args, **kwargs):
        pass


class _ExecSpyMenuFactory:
    def __init__(self, preview):
        self.preview = preview
        self.menus = []

    def __call__(self, *args, **kwargs):
        menu = _ExecSpyMenu(*args, **kwargs)
        self.menus.append(menu)
        return menu


# ═══════════════════════════════════════════════
# B8：_line_switch_points 编辑后置脏重建
# ═══════════════════════════════════════════════


class TestLineSwitchPointsDirty:
    def test_edit_marks_snapshot_dirty_and_wipe_rebuilds(self, qapp):
        preview = preview_module.KaraokePreview()
        project = _two_line_project()
        preview.set_project(project)

        assert preview._line_switch_points == [(1000, 0), (5000, 1)]
        assert preview._line_switch_points_dirty is False

        # 暂停编辑：第 1 行起始时间戳 5000 → 1000
        ch = project.sentences[1].characters[0]
        ch.timestamps = [1000]
        ch._update_offset_timestamps()
        preview._invalidate_line_and_dependents(1)

        assert preview._line_switch_points_dirty is True

        rows = preview._wipe_affected_lines(None, 2000)
        # 重建后的快照把 2000ms 归入第 1 行（过期快照仍认为第 1 行 5000ms 起）
        assert rows is not None and 1 in rows
        assert preview._line_switch_points_dirty is False  # 用后清除
        assert preview._line_switch_points == [(1000, 0), (1000, 1)]
        preview.close()

    def test_stale_snapshot_reports_wrong_line_for_time(self, qapp):
        """对照：不重建时，过期快照让 2000ms 定位到旧行（自动滚动/局部重绘失准）。"""
        preview = preview_module.KaraokePreview()
        project = _two_line_project()
        preview.set_project(project)

        ch = project.sentences[1].characters[0]
        ch.timestamps = [1000]
        ch._update_offset_timestamps()
        preview._invalidate_line_and_dependents(1)

        # 模拟"旧实现不重建"：清掉置脏位后查询仍按过期快照
        preview._line_switch_points_dirty = False
        assert preview._find_line_for_time(2000) == 0  # 旧快照：第 1 行仍 5000ms 起

        # 修复后的行为：使用前重建 → 正确定位到第 1 行
        preview._line_switch_points_dirty = True
        assert preview._wipe_affected_lines(None, 2000) is not None
        assert preview._find_line_for_time(2000) == 1
        preview.close()
