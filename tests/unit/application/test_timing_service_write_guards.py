"""TimingService 打轴写入守卫与可撤销微调测试。

覆盖：
- A1: 打轴时间戳倒退经 on_timing_error(TIMESTAMP_BACKWARD) 告警（写入不阻断）
- C2: 稀疏跳位打轴（前一 cp 无时间戳可回填）被拒绝并上报 TIMESTAMP_GAP
- A8: adjust_current_timestamp 经 SetTimestampCommand 入撤销栈，可撤销/重做
- A14: 打轴并删除下一节奏点使用窄快照命令（TagAndDeleteNextCommand），
  撤销/重做只还原受影响字符，光标属性保持 SentenceSnapshotCommand 契约
"""

from typing import Callable, List, Optional, Tuple

import pytest

from strange_uta_game.backend.application.command_manager import CommandManager
from strange_uta_game.backend.application.commands import (
    SentenceSnapshotCommand,
    TagAndDeleteNextCommand,
)
from strange_uta_game.backend.application.timing_service import (
    CheckpointPosition,
    TimingService,
)
from strange_uta_game.backend.domain import (
    Project,
    Ruby,
    RubyPart,
    Sentence,
    Character,
    Singer,
)
from strange_uta_game.backend.infrastructure.audio.base import (
    AudioInfo,
    IAudioEngine,
    PlaybackState,
)


class FakeAudioEngine(IAudioEngine):
    """轻量音频引擎桩，仅满足 TimingService 接口需求"""

    def __init__(self):
        self._position_ms = 0
        self._playing = False

    def load(self, file_path: str) -> None:
        pass

    def play(self) -> None:
        self._playing = True

    def pause(self) -> None:
        self._playing = False

    def stop(self) -> None:
        self._playing = False
        self._position_ms = 0

    def get_position_ms(self) -> int:
        return self._position_ms

    def set_position_ms(self, position_ms: int) -> None:
        self._position_ms = position_ms

    def get_duration_ms(self) -> int:
        return 60000

    def get_playback_state(self) -> PlaybackState:
        return PlaybackState.PLAYING if self._playing else PlaybackState.STOPPED

    def is_playing(self) -> bool:
        return self._playing

    def set_speed(self, speed: float) -> None:
        pass

    def get_speed(self) -> float:
        return 1.0

    def set_volume(self, volume: float) -> None:
        pass

    def get_volume(self) -> float:
        return 1.0

    def set_position_callback(self, callback: Callable[[int], None]) -> None:
        pass

    def clear_position_callback(self) -> None:
        pass

    def get_audio_info(self) -> Optional[AudioInfo]:
        return None

    def get_original_samples(self):
        return None

    def get_mono_samples(self):
        return None

    def release(self) -> None:
        pass


class ErrorRecorder:
    """TimingCallbacks 回调记录器（on_timing_error 记录，其余 no-op）"""

    def __init__(self):
        self.errors: List[Tuple[str, str]] = []

    def on_timing_error(self, error_type: str, message: str) -> None:
        self.errors.append((error_type, message))

    def on_timetag_added(
        self, singer_id: str, line_idx: int, char_idx: int, checkpoint_idx: int, timestamp_ms: int
    ) -> None:
        pass

    def on_position_changed(self, position_ms: int, duration_ms: int, singer_positions) -> None:
        pass

    def on_singer_changed(self, new_singer_id: str, prev_singer_id: str) -> None:
        pass

    def on_checkpoint_moved(self, position: CheckpointPosition) -> None:
        pass


def _make_project(c1_count: int = 2, c2_count: int = 2, c2_sentence_end: bool = True):
    """构造: '愛' (c1_count) + '空' (c2_count, 可选停顿点)"""
    project = Project()
    singer = Singer(name="default")
    project.add_singer(singer)

    sentence = Sentence(singer_id=singer.id)
    c1 = Character(char="愛", check_count=c1_count, singer_id=singer.id)
    c2 = Character(
        char="空",
        check_count=c2_count,
        singer_id=singer.id,
        is_sentence_end=c2_sentence_end,
    )
    sentence.characters.append(c1)
    sentence.characters.append(c2)
    project.add_sentence(sentence)
    return project, sentence, c1, c2


