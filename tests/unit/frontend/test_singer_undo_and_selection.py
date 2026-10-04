"""演唱者界面：撤销登记 / 选中清理 / 默认开关 / 颜色槽位 / 名称校验。

覆盖 H4 / H7 / H8 / H9 / H13（名称校验入口）：
- `_push_singers_undo` 把演唱者操作登记到共享 CommandManager，可撤销（H4）；
- `_notify_singers_changed` 清理已删除演唱者残留的僵尸选中 ID（H7）；
- 编辑已默认演唱者时「设为默认」开关置灰（H8）；
- 「从已有演唱者加载颜色」后激活槽位复位（H9）；
- 演唱者名含逗号等危险字符时确定被拒绝（H13）。
"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QApplication, QDialog, QMenu

import strange_uta_game.frontend.singer.singer_interface as si_mod
from strange_uta_game.backend.application import CommandManager
from strange_uta_game.backend.domain import Project, Singer
from strange_uta_game.frontend.singer.singer_interface import (
    SingerEditDialog,
    SingerManagerInterface,
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _InfoBarRecorder:
    calls: list = []

    @classmethod
    def warning(cls, **kw):
        cls.calls.append(("warning", kw))

    @classmethod
    def error(cls, **kw):
        cls.calls.append(("error", kw))

    @classmethod
    def success(cls, **kw):
        cls.calls.append(("success", kw))

    @classmethod
    def info(cls, **kw):
        cls.calls.append(("info", kw))


def _make_page(qapp) -> tuple[SingerManagerInterface, Project]:
    page = SingerManagerInterface()
    project = Project(
        singers=[
            Singer(name="A", color="#FF0000", is_default=True),
            Singer(name="B", color="#00FF00"),
        ]
    )
    page.set_project(project)
    return page, project


def test_push_singers_undo_registers_and_restores(qapp):
    """操作经 helper 登记 undo：undo 还原 singers 快照。"""
    page, project = _make_page(qapp)
    cm = CommandManager()
    # 独立 page 的 window() 是自身 → 直接挂 _timing_service 模拟共享服务
    page._timing_service = SimpleNamespace(command_manager=cm)

    before = deepcopy(project.singers)
    page._singer_service.add_singer(name="C", color="#0000FF")
    page._push_singers_undo(before, "添加演唱者 C")

    assert [s.name for s in project.singers] == ["A", "B", "C"]
    assert cm.can_undo()
    cm.undo()
    assert [s.name for s in project.singers] == ["A", "B"]


def test_push_singers_undo_skipped_without_shared_manager(qapp):
    """没有共享 CommandManager（独立窗口）时静默跳过，不报错。"""
    page, project = _make_page(qapp)
    before = deepcopy(project.singers)
    page._singer_service.add_singer(name="C", color="#0000FF")
    page._push_singers_undo(before, "添加演唱者 C")
    assert [s.name for s in project.singers] == ["A", "B", "C"]


def test_delete_registers_undo_with_sentences(qapp):
    """删除（句子转移）路径的 undo：singers 与句子归属一并还原。"""
    page, project = _make_page(qapp)
    page._timing_service = SimpleNamespace(command_manager=CommandManager())
    cm = page._get_shared_command_manager()
    b = project.singers[1]

    from strange_uta_game.backend.domain import Sentence
    project.sentences = [Sentence.from_text("あいう", b.id)]
    before_singers = deepcopy(project.singers)
    before_sentences = deepcopy(project.sentences)

    assert page._singer_service.batch_remove_singers([b.id], project.singers[0].id)
    page._push_singers_undo(
        before_singers, "删除 1 位演唱者", before_sentences=before_sentences
    )
    assert [s.name for s in project.singers] == ["A"]

    cm.undo()
    assert [s.name for s in project.singers] == ["A", "B"]
    assert project.sentences[0].singer_id == b.id


def test_notify_singers_changed_prunes_zombie_selected_ids(qapp):
    """删除演唱者后 `_selected_ids` 不再残留僵尸 ID（H7）。"""
    page, project = _make_page(qapp)
    a, b = project.singers
    page._selected_ids |= {a.id, b.id}

    # 模拟 B 已被删除（转移给 A）
    project.remove_singer(b.id, transfer_to=a.id)
    page._notify_singers_changed()

    assert page._selected_ids == {a.id}


def test_default_switch_disabled_when_already_default(qapp):
    """编辑已默认演唱者：「设为默认」开关置灰并带提示（H8）。"""
    singer = Singer(name="A", is_default=True)
    dlg = SingerEditDialog(singer)
    assert dlg.chk_default.isChecked()
    assert not dlg.chk_default.isEnabled()
    assert dlg.chk_default.toolTip()

    # 新增场景开关可用
    dlg2 = SingerEditDialog()
    assert dlg2.chk_default.isEnabled()


def test_load_from_singer_resets_active_split_idx(qapp, monkeypatch):
    """加载颜色后 `_active_split_idx` 复位到 0，不再指向越界槽位（H9）。"""
    src = Singer(name="来源", color="#123456")  # 单色、无分色
    dlg = SingerEditDialog(existing_singers=[src])
    # 切到分色（自动补一个对比色，共 2 槽位）并激活末位
    dlg._rb_split.setChecked(True)
    dlg._on_mode_changed()
    dlg._set_active_split_idx(1)
    assert dlg._active_split_idx == 1

    # 拦截 QMenu.exec：返回指向 src 的伪 action，模拟用户选择
    class _FakeAction:
        def __init__(self, data):
            self._data = data

        def data(self):
            return self._data

    monkeypatch.setattr(QMenu, "exec", lambda self, *a, **k: _FakeAction(src))
    dlg._on_load_from_singer()

    assert dlg._active_split_idx == 0
    assert dlg._color == "#123456"
    assert dlg._split_colors == []


def test_name_validation_rejects_dangerous_chars(qapp, monkeypatch):
    """名称含逗号等列分隔/标签字符时确定被拒绝（H13 名称校验入口）。"""
    dlg = SingerEditDialog()
    monkeypatch.setattr(si_mod, "InfoBar", _InfoBarRecorder)
    _InfoBarRecorder.calls.clear()

    dlg.line_name.setText("A,B")
    dlg.accept()
    assert dlg.result() != int(QDialog.DialogCode.Accepted)
    warnings = [kw for kind, kw in _InfoBarRecorder.calls if kind == "warning"]
    assert warnings and "非法字符" in warnings[0]["title"]

    # 合法名称可正常接受
    dlg.line_name.setText("AB")
    dlg.accept()
    assert dlg.result() == int(QDialog.DialogCode.Accepted)
