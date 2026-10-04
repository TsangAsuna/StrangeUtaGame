"""FindDialog 关闭后的僵尸连接清理（H6）。

关闭后的 FindDialog 仍以父窗口存活；若 textChanged 连接不断开，
每次编辑都会在 250ms 防抖后重跑搜索、把已清掉的高亮"复活"。
"""

from __future__ import annotations

import pytest
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from strange_uta_game.backend.domain import Character, Project, Sentence, Singer
from strange_uta_game.frontend.editor.fulltext_interface import RubyInterface


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _make_page(qapp):
    p = Project()
    taro = Singer(name="太郎", is_default=True)
    p.singers = [taro]
    p.sentences = [
        Sentence(
            singer_id=taro.id,
            characters=[
                Character(char="大", check_count=1, timestamps=[1000], singer_id=taro.id),
                Character(char="冒", check_count=1, timestamps=[1200], singer_id=taro.id),
                Character(char="険", check_count=1, timestamps=[1400], singer_id=taro.id),
            ],
        )
    ]
    w = RubyInterface()
    w.set_project(p)
    return w


def test_closing_find_dialog_clears_highlights(qapp):
    w = _make_page(qapp)
    w._open_find()
    dlg = w._find_dialog
    assert dlg is not None and dlg.isVisible()

    dlg._search_input.setText("大")
    dlg._on_search()
    assert w._find_highlights, "搜索命中后应有高亮"

    dlg.close()
    assert w._find_highlights == []
    assert w._find_dialog is None


def test_closed_find_dialog_does_not_revive_highlights(qapp):
    """关闭后编辑文本：防抖窗口内不再重跑搜索复活高亮（修复前会复活）。"""
    w = _make_page(qapp)
    w._open_find()
    dlg = w._find_dialog
    dlg._search_input.setText("大")
    dlg._on_search()
    assert w._find_highlights

    dlg.close()
    assert w._find_highlights == []

    # 模拟僵尸路径：关闭后编辑器发出 textChanged，越过 250ms 防抖窗口
    w.text_edit.textChanged.emit()
    QTest.qWait(320)
    qapp.processEvents()
    assert w._find_highlights == []


def test_reopen_after_close_creates_fresh_dialog(qapp):
    w = _make_page(qapp)
    w._open_find()
    first = w._find_dialog
    first.close()

    w._open_find()
    second = w._find_dialog
    assert second is not None and second is not first
    second.close()
    assert w._find_dialog is None
