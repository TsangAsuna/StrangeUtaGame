"""领域命令修复回归测试（C9）。

覆盖：
- UpdateCharacterCommand.check_count 走 set_check_count 权威 setter，
  undo 逐位还原 timestamps / ruby（setattr 绕过 setter 的缺陷）
- RemoveSingerCommand.undo 还原句子顺序与逐字 singer_id
"""

from strange_uta_game.backend.application.commands import (
    RemoveSingerCommand,
    SetTimestampCommand,
    UpdateCharacterCommand,
)
from strange_uta_game.backend.domain import (
    Character,
    Project,
    Ruby,
    RubyPart,
    Sentence,
    Singer,
)


def _make_project_with_ruby_char():
    """'あ' (check_count=3, ruby 3 mora) + 'い'"""
    project = Project()
    singer = project.singers[0]
    ruby = Ruby(parts=[RubyPart("あ"), RubyPart("ど"), RubyPart("れ")])
    char = Character(char="あ", check_count=3, singer_id=singer.id, ruby=ruby)
    char2 = Character(char="い", check_count=1, singer_id=singer.id)
    sentence = Sentence(singer_id=singer.id, characters=[char, char2])
    project.add_sentence(sentence)
    return project, sentence, char


class TestUpdateCharacterCommandCheckCount:
    def test_shrink_routes_through_set_check_count(self):
        """check_count 缩减：timestamps 截断 + ruby.parts 合并尾段（不绕 setter）"""
        project, sentence, char = _make_project_with_ruby_char()

        cmd = UpdateCharacterCommand(project, sentence.id, 0, check_count=1)
        cmd.execute()

        assert char.check_count == 1
        assert len(char.timestamps) <= 1
        # ruby 文本不丢失，parts 收缩为 1 段
        assert char.ruby.text == "あどれ"
        assert len(char.ruby.parts) == 1

    def test_undo_restores_timestamps_and_ruby_parts_exactly(self):
        project, sentence, char = _make_project_with_ruby_char()
        char.add_timestamp(1000, 0)
        char.add_timestamp(1500, 1)
        char.add_timestamp(2000, 2)

        cmd = UpdateCharacterCommand(project, sentence.id, 0, check_count=2)
        cmd.execute()
        assert char.check_count == 2
        assert len(char.ruby.parts) == 2

        cmd.undo()
        # 逐位还原：check_count / timestamps / ruby 分段
        assert char.check_count == 3
        assert char.timestamps == [1000, 1500, 2000]
        assert [p.text for p in char.ruby.parts] == ["あ", "ど", "れ"]

    def test_undo_restores_plain_attrs(self):
        project, sentence, char = _make_project_with_ruby_char()

        cmd = UpdateCharacterCommand(
            project, sentence.id, 0, linked_to_next=True, is_line_end=True
        )
        cmd.execute()
        assert char.linked_to_next is True
        assert char.is_line_end is True

        cmd.undo()
        assert char.linked_to_next is False
        assert char.is_line_end is False


class TestRemoveSingerCommandUndo:
    def _two_singer_project(self):
        project = Project()
        default = project.singers[0]
        other = Singer(name="B")
        project.add_singer(other)
        return project, default, other

    def test_undo_cascade_restores_sentence_order(self):
        """级联删除（无 transfer）：undo 按原位置还原被删句子"""
        project, default, other = self._two_singer_project()
        s1 = Sentence.from_text("1", other.id)
        s2 = Sentence.from_text("2", default.id)
        s3 = Sentence.from_text("3", other.id)
        project.add_sentence(s1)
        project.add_sentence(s2)
        project.add_sentence(s3)

        cmd = RemoveSingerCommand(project, other.id)
        cmd.execute()
        assert [s.text for s in project.sentences] == ["2"]

        cmd.undo()
        # 顺序还原：被删句子回到原位置
        assert [s.text for s in project.sentences] == ["1", "2", "3"]
        assert project.get_singer(other.id) is not None

    def test_undo_transfer_restores_per_char_singer_ids(self):
        """转移删除：undo 还原句级与逐字 singer_id"""
        project, default, other = self._two_singer_project()
        s = Sentence.from_text("ab", other.id)
        project.add_sentence(s)

        cmd = RemoveSingerCommand(project, other.id, transfer_to=default.id)
        cmd.execute()
        # 句级与逐字都被改写为 default
        assert s.singer_id == default.id
        assert all(ch.singer_id == default.id for ch in s.characters)

        cmd.undo()
        assert s.singer_id == other.id
        assert all(ch.singer_id == other.id for ch in s.characters)
        assert project.get_singer(other.id) is not None


class TestSetTimestampCommand:
    def test_execute_undo_redo(self):
        project = Project()
        singer = project.singers[0]
        char = Character(char="あ", check_count=1, singer_id=singer.id)
        sentence = Sentence(singer_id=singer.id, characters=[char])
        project.add_sentence(sentence)
        char.add_timestamp(1000, 0)

        cmd = SetTimestampCommand(
            project, sentence.id, 0, 0, old_ts=1000, new_ts=1250
        )
        cmd.execute()
        assert char.timestamps == [1250]

        cmd.undo()
        assert char.timestamps == [1000]

        cmd.redo()
        assert char.timestamps == [1250]

    def test_out_of_range_raises_and_keeps_value(self):
        project = Project()
        singer = project.singers[0]
        char = Character(char="あ", check_count=2, singer_id=singer.id)
        sentence = Sentence(singer_id=singer.id, characters=[char])
        project.add_sentence(sentence)
        char.add_timestamp(1000, 0)

        cmd = SetTimestampCommand(
            project, sentence.id, 0, 1, old_ts=0, new_ts=500
        )
        try:
            cmd.execute()
            raised = False
        except ValueError:
            raised = True
        assert raised
        # 写入失败不破坏现有数据
        assert char.timestamps == [1000]
