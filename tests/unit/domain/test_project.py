import pytest
from datetime import datetime
from strange_uta_game.backend.domain import (
    Character,
    Project,
    ProjectMetadata,
    Singer,
    Sentence,
    ValidationError,
    DomainError,
)


class TestProject:
    def test_creation_with_defaults(self):
        project = Project()
        assert project.id is not None
        assert len(project.sentences) == 0
        assert len(project.singers) == 1
        assert project.audio_duration_ms == 0
        assert isinstance(project.metadata, ProjectMetadata)

        default_singer = project.singers[0]
        assert default_singer.is_default is True
        assert default_singer.is_placeholder is True
        assert default_singer.name == "未命名"
        assert default_singer.backend_number == 1

    def test_add_singer(self):
        project = Project()
        new_singer = Singer(name="和声", color="#4ECDC4")
        project.add_singer(new_singer)
        assert len(project.singers) == 2

    def test_remove_singer_with_cascade(self):
        project = Project()
        singer = Singer(name="和声")
        project.add_singer(singer)

        s = Sentence.from_text("测试", singer.id)
        project.add_sentence(s)
        assert len(project.sentences) == 1

        project.remove_singer(singer.id)
        assert len(project.sentences) == 0

    def test_remove_singer_with_transfer(self):
        project = Project()
        default_singer = project.get_default_singer()
        singer = Singer(name="和声")
        project.add_singer(singer)

        s = Sentence.from_text("测试", singer.id)
        project.add_sentence(s)

        project.remove_singer(singer.id, transfer_to=default_singer.id)
        assert len(project.sentences) == 1
        assert project.sentences[0].singer_id == default_singer.id

    def test_add_sentence(self):
        project = Project()
        singer = project.get_default_singer()
        s = Sentence.from_text("测试", singer.id)
        project.add_sentence(s)
        assert len(project.sentences) == 1
        assert project.sentences[0].text == "测试"

    def test_move_sentence(self):
        project = Project()
        singer = project.get_default_singer()
        s1 = Sentence.from_text("1", singer.id)
        s2 = Sentence.from_text("2", singer.id)
        project.add_sentence(s1)
        project.add_sentence(s2)

        project.move_sentence(s2.id, 0)
        assert project.sentences[0].text == "2"
        assert project.sentences[1].text == "1"

    def test_get_all_timestamps(self):
        project = Project()
        singer = project.get_default_singer()
        s = Sentence.from_text("AB", singer.id)
        # A: count=1, B: count=2
        s.characters[0].add_timestamp(1000)
        s.characters[1].add_timestamp(2000)
        project.add_sentence(s)

        all_ts = project.get_all_timestamps()
        assert len(all_ts) == 2
        # (sentence_id, s_idx, c_idx, cp_idx, ts)
        assert all_ts[0][4] == 1000
        assert all_ts[1][4] == 2000

    def test_collect_global_timestamps_with_handles(self):
        """collect_all_global_timestamp_ms_with_chars 返回带模型句柄的 7 元组，
        供波形时间标签拖拽编辑命中后定位到具体 checkpoint。"""
        project = Project()
        singer = project.get_default_singer()
        s = Sentence.from_text("ab", singer.id)
        # a: 两个 checkpoint
        s.characters[0].check_count = 2
        s.characters[0].timestamps = [1000, 1500]
        s.characters[0]._update_offset_timestamps()
        # b: 一个 checkpoint + 停顿点
        s.characters[1].check_count = 1
        s.characters[1].timestamps = [2000]
        s.characters[1].is_sentence_end = True
        s.characters[1].set_sentence_end_ts(2500)
        project.add_sentence(s)
        # 项目级统一偏移 +100
        for ch in s.characters:
            ch.set_offset(100)

        out = project.collect_all_global_timestamp_ms_with_chars()
        assert len(out) == 4
        # 每行均为 (ts, char, line_idx, char_idx, cp_idx, is_sentence_end, ruby_text)
        assert all(len(row) == 7 for row in out)
        # 偏移已施加在显示时间戳上
        assert out[0][0] == 1100 and out[1][0] == 1600
        # 句柄正确
        assert out[0][2:6] == (0, 0, 0, False)
        assert out[1][2:6] == (0, 0, 1, False)
        assert out[2][2:6] == (0, 1, 0, False)
        # 停顿点：cp_idx == check_count，is_sentence_end True
        assert out[3][2:6] == (0, 1, 1, True) and out[3][0] == 2600
        # 同一字符仅首个 checkpoint 在前端去重逻辑里携带标签（此处验证字符文本一致）
        assert out[0][1] == "a" and out[1][1] == "a"

    def test_get_timing_statistics(self):
        project = Project()
        singer = project.get_default_singer()
        s = Sentence.from_text("AB", singer.id)
        # A(1) + B(2) = 3 total checkpoints
        s.characters[0].add_timestamp(1000)
        project.add_sentence(s)

        stats = project.get_timing_statistics()
        assert stats["total_lines"] == 1
        assert stats["total_chars"] == 2
        assert stats["total_timetags"] == 1
        assert stats["total_checkpoints"] == 3
        assert stats["timing_progress"] == "1/3"

    def test_validate(self):
        project = Project()
        assert project.is_valid()

        # Invalid singer_id
        s = Sentence.from_text("test", "nonexistent")
        # Bypass add_sentence validation
        project.sentences.append(s)
        assert not project.is_valid()
        errors = project.validate()
        assert any("singer_id" in e for e in errors)

    def test_compat_aliases(self):
        project = Project()
        singer = project.get_default_singer()
        s = Sentence.from_text("test", singer.id)
        project.add_line(s)
        assert len(project.lines) == 1
        assert project.get_line(s.id) == s

        project.remove_line(s.id)
        assert len(project.lines) == 0

    def test_insert_blank_line_creates_space_char(self):
        project = Project()
        singer = project.get_default_singer()
        project.add_sentence(Sentence.from_text("测试", singer.id))

        new_idx = project.insert_blank_line(0, singer_id="singer_1")

        assert new_idx == 1
        new_sentence = project.sentences[1]
        assert new_sentence.singer_id == "singer_1"
        assert len(new_sentence.characters) == 1

        char = new_sentence.characters[0]
        assert char.char == " "
        assert char.singer_id == "singer_1"
        assert char.ruby is None
        assert char.check_count == 0
        assert char.is_line_end is True
        assert char.is_sentence_end is False
        assert char.timestamps == []

    def test_merge_line_inserts_space(self):
        project = Project()
        project.add_singer(Singer(id="s1", name="Singer 1"))
        project.add_singer(Singer(id="s2", name="Singer 2"))
        prev_sentence = Sentence(
            singer_id="s1",
            characters=[
                Character(
                    char="a",
                    singer_id="s1",
                    is_line_end=True,
                )
            ],
        )
        current_sentence = Sentence(
            singer_id="s2",
            characters=[Character(char="b", singer_id="s2", is_line_end=True)],
        )
        project.add_sentence(prev_sentence)
        project.add_sentence(current_sentence)

        result = project.merge_line_into_previous(1)

        assert result is True
        assert len(project.sentences) == 1
        chars = project.sentences[0].characters
        assert [char.char for char in chars] == ["a", " ", "b"]
        assert chars[0].is_line_end is False
        assert chars[1].singer_id == "s1"
        assert chars[1].check_count == 0
        assert chars[1].ruby is None
        assert chars[1].timestamps == []
        assert chars[1].is_line_end is False
        assert chars[1].is_sentence_end is False


