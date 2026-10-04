"""全文本编辑界面：撤销登记与未应用修改保护。

覆盖 H1 / H4：
- `_on_apply_changes` 的整体 sentences 替换登记为 SentenceSnapshotCommand，
  可从共享 CommandManager 撤销还原（H4）；
- 后台注音任务完成时的 `_confirm_keep_dirty_edits`：无未应用修改直接放行，
  有未应用修改时按用户选择放行/保留（H1）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QApplication

import strange_uta_game.frontend.editor.fulltext_interface as ft_mod
from strange_uta_game.backend.application import CommandManager
from strange_uta_game.backend.domain import (
    Character,
    Project,
    Ruby,
    RubyPart,
    Sentence,
    Singer,
)
from strange_uta_game.frontend.editor.fulltext_interface import RubyInterface


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _ruby_char(ch, moras, ts, *, linked=False, singer="", end_ts=None):
    c = Character(
        char=ch,
        check_count=len(moras),
        timestamps=list(ts),
        linked_to_next=linked,
        is_sentence_end=end_ts is not None,
        sentence_end_ts=end_ts,
        singer_id=singer,
    )
    c.set_ruby(Ruby(parts=[RubyPart(text=m) for m in moras]))
    c.push_to_ruby()
    return c


def _build_project():
    p = Project()
    p.global_offset_ms = 300
    taro = Singer(name="太郎", is_default=True)
    hanako = Singer(name="花子")
    p.singers = [taro, hanako]
    line0 = Sentence(
        singer_id=taro.id,
        characters=[
            _ruby_char("大", ["だ", "い"], [1000, 1200], linked=True, singer=taro.id),
            _ruby_char("険", ["け", "ん"], [1800, 2000], singer=taro.id),
        ],
    )
    c = Character(
        char="あ", check_count=1, timestamps=[500],
        is_sentence_end=True, sentence_end_ts=900, singer_id=hanako.id,
    )
    line1 = Sentence(singer_id=hanako.id, characters=[c])
    p.sentences = [line0, line1]
    return p, taro, hanako


def _snapshot(p):
    return [
        [
            (ch.char, list(ch.timestamps), ch.sentence_end_ts,
             [pt.text for pt in ch.ruby.parts] if ch.ruby else None,
             ch.linked_to_next, ch.singer_id)
            for ch in s.characters
        ]
        for s in p.sentences
    ]


def test_apply_changes_registers_undo(qapp):
    """应用更改后撤销栈有记录；undo 完整还原到应用前的句子内容。"""
    p, _taro, _hanako = _build_project()
    before = _snapshot(p)
    w = RubyInterface()
    w.set_project(p)
    cm = CommandManager()
    w._timing_service = SimpleNamespace(command_manager=cm)  # window() == 自身

    lines = w.text_edit.toPlainText().split("\n")
    w.text_edit.setPlainText("\n".join(lines) + "\nなにぬ")
    assert w.is_dirty()
    w._on_apply_changes()

    assert len(p.sentences) == 3
    assert cm.can_undo()
    cm.undo()
    assert _snapshot(p) == before


def test_push_undo_skipped_without_command_manager(qapp):
    """沿父链找不到打轴服务时静默跳过（不报错、不产生撤销条目）。"""
    p, _taro, _hanako = _build_project()
    w = RubyInterface()  # 无父链、无 _timing_service
    w.set_project(p)
    w._push_sentences_undo([], "不应登记")
    assert w._find_timing_service() is None


def test_confirm_dirty_choices(qapp, monkeypatch):
    """`_confirm_keep_dirty_edits`：不脏直接放行；脏时按弹窗选择决定。"""
    p, _taro, _hanako = _build_project()
    w = RubyInterface()
    w.set_project(p)

    # 无未应用修改 → 放行（不弹窗）
    assert w.is_dirty() is False
    assert w._confirm_keep_dirty_edits("测试任务") is True

    # 有未应用修改 → 走弹窗
    w.text_edit.setPlainText(w.text_edit.toPlainText() + "\nなにぬ")
    assert w.is_dirty()
    choices = iter([0])  # 「覆盖修改」→ 放行
    monkeypatch.setattr(ft_mod, "message_choice", lambda *a, **k: next(choices))
    assert w._confirm_keep_dirty_edits("测试任务") is True

    choices = iter([1])  # 「保留修改」→ 不放行
    monkeypatch.setattr(ft_mod, "message_choice", lambda *a, **k: next(choices))
    assert w._confirm_keep_dirty_edits("测试任务") is False

    choices = iter([-1])  # Esc/关闭 → 不放行
    monkeypatch.setattr(ft_mod, "message_choice", lambda *a, **k: next(choices))
    assert w._confirm_keep_dirty_edits("测试任务") is False
