"""字符编辑对话框连词校验 / 结果映射 / 原文写回回归测试（bug 审查 B3/B7/B9）。

- B3：CharEditDialog 连词直写无末字/行尾校验；换长度不应用连词勾选、
  不回填 is_sentence_end/is_line_end（与 ModifyCharacterDialog 同口径）
- B7：SetSingerByLineDialog.result_map 引用未赋值的 self._result_map
- B9：写回文本 .strip() 吞有义空格（长度比较用未 strip 文本）
"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QCheckBox

from strange_uta_game.backend.domain import Project, Sentence, Singer
from strange_uta_game.frontend.editor.timing.dialogs import (
    CharEditDialog,
    ModifyCharacterDialog,
    SetSingerByLineDialog,
)

# 持有 QApplication 强引用：模块夹具结束时若销毁 QApplication，其析构会
# 级联删除 theme 单例，导致后续测试模块构造控件时崩溃（既有测试同型问题）。
_APP_HOLDER = []


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    _APP_HOLDER.append(app)
    yield app


def _sentence(text: str, singer_id="singer") -> Sentence:
    return Sentence.from_text(text, singer_id)


def _sentence_with_line_end(text: str, **last_flags) -> Sentence:
    s = _sentence(text)
    last = s.characters[-1]
    for flag, value in last_flags.items():
        setattr(last, flag, value)
    return s


# ═══════════════════════════════════════════════
# B3：CharEditDialog 连词校验与标志回填
# ═══════════════════════════════════════════════


class TestCharEditDialogLinkedValidation:
    def test_same_length_last_char_link_request_is_rejected(self, qapp):
        """原地改：末字的连词勾选被校验跳过并记录 failures（旧实现直写连词）。"""
        s = _sentence("あい")
        dialog = CharEditDialog(s, 1)  # 末字「い」
        dialog.edit_new_chars.setText("X")  # 同长度原地改
        dialog._char_rows[0][3].setChecked(True)  # 勾选向后连词

        dialog._on_accept()

        assert s.characters[1].char == "X"
        # 末字禁止连词 → 强制 False 且记录失败项
        assert s.characters[1].linked_to_next is False
        failures = dialog.get_linked_failures()
        assert len(failures) == 1
        abs_idx, ch, reason = failures[0]
        assert abs_idx == 1 and ch == "X" and "最后一个字符" in reason
        dialog.close()

    def test_same_length_middle_char_link_allowed(self, qapp):
        s = _sentence("あい")
        dialog = CharEditDialog(s, 0)  # 首字「あ」
        dialog.edit_new_chars.setText("X")
        dialog._char_rows[0][3].setChecked(True)

        dialog._on_accept()

        assert s.characters[0].linked_to_next is True
        assert dialog.get_linked_failures() == []
        dialog.close()

    def test_length_change_applies_link_requests(self, qapp):
        """换长度：新字符按用户勾选应用连词（旧实现一律 linked_to_next=False）。"""
        s = _sentence("あい")
        dialog = CharEditDialog(s, 0)
        dialog.edit_new_chars.setText("XYZ")  # 1 字 → 3 字
        dialog._char_rows[0][3].setChecked(True)
        dialog._char_rows[1][3].setChecked(True)
        dialog._char_rows[2][3].setChecked(False)

        dialog._on_accept()

        assert [c.char for c in s.characters] == ["X", "Y", "Z", "い"]
        assert s.characters[0].linked_to_next is True
        assert s.characters[1].linked_to_next is True
        assert s.characters[2].linked_to_next is False
        dialog.close()

    def test_length_change_backfills_line_end_and_sentence_end(self, qapp):
        """换长度：旧末字的 is_line_end / is_sentence_end 回填到新末字。"""
        s = _sentence_with_line_end("あい", is_line_end=True, is_sentence_end=True)
        dialog = CharEditDialog(s, 1)  # 末字「い」
        dialog.edit_new_chars.setText("XY")  # 1 字 → 2 字

        dialog._on_accept()

        assert [c.char for c in s.characters] == ["あ", "X", "Y"]
        assert s.characters[-1].is_line_end is True
        assert s.characters[-1].is_sentence_end is True
        # 非末字不携带标志
        assert s.characters[1].is_line_end is False
        assert s.characters[1].is_sentence_end is False
        dialog.close()

    def test_length_change_line_end_flag_blocks_link(self, qapp):
        """换长度：回填行尾标志后的新末字，其连词勾选被校验跳过。"""
        s = _sentence_with_line_end("あい", is_line_end=True)
        dialog = CharEditDialog(s, 1)
        dialog.edit_new_chars.setText("XY")
        dialog._char_rows[0][3].setChecked(True)
        dialog._char_rows[1][3].setChecked(True)

        dialog._on_accept()

        # 第 2 个新字符既是句末字（回填后仍在行尾）→ 连词被跳过并记录
        assert s.characters[1].linked_to_next is True
        assert s.characters[2].linked_to_next is False
        failures = dialog.get_linked_failures()
        assert len(failures) == 1 and failures[0][0] == 2
        dialog.close()

    def test_write_back_preserves_meaningful_spaces(self, qapp):
        """B9：CharEditDialog 写回保留原文（不 strip）。"""
        s = _sentence("あい")
        dialog = CharEditDialog(s, 0)
        dialog.edit_new_chars.setText("x y")  # 含实义空格，长度不变

        dialog._on_accept()

        assert [c.char for c in s.characters] == ["x", " ", "y", "い"]
        dialog.close()


# ═══════════════════════════════════════════════
# B9：ModifyCharacterDialog 写回原文
# ═══════════════════════════════════════════════


class TestModifyCharacterDialogPreservesSpaces:
    def test_same_length_write_back_keeps_trailing_space(self, qapp):
        """同长度原地改：尾部空格保留，不再被 strip 吞掉。"""
        s = _sentence("あいう")
        dialog = ModifyCharacterDialog(s, 0, 2)
        dialog.edit_new_chars.setText("XY ")  # 3 字（旧实现 strip 后按换长度处理）

        dialog._on_execute()

        # 同长度原地改 → 第 3 字是空格本体
        assert [c.char for c in s.characters] == ["X", "Y", " "]
        dialog.close()

    def test_length_change_with_spaces_replaces_slice(self, qapp):
        """长度比较用未 strip 文本：带空格的文本按其实际长度走换长度流程。"""
        s = _sentence("あいう")
        dialog = ModifyCharacterDialog(s, 0, 2)
        dialog.edit_new_chars.setText("XYZW")  # 4 字 ≠ 3 字

        dialog._on_execute()

        assert [c.char for c in s.characters] == ["X", "Y", "Z", "W"]
        dialog.close()


# ═══════════════════════════════════════════════
# B7：SetSingerByLineDialog.result_map
# ═══════════════════════════════════════════════


class TestSetSingerByLineDialogResultMap:
    def _make_dialog(self):
        project = Project()
        project.singers.clear()
        singer = Singer(name="Alpha", is_default=True)
        project.singers.append(singer)
        sentences = [_sentence("あい", singer.id), _sentence("かき", singer.id)]
        return SetSingerByLineDialog(sentences, [singer]), singer

    def test_result_map_no_attribute_error_after_apply(self, qapp):
        """应用后 result_map() 不再 AttributeError（旧实现引用未赋值属性）。"""
        dialog, singer = self._make_dialog()
        dialog.singer_list.item(0).setSelected(True)
        chk = dialog.table.cellWidget(0, 0).findChild(QCheckBox)
        chk.setChecked(True)

        dialog._on_apply()

        assert dialog.result_map() == {0: singer.id}
        dialog.close()

    def test_result_map_accumulates_multiple_applies(self, qapp):
        """对话框不关闭、可多次应用 → result_map 累计历次结果。"""
        dialog, singer = self._make_dialog()
        dialog.singer_list.item(0).setSelected(True)

        chk0 = dialog.table.cellWidget(0, 0).findChild(QCheckBox)
        chk0.setChecked(True)
        dialog._on_apply()
        chk1 = dialog.table.cellWidget(1, 0).findChild(QCheckBox)
        chk1.setChecked(True)
        dialog._on_apply()

        assert dialog.result_map() == {0: singer.id, 1: singer.id}
        dialog.close()