def _make_service(project, with_manager: bool = True):
    audio = FakeAudioEngine()
    manager = CommandManager() if with_manager else None
    svc = TimingService(audio_engine=audio, command_manager=manager)
    svc.set_project(project)
    # 归零打轴偏移，让时间戳断言直接等于引擎位置
    svc.set_timing_offset(0)
    recorder = ErrorRecorder()
    svc.set_callbacks(recorder)
    return svc, manager, recorder


# ==================== A1: 时间戳倒退告警 ====================


class TestTimestampBackwardWarning:
    def test_backward_write_warns_but_still_writes(self):
        """倒退时间戳：触发 TIMESTAMP_BACKWARD 告警，写入本身不被阻断"""
        project, sentence, c1, c2 = _make_project()
        svc, manager, recorder = _make_service(project)

        svc.on_key_changed(1000, "pressed")  # 写 c1 cp0，推进到 (0,0,1)
        assert c1.timestamps == [1000]

        # 回退光标到 cp1 再写入更早的时间戳
        svc.move_to_checkpoint(0, 0, 1)
        svc.on_key_changed(500, "pressed")

        assert ("TIMESTAMP_BACKWARD", recorder.errors[-1][1]) == recorder.errors[-1]
        assert recorder.errors[-1][0] == "TIMESTAMP_BACKWARD"
        # 告警不阻断写入
        assert c1.timestamps == [1000, 500]

    def test_forward_write_no_warning(self):
        """顺序打轴不产生任何告警"""
        project, sentence, c1, c2 = _make_project()
        svc, manager, recorder = _make_service(project)

        svc.on_key_changed(1000, "pressed")
        svc.on_key_changed(1500, "pressed")

        assert c1.timestamps == [1000, 1500]
        assert recorder.errors == []

    def test_backward_warning_skips_untimed_cps(self):
        """前一 cp 未打轴时继续向前找最近的已写入时间戳做比较"""
        project, sentence, c1, c2 = _make_project(c1_count=3)
        svc, manager, recorder = _make_service(project)

        svc.on_key_changed(1000, "pressed")  # c1 cp0 = 1000，推进到 cp1
        # 直接跳到 cp2（cp1 留空）写入更早时间戳
        svc.move_to_checkpoint(0, 0, 2)
        svc.on_key_changed(300, "pressed")

        assert recorder.errors[-1][0] == "TIMESTAMP_BACKWARD"
        # 空位用前一 cp 时间戳回填，再写入目标位
        assert c1.timestamps == [1000, 1000, 300]


# ==================== C2: 稀疏跳位拒绝 ====================


class TestSparseGapRejected:
    def test_sparse_write_without_previous_rejected(self):
        """跳过前一 cp 直接打轴（无时间戳可回填）：拒绝写入并上报 TIMESTAMP_GAP"""
        project, sentence, c1, c2 = _make_project()
        svc, manager, recorder = _make_service(project)

        # 直接定位到 cp1（cp0 尚未打轴）
        svc.move_to_checkpoint(0, 0, 1)
        svc.on_key_changed(500, "pressed")

        assert recorder.errors[-1][0] == "TIMESTAMP_GAP"
        # 拒绝写入、不推进
        assert c1.timestamps == []
        assert svc.get_current_position().checkpoint_idx == 1

    def test_sparse_write_with_previous_backfills(self):
        """前一 cp 已有时间戳时，中间空位用前一 cp 时间戳回填（非 0ms）"""
        project, sentence, c1, c2 = _make_project(c1_count=3)
        svc, manager, recorder = _make_service(project)

        svc.on_key_changed(1000, "pressed")  # cp0 = 1000
        # 跳过 cp1 直接打 cp2
        svc.move_to_checkpoint(0, 0, 2)
        svc.on_key_changed(2000, "pressed")

        assert c1.timestamps == [1000, 1000, 2000]
        assert 0 not in c1.timestamps

    def test_tail_cp_not_subject_to_sparse_rejection(self):
        """停顿点 cp 走 sentence_end_ts，不受稀疏回填拒绝约束"""
        project, sentence, c1, c2 = _make_project(c2_count=0)
        svc, manager, recorder = _make_service(project)

        # c1 check_count=2 → 先打两个普通 cp
        svc.on_key_changed(1000, "pressed")
        svc.on_key_changed(1500, "pressed")
        # 现在停在 c2 的停顿点 cp（cp_idx == 0 == check_count）
        svc.on_key_changed(2000, "released")

        assert c2.sentence_end_ts == 2000
        assert recorder.errors == []