class TestShiftSelectedCheckpointTailCp:
    """shift_selected_checkpoint_if_lost 承认句尾停顿点 cp（C4）。"""

    def _project_with_sentence_end_char(self):
        project = Project()
        singer = project.get_default_singer()
        sentence = Sentence(
            singer_id=singer.id,
            characters=[
                Character(char="空", check_count=1, singer_id=singer.id,
                          is_sentence_end=True),
            ],
        )
        project.add_sentence(sentence)
        return project, sentence

    def test_tail_cp_is_still_valid(self):
        """选中句尾停顿点 cp（cp_idx == check_count）不应被顺延"""
        project, sentence = self._project_with_sentence_end_char()
        project.set_selected_checkpoint(0, 0, 1)  # tail cp = check_count

        assert project.shift_selected_checkpoint_if_lost() is False
        assert project.get_selected_checkpoint() == (0, 0, 1)

    def test_tail_cp_valid_with_zero_check_count(self):
        """check_count=0 的停顿点字符：cp 0 即停顿点 cp，仍有效"""
        project, sentence = self._project_with_sentence_end_char()
        sentence.characters[0].check_count = 0
        project.set_selected_checkpoint(0, 0, 0)

        assert project.shift_selected_checkpoint_if_lost() is False
        assert project.get_selected_checkpoint() == (0, 0, 0)

    def test_stale_cp_beyond_tail_still_shifts(self):
        """真正的越界 cp（非停顿点字符的 cp_idx == check_count）仍需顺延"""
        project = Project()
        singer = project.get_default_singer()
        sentence = Sentence(
            singer_id=singer.id,
            characters=[Character(char="あ", check_count=1, singer_id=singer.id)],
        )
        project.add_sentence(sentence)
        project.set_selected_checkpoint(0, 0, 1)  # check_count=1，cp1 无效

        assert project.shift_selected_checkpoint_if_lost() is True
        assert project.get_selected_checkpoint() == (0, 0, 0)


