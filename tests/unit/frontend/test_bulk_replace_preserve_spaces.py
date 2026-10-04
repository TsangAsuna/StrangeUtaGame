"""批量替换写回保留原文回归测试（bug 审查 B9：bulk_change_dialog.py）。

替换文本 .strip() 会吞掉有义空格，且把同长度原地改误判成换长度
（长度比较用未 strip 文本）；搜索词仍按 strip 后匹配。
"""

from __future__ import annotations

import pytest

from strange_uta_game.backend.domain import Project, Sentence, Singer
from strange_uta_game.frontend.editor.timing.bulk_change_dialog import BulkChangeDialog

# 持有 QApplication 强引用：模块夹具结束时若销毁 QApplication，其析构会
# 级联删除 theme 单例，导致后续测试模块构造控件时崩溃（既有测试同型问题）。
_APP_HOLDER = []


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    _APP_HOLDER.append(app)
    yield app


def _project_with_line(text: str) -> Project:
    project = Project()
    project.singers.clear()
    singer = Singer(name="Default", is_default=True)
    project.singers.append(singer)
    project.add_sentence(Sentence.from_text(text, singer.id))
    return project


class TestBulkReplacePreservesSpaces:
    def test_same_length_write_back_keeps_char(self, qapp):
        """同长度替换按原地修改写回（未 strip 文本参与长度比较）。"""
        project = _project_with_line("あいう")
        bulk = BulkChangeDialog(project, initial_word="い")
        bulk.edit_new_chars.setText("X")  # 1 字 == 1 字 → 同长度原地改

        bulk._on_execute()

        assert project.sentences[0].characters[1].char == "X"
        bulk.close()

    def test_two_char_replacement_is_length_change(self, qapp, monkeypatch):
        """搜索 1 字替换 2 字：长度比较用未 strip 文本 → 走换长度确认流程。"""
        project = _project_with_line("あいう")
        bulk = BulkChangeDialog(project, initial_word="あ")
        bulk.edit_new_chars.setText("xあ")  # 未 strip 长度 2 ≠ 1 → 换长度
        confirmed = []
        monkeypatch.setattr(
            "strange_uta_game.frontend.editor.timing.bulk_change_dialog.message_question",
            lambda *a, **k: confirmed.append(1) or True,
        )

        bulk._on_execute()

        assert confirmed == [1]  # 换长度确认弹窗确实被触发（未误判为同长度）
        chars = [c.char for c in project.sentences[0].characters]
        assert chars[:2] == ["x", "あ"]
        bulk.close()

    def test_match_word_still_stripped(self, qapp):
        """搜索词仍按 strip 后匹配（匹配口径保持不变）。"""
        project = _project_with_line("あいう")
        bulk = BulkChangeDialog(project, initial_word="い")
        bulk.edit_new_chars.setText("X")

        bulk._on_execute()

        assert project.sentences[0].characters[1].char == "X"
        bulk.close()