# ==================== A8: 微调可撤销 ====================


class TestAdjustCurrentTimestampUndoable:
    def test_adjust_normal_cp_records_command(self):
        project, sentence, c1, c2 = _make_project()
        c1.add_timestamp(1000, 0)
        c1.add_timestamp(1500, 1)
        svc, manager, recorder = _make_service(project)

        assert svc.move_to_checkpoint(0, 0, 0)
        assert svc.adjust_current_timestamp(250) is True

        assert c1.timestamps[0] == 1250
        assert manager.can_undo()
        cmd = manager.get_last_redone_command()
        assert cmd.description.startswith("微调时间戳")

        # 撤销 / 重做
        svc.undo()
        assert c1.timestamps[0] == 1000
        svc.redo()
        assert c1.timestamps[0] == 1250

    def test_adjust_sentence_end_ts_undoable(self):
        project, sentence, c1, c2 = _make_project(c2_count=1)
        c1.add_timestamp(1000, 0)
        c2.set_sentence_end_ts(3000)
        svc, manager, recorder = _make_service(project)

        # c2: cp0 普通 cp，cp1 == check_count 为停顿点 cp
        assert svc.move_to_checkpoint(0, 1, 1)
        assert svc.adjust_current_timestamp(-500) is True
        assert c2.sentence_end_ts == 2500

        svc.undo()
        assert c2.sentence_end_ts == 3000
        svc.redo()
        assert c2.sentence_end_ts == 2500

    def test_adjust_clamps_at_zero(self):
        project, sentence, c1, c2 = _make_project()
        c1.add_timestamp(100, 0)
        c1.add_timestamp(1500, 1)
        svc, manager, recorder = _make_service(project)

        assert svc.move_to_checkpoint(0, 0, 0)
        assert svc.adjust_current_timestamp(-5000) is True
        assert c1.timestamps[0] == 0

    def test_zero_delta_noop_no_undo_entry(self):
        """零增量：返回 True 但不产生撤销条目"""
        project, sentence, c1, c2 = _make_project()
        c1.add_timestamp(1000, 0)
        svc, manager, recorder = _make_service(project)

        assert svc.move_to_checkpoint(0, 0, 0)
        assert manager.can_undo() is False
        assert svc.adjust_current_timestamp(0) is True
        assert c1.timestamps[0] == 1000
        assert manager.can_undo() is False

    def test_adjust_without_timestamps_returns_false(self):
        project, sentence, c1, c2 = _make_project()
        svc, manager, recorder = _make_service(project)

        assert svc.move_to_checkpoint(0, 0, 0)
        assert svc.adjust_current_timestamp(100) is False


# ==================== A14: 窄快照「打轴并删除下一节奏点」 ====================