class TestRemoveSingerGuards:
    """remove_singer 的默认演唱者保护与自转移拒绝（C8）。"""

    def test_reject_self_transfer(self):
        project = Project()
        singer = Singer(name="和声")
        project.add_singer(singer)

        with pytest.raises(ValidationError, match="自身"):
            project.remove_singer(singer.id, transfer_to=singer.id)

    def test_reject_removing_last_default(self):
        """删除默认演唱者后项目将失去默认：拒绝"""
        project = Project()
        default = project.get_default_singer()
        project.add_singer(Singer(name="和声"))  # 非默认

        with pytest.raises(ValidationError, match="默认演唱者"):
            project.remove_singer(default.id)

    def test_allow_removing_default_after_reassign(self):
        """先把其他演唱者设为默认再删除：允许（导入预设流程的先设后删）"""
        project = Project()
        default = project.get_default_singer()
        singer = Singer(name="和声")
        project.add_singer(singer)
        # 模拟 singer_interface 导入预设流程：先设新默认，再删旧默认占位符
        for s in project.singers:
            s.is_default = s.id == singer.id

        project.remove_singer(default.id, transfer_to=singer.id)

        assert project.get_singer(default.id) is None
        assert project.get_default_singer().id == singer.id


class TestSentenceOrderApis:
    """add_sentence 参考句校验与 move_sentence 槽位语义（C9）。"""

    def test_add_sentence_missing_reference_raises(self):
        project = Project()
        singer = project.get_default_singer()
        s = Sentence.from_text("测试", singer.id)

        with pytest.raises(DomainError, match="参考句子"):
            project.add_sentence(s, after_sentence_id="nonexistent")
        # 抛错时不产生静默追加
        assert len(project.sentences) == 0

    def test_add_sentence_valid_reference_inserts_after(self):
        project = Project()
        singer = project.get_default_singer()
        s1 = Sentence.from_text("1", singer.id)
        s2 = Sentence.from_text("2", singer.id)
        project.add_sentence(s1)
        project.add_sentence(s2, after_sentence_id=s1.id)

        assert [s.text for s in project.sentences] == ["1", "2"]

    def test_move_sentence_backward_takes_target_slot(self):
        project = Project()
        singer = project.get_default_singer()
        for text in ("1", "2", "3"):
            project.add_sentence(Sentence.from_text(text, singer.id))

        project.move_sentence(project.sentences[2].id, 0)

        assert [s.text for s in project.sentences] == ["3", "1", "2"]

    def test_move_sentence_forward_takes_target_slot(self):
        """向前移动落到目标槽位（被目标位原句让位），不再偏移一位"""
        project = Project()
        singer = project.get_default_singer()
        for text in ("1", "2", "3", "4"):
            project.add_sentence(Sentence.from_text(text, singer.id))

        project.move_sentence(project.sentences[0].id, 2)

        assert [s.text for s in project.sentences] == ["2", "1", "3", "4"]

    def test_move_sentence_to_same_position_is_noop(self):
        project = Project()
        singer = project.get_default_singer()
        s1 = Sentence.from_text("1", singer.id)
        s2 = Sentence.from_text("2", singer.id)
        project.add_sentence(s1)
        project.add_sentence(s2)

        project.move_sentence(s1.id, 0)

        assert [s.text for s in project.sentences] == ["1", "2"]