class TestTagAndDeleteNextNarrowSnapshot:
    def test_delete_next_same_char_uses_narrow_command(self):
        """被删 cp 属于同一字符：仅 1 个条目，撤销还原 check_count + 时间戳"""
        project, sentence, c1, c2 = _make_project(c1_count=2)
        svc, manager, recorder = _make_service(project)

        svc._audio_engine.set_position_ms(1000)
        svc.on_tag_and_delete_next_pressed("SPACE", queue_delay_ms=0)

        # c1 check_count 2→1，cp0 已写入
        assert c1.check_count == 1
        assert c1.timestamps == [1000]

        cmd = manager.get_last_redone_command()
        assert isinstance(cmd, TagAndDeleteNextCommand)
        # 窄快照：仅受影响的 c1 一个条目
        assert len(cmd._entries) == 1

        # 撤销：check_count 与时间戳还原，光标回到写入前字符
        svc.undo()
        assert c1.check_count == 2
        assert c1.timestamps == []
        assert cmd.undo_position == (0, 0)

        # 重做：再次删除
        svc.redo()
        assert c1.check_count == 1
        assert c1.timestamps == [1000]

    def test_delete_next_cross_char_restores_both(self):
        """被删 cp 属于下一字符：2 个条目，撤销同时还原两字"""
        project, sentence, c1, c2 = _make_project(c1_count=1, c2_count=2)
        svc, manager, recorder = _make_service(project)

        # 全局 cp: (0,0,0) → (0,1,0) → (0,1,1) → (0,1,2)←停顿点
        svc._audio_engine.set_position_ms(1000)
        svc.on_tag_and_delete_next_pressed("SPACE", queue_delay_ms=0)

        # 写 c1 cp0；删除下一节奏点 (0,1,0) → c2 check_count 2→1
        assert c1.timestamps == [1000]
        assert c2.check_count == 1

        cmd = manager.get_last_redone_command()
        assert isinstance(cmd, TagAndDeleteNextCommand)
        assert len(cmd._entries) == 2

        svc.undo()
        assert c1.timestamps == []
        assert c2.check_count == 2

        svc.redo()
        assert c1.timestamps == [1000]
        assert c2.check_count == 1

    def test_delete_next_tail_cp_restores_sentence_end(self):
        """删除的是停顿点尾部 cp：撤销还原 is_sentence_end 标记"""
        project = Project()
        singer = project.singers[0]
        sentence = Sentence(singer_id=singer.id)
        c1 = Character(char="愛", check_count=1, singer_id=singer.id)
        c2 = Character(
            char="空", check_count=1, singer_id=singer.id, is_sentence_end=True
        )
        c3 = Character(char="光", check_count=1, singer_id=singer.id)
        sentence.characters.extend([c1, c2, c3])
        project.add_sentence(sentence)
        svc, manager, recorder = _make_service(project)

        # 全局 cp: (0,0,0) → (0,1,0) → (0,1,1)←停顿点 → (0,2,0)
        # 定位到 c2 的普通 cp，打轴并删除下一 cp = 停顿点尾部 cp
        assert svc.move_to_checkpoint(0, 1, 0)
        svc._audio_engine.set_position_ms(1000)
        svc.on_tag_and_delete_next_pressed("SPACE", queue_delay_ms=0)

        assert c2.is_sentence_end is False
        assert c2.sentence_end_ts is None

        svc.undo()
        assert c2.is_sentence_end is True
        svc.redo()
        assert c2.is_sentence_end is False

    def test_narrow_command_is_sentence_snapshot_subclass(self):
        """前端按 isinstance(cmd, SentenceSnapshotCommand) 走结构化刷新路径"""
        assert issubclass(TagAndDeleteNextCommand, SentenceSnapshotCommand)

    def test_delete_next_restores_ruby_parts_exactly(self):
        """被删 cp 所在字符带 ruby：撤销后 ruby 分段逐位还原（不走重分段）"""
        project = Project()
        singer = project.singers[0]
        sentence = Sentence(singer_id=singer.id)
        c1 = Character(char="愛", check_count=1, singer_id=singer.id)
        ruby = Ruby(parts=[RubyPart("あ"), RubyPart("ど")])
        c2 = Character(char="空", check_count=2, singer_id=singer.id, ruby=ruby)
        sentence.characters.extend([c1, c2])
        project.add_sentence(sentence)
        svc, manager, recorder = _make_service(project)

        # 定位到 (0,1,0)，打轴并删除 → c2 check_count 2→1，parts 合并尾段
        assert svc.move_to_checkpoint(0, 1, 0)
        svc._audio_engine.set_position_ms(1000)
        svc.on_tag_and_delete_next_pressed("SPACE", queue_delay_ms=0)

        assert c2.check_count == 1
        # parts 收缩为 1 段（合并）
        assert len(c2.ruby.parts) == 1

        svc.undo()
        # 撤销逐位还原原分段（不是按 mora 重新拆分）
        assert [p.text for p in c2.ruby.parts] == ["あ", "ど"]
        svc.redo()
        assert len(c2.ruby.parts) == 1
