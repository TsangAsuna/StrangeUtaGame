"""打轴主界面按键守卫回归测试（A2/A3/A4/A7/A9/A10/A12）。

- A3：tag 分支的 isAutoRepeat 过滤必须先于 not playing 分支；
  旧扁平快捷键 schema 只回填 timing_mode。
- A4：注音分析期间（_ruby_analyzing/_ruby_subset_analyzing）打轴键与
  结构编辑被 InfoBar 提示并拒绝。
- A7：暂停/停止/播放结束/未播放 release 时清理 _pressed_keys 悬挂键。
- A2：暂停态点击「打轴」按钮先走 _on_play 同步 UI。
- A9：截断超长时间戳经 SentenceSnapshotCommand 入撤销栈。
- A10：补全时间戳行尾均分钳制不早于前方锚点。
- A12：queue_delay_ms 钳制到 500ms 并节流提示。
"""

from types import MethodType, SimpleNamespace

from PyQt6.QtCore import Qt

import strange_uta_game.frontend.editor.timing_interface as ti
from strange_uta_game.backend.application.command_manager import CommandManager
from strange_uta_game.backend.domain import Character, Sentence
from strange_uta_game.frontend.editor.timing_interface import EditorInterface


class _StubInfoBar:
    """InfoBar 替身：记录调用，避免测试中创建真实浮窗。"""

    calls: list = []

    @classmethod
    def warning(cls, **kwargs):
        cls.calls.append(("warning", kwargs))

    @classmethod
    def reset(cls):
        cls.calls.clear()


class _FakeKeyEvent:
    def __init__(self, *, key=Qt.Key.Key_Space, auto_repeat=False):
        self._key = key
        self._auto_repeat = auto_repeat
        self.accepted = False
        self.ignored = False

    def key(self):
        return self._key

    def modifiers(self):
        return Qt.KeyboardModifier.NoModifier

    def nativeVirtualKey(self):
        return 0

    def nativeScanCode(self):
        return 0

    def isAutoRepeat(self):
        return self._auto_repeat

    def accept(self):
        self.accepted = True

    def ignore(self):
        self.ignored = True


class _FakeProject:
    """Project 替身：SentenceSnapshotCommand 只需要 sentences + _update_timestamp。"""

    def __init__(self, sentences):
        self.sentences = sentences
        self.update_timestamp_count = 0

    def _update_timestamp(self):
        self.update_timestamp_count += 1


def _bind(editor, *names):
    """把 EditorInterface 的真实方法绑到替身实例上。"""
    for name in names:
        setattr(editor, name, MethodType(getattr(EditorInterface, name), editor))


def _service_double(playing=True, calls=None):
    calls = calls if calls is not None else []
    return SimpleNamespace(
        is_playing=lambda: playing,
        on_timing_key_pressed=lambda key, delay=0: calls.append(("pressed", key, delay)),
        on_timing_key_released=lambda key, delay=0: calls.append(("released", key, delay)),
        on_tag_and_delete_next_pressed=lambda key, delay=0: calls.append(
            ("delete_pressed", key, delay)
        ),
        on_tag_and_delete_next_released=lambda key, delay=0: calls.append(
            ("delete_released", key, delay)
        ),
        is_current_cp_sentence_end_tail=lambda: False,
    )


def _key_editor_double(*, playing=True, pressed_keys=None, analyzing=False):
    """_keyPressEvent_impl / keyReleaseEvent 的最小替身。"""
    calls = []
    editor = SimpleNamespace(
        _timing_service=_service_double(playing=playing, calls=calls),
        _keysound_player=None,
        _key_map_short={"SPACE": "tag_now", "D": "tag_and_delete_next"},
        _key_map_long={},
        _settings_loaded=True,
        _pressed_keys=pressed_keys if pressed_keys is not None else set(),
        _qt_key_to_name=lambda key, modifiers=0, nvk=0, nsc=0: {
            Qt.Key.Key_Space: "SPACE",
            Qt.Key.Key_D: "D",
        }.get(key, ""),
        _add_checkpoint=lambda: calls.append(("checkpoint",)),
        _execute_action=lambda action, key: calls.append(("action", action)),
        _show_runtime_error=lambda msg: calls.append(("error", msg)),
        tr=lambda text: text,
        _service_calls=calls,
    )
    if analyzing:
        editor._ruby_analyzing = True
    _bind(
        editor,
        "_reject_during_ruby_analysis",
        "_ruby_analysis_in_progress",
        "_clamp_queue_delay",
    )
    return editor


# ── A3：auto-repeat 过滤先于 not playing 分支 ──────────────────────────


def test_auto_repeat_tag_press_while_paused_does_not_add_checkpoint():
    editor = _key_editor_double(playing=False)
    event = _FakeKeyEvent(auto_repeat=True)

    EditorInterface._keyPressEvent_impl(editor, event)

    assert event.ignored is True
    # 旧实现：过滤在 not playing 之后 → 长按一次连发几十次 _add_checkpoint
    assert ("checkpoint",) not in editor._service_calls
    assert editor._service_calls == []


def test_plain_tag_press_while_paused_still_adds_checkpoint():
    editor = _key_editor_double(playing=False)
    event = _FakeKeyEvent(auto_repeat=False)

    EditorInterface._keyPressEvent_impl(editor, event)

    assert event.accepted is True
    assert ("checkpoint",) in editor._service_calls


def test_auto_repeat_tag_and_delete_next_press_while_paused_rejected():
    editor = _key_editor_double(playing=False)
    event = _FakeKeyEvent(key=Qt.Key.Key_D, auto_repeat=True)

    EditorInterface._keyPressEvent_impl(editor, event)

    assert event.ignored is True
    assert ("checkpoint",) not in editor._service_calls


def test_auto_repeat_tag_release_while_playing_is_ignored():
    # Qt 自动重复的中间 release 不是真实抬键：不能摘键、不能触发 service
    editor = _key_editor_double(playing=True, pressed_keys={"SPACE"})
    event = _FakeKeyEvent(auto_repeat=True)

    EditorInterface.keyReleaseEvent(editor, event)

    assert event.ignored is True
    assert editor._pressed_keys == {"SPACE"}
    assert editor._service_calls == []


def test_auto_repeat_tag_release_while_paused_discards_key():
    editor = _key_editor_double(playing=False, pressed_keys=set())
    event = _FakeKeyEvent(auto_repeat=True)

    EditorInterface.keyReleaseEvent(editor, event)

    assert event.accepted is True
    assert editor._service_calls == []


# ── A4：注音分析期间拒绝打轴/结构编辑 ──────────────────────────────────


def test_tag_press_rejected_during_ruby_analysis(monkeypatch):
    monkeypatch.setattr(ti, "InfoBar", _StubInfoBar)
    _StubInfoBar.reset()
    editor = _key_editor_double(playing=True, analyzing=True)
    event = _FakeKeyEvent(auto_repeat=False)

    EditorInterface._keyPressEvent_impl(editor, event)

    assert event.accepted is True
    assert editor._service_calls == []  # 打轴调用被拒绝
    assert _StubInfoBar.calls and _StubInfoBar.calls[0][0] == "warning"


def test_tag_and_delete_next_press_rejected_during_ruby_analysis(monkeypatch):
    monkeypatch.setattr(ti, "InfoBar", _StubInfoBar)
    _StubInfoBar.reset()
    editor = _key_editor_double(playing=True, analyzing=True)
    event = _FakeKeyEvent(key=Qt.Key.Key_D, auto_repeat=False)

    EditorInterface._keyPressEvent_impl(editor, event)

    assert editor._service_calls == []
    assert _StubInfoBar.calls


def test_structural_edit_rejected_during_ruby_analysis(monkeypatch):
    monkeypatch.setattr(ti, "InfoBar", _StubInfoBar)
    _StubInfoBar.reset()
    editor = SimpleNamespace(
        _project=object(),
        tr=lambda text: text,
    )
    editor._ruby_subset_analyzing = True
    _bind(editor, "_reject_during_ruby_analysis", "_ruby_analysis_in_progress")

    mutator_called = []

    ok = EditorInterface._execute_structural_edit(
        editor, "测试编辑", lambda: mutator_called.append(1) or (0, 0, None, "lyrics")
    )

    assert ok is False
    assert mutator_called == []  # mutator 未被调用
    assert _StubInfoBar.calls


def test_structural_edit_allowed_when_not_analyzing():
    editor = SimpleNamespace(
        _project=_FakeProject([Sentence(singer_id="s1")]),
        _timing_service=None,
        _current_line_idx=0,
        preview=SimpleNamespace(_current_char_idx=0),
        tr=lambda text: text,
        _sync_after_structure_change=lambda **kwargs: None,
    )
    _bind(editor, "_reject_during_ruby_analysis", "_ruby_analysis_in_progress")

    ok = EditorInterface._execute_structural_edit(
        editor, "测试编辑", lambda: (0, 0, None, "lyrics")
    )

    assert ok is True


# ── A7：悬挂键清理 ─────────────────────────────────────────────────────


def _transport_double(calls):
    return SimpleNamespace(
        set_playing=lambda v: calls.append(("transport_playing", v)),
        set_position=lambda ms: calls.append(("transport_pos", ms)),
        set_duration=lambda ms: calls.append(("transport_dur", ms)),
    )


def test_on_pause_clears_pressed_keys():
    calls = []
    editor = SimpleNamespace(
        _timing_service=SimpleNamespace(pause=lambda: calls.append(("pause",))),
        transport=_transport_double(calls),
        preview=SimpleNamespace(set_playing=lambda v: None),
        timeline=SimpleNamespace(set_playing=lambda v: None),
        lbl_status=SimpleNamespace(setText=lambda text: None),
        tr=lambda text: text,
        _update_mode_indicator=lambda playing=None: None,
        _auto_scroll_suspended=False,
        _auto_scroll_new_line_reached=False,
        _auto_scroll_cooldown_timer=SimpleNamespace(stop=lambda: None),
        _position_poll_timer=SimpleNamespace(stop=lambda: None),
        _validate_all_timestamps=lambda: None,
        _pressed_keys={"SPACE", "D"},
    )

    EditorInterface._on_pause(editor)

    assert editor._pressed_keys == set()


def test_on_stop_clears_pressed_keys():
    calls = []
    editor = SimpleNamespace(
        _timing_service=SimpleNamespace(stop=lambda: None),
        transport=_transport_double(calls),
        preview=SimpleNamespace(set_playing=lambda v: None),
        timeline=SimpleNamespace(
            set_playing=lambda v: None, set_position=lambda ms: None
        ),
        lbl_status=SimpleNamespace(setText=lambda text: None),
        tr=lambda text: text,
        _update_mode_indicator=lambda playing=None: None,
        _auto_scroll_suspended=False,
        _auto_scroll_new_line_reached=False,
        _auto_scroll_cooldown_timer=SimpleNamespace(stop=lambda: None),
        _position_poll_timer=SimpleNamespace(stop=lambda: None),
        _validate_all_timestamps=lambda: None,
        _pressed_keys={"SPACE"},
    )

    EditorInterface._on_stop(editor)

    assert editor._pressed_keys == set()


def test_poll_playback_end_clears_pressed_keys(monkeypatch):
    monkeypatch.setattr(ti, "perf_enabled", lambda: False)
    calls = []
    editor = SimpleNamespace(
        _timing_service=SimpleNamespace(
            get_position_ms=lambda: 100,
            get_duration_ms=lambda: 200,
            _audio_engine=SimpleNamespace(is_playing=lambda: False),
        ),
        y=lambda: 0,
        _position_poll_hidden=lambda: False,
        _playback_range_end_ms=None,
        _last_polled_duration_ms=-1,
        transport=_transport_double(calls),
        preview=SimpleNamespace(
            set_position=lambda ms: None,
            set_duration=lambda ms: None,
            set_current_time_ms=lambda ms: None,
            set_playing=lambda v: None,
        ),
        timeline=SimpleNamespace(
            set_position=lambda ms: None,
            set_duration=lambda ms: None,
            set_playing=lambda v: None,
        ),
        lbl_status=SimpleNamespace(setText=lambda text: None),
        tr=lambda text: text,
        _update_mode_indicator=lambda playing=None: None,
        _auto_scroll_suspended=False,
        _auto_scroll_new_line_reached=False,
        _auto_scroll_cooldown_timer=SimpleNamespace(stop=lambda: None),
        _position_poll_timer=SimpleNamespace(stop=lambda: None),
        _validate_all_timestamps=lambda: None,
        _pressed_keys={"SPACE"},
    )

    EditorInterface._poll_audio_position(editor)

    assert editor._pressed_keys == set()


def test_release_while_paused_discards_dangling_key():
    # 按住打轴键期间停止播放 → release 到达时已不在播放态。
    # 旧实现直接 return，键永久残留并吞掉下次播放同键的 press。
    editor = _key_editor_double(playing=False, pressed_keys={"SPACE"})
    event = _FakeKeyEvent(auto_repeat=False)

    EditorInterface.keyReleaseEvent(editor, event)

    assert "SPACE" not in editor._pressed_keys
    assert editor._service_calls == []


def test_release_while_playing_still_triggers_service():
    editor = _key_editor_double(playing=True, pressed_keys={"SPACE"})
    event = _FakeKeyEvent(auto_repeat=False)

    EditorInterface.keyReleaseEvent(editor, event)

    assert "SPACE" not in editor._pressed_keys
    assert ("released", "SPACE", 0) in editor._service_calls


# ── A2：底部「打轴」按钮暂停态先走 _on_play ───────────────────────────


def _tag_button_editor_double(*, playing, calls):
    editor = SimpleNamespace(
        _timing_service=_service_double(playing=playing, calls=calls),
        _show_runtime_error=lambda msg: calls.append(("error", msg)),
        tr=lambda text: text,
    )
    _bind(
        editor,
        "_reject_during_ruby_analysis",
        "_ruby_analysis_in_progress",
        "_clamp_queue_delay",
    )
    return editor


def test_tag_button_while_paused_plays_through_on_play_first():
    calls = []
    editor = _tag_button_editor_double(playing=False, calls=calls)
    editor._on_play = lambda: calls.append(("play",))

    EditorInterface._on_tag_now(editor)

    # 旧实现：service 自动开播绕过 _on_play，UI 失同步
    assert calls == [("play",), ("pressed", "SPACE", 0), ("released", "SPACE", 0)]


def test_tag_button_while_playing_does_not_replay():
    calls = []
    editor = _tag_button_editor_double(playing=True, calls=calls)
    editor._on_play = lambda: calls.append(("play",))

    EditorInterface._on_tag_now(editor)

    assert ("play",) not in calls
    assert ("pressed", "SPACE", 0) in calls
    assert ("released", "SPACE", 0) in calls


def test_tag_button_rejected_during_ruby_analysis(monkeypatch):
    monkeypatch.setattr(ti, "InfoBar", _StubInfoBar)
    _StubInfoBar.reset()
    calls = []
    editor = _tag_button_editor_double(playing=True, calls=calls)
    editor._ruby_analyzing = True
    editor._on_play = lambda: calls.append(("play",))

    EditorInterface._on_tag_now(editor)

    assert calls == []
    assert _StubInfoBar.calls


# ── A9：截断时间戳入撤销栈 ─────────────────────────────────────────────


def _truncation_editor_double(project, command_manager, calls):
    editor = SimpleNamespace(
        _project=project,
        _timing_service=SimpleNamespace(command_manager=command_manager),
        _current_line_idx=0,
        preview=SimpleNamespace(_current_char_idx=0),
        tr=lambda text: text,
        refresh_lyric_display=lambda: calls.append(("refresh",)),
        _update_time_tags_display=lambda: calls.append(("tags",)),
    )
    return editor


def _sentence_with_extra_timestamps():
    ch = Character(char="あ", check_count=1, timestamps=[10, 20, 30])
    return Sentence(singer_id="s1", characters=[ch])


def test_validate_line_timestamps_truncation_is_undoable():
    sentence = _sentence_with_extra_timestamps()
    project = _FakeProject([sentence])
    manager = CommandManager()
    calls = []
    editor = _truncation_editor_double(project, manager, calls)

    EditorInterface._validate_line_timestamps(editor, 0)

    # 截断发生：入撤销栈且可撤销回完整 timestamps
    assert manager.get_undo_stack_size() == 1
    assert editor._project.sentences[0].characters[0].timestamps == [10]
    assert manager.undo() == editor.tr("校验时间戳（截断超长 timestamps）")
    assert editor._project.sentences[0].characters[0].timestamps == [10, 20, 30]


def test_validate_line_timestamps_no_truncation_no_command():
    ch = Character(char="あ", check_count=1, timestamps=[10])
    sentence = Sentence(singer_id="s1", characters=[ch])
    project = _FakeProject([sentence])
    manager = CommandManager()
    editor = _truncation_editor_double(project, manager, [])

    EditorInterface._validate_line_timestamps(editor, 0)

    assert manager.get_undo_stack_size() == 0
    assert project.update_timestamp_count == 0


# ── A10：行尾均分钳制 ──────────────────────────────────────────────────


def _completion_editor_double(sentence):
    editor = SimpleNamespace(
        _project=_FakeProject([sentence]),
        _timing_service=None,
        _current_line_idx=0,
        preview=SimpleNamespace(_current_char_idx=0),
        _sync_after_structure_change=lambda **kwargs: None,
    )
    _bind(editor, "_execute_structural_edit", "_reject_during_ruby_analysis",
          "_ruby_analysis_in_progress")
    return editor


def _sentence_tail_segment():
    # 首字已打轴（锚点 1000），行尾 3 个平假名未打轴
    anchor = Character(char="あ", check_count=1, timestamps=[1000])
    tail = [Character(char=c, check_count=0) for c in "いうえ"]
    return Sentence(singer_id="s1", characters=[anchor, *tail])


def test_complete_timestamp_tail_avg_clamped_to_prev_anchor():
    sentence = _sentence_tail_segment()
    editor = _completion_editor_double(sentence)

    count = EditorInterface._execute_complete_timestamp(
        editor, {"hiragana"}, [], tail_offset_ms=-500  # 脏数据：tail 补偿为负
    )

    assert count == 3
    tail_ts = [sentence.characters[i].timestamps[0] for i in (1, 2, 3)]
    # 旧实现：end_ts = 1000 + (-500) = 500 < prev_ts → 时间戳递减（875/750/625）
    assert tail_ts == [1000, 1000, 1000]
    assert tail_ts == sorted(tail_ts)


# ── A12：queue_delay_ms 钳制 ───────────────────────────────────────────


def _clamp_editor_double():
    editor = SimpleNamespace(tr=lambda text: text)
    _bind(editor, "_clamp_queue_delay")
    return editor


def test_queue_delay_under_limit_passthrough(monkeypatch):
    monkeypatch.setattr(ti, "InfoBar", _StubInfoBar)
    _StubInfoBar.reset()
    editor = _clamp_editor_double()

    assert editor._clamp_queue_delay(120) == 120
    assert _StubInfoBar.calls == []


def test_queue_delay_over_limit_clamped_and_warned_once(monkeypatch):
    monkeypatch.setattr(ti, "InfoBar", _StubInfoBar)
    _StubInfoBar.reset()
    editor = _clamp_editor_double()

    assert editor._clamp_queue_delay(800) == 500
    assert len(_StubInfoBar.calls) == 1
    # 节流窗口内的后续超阈值不再重复弹窗
    assert editor._clamp_queue_delay(900) == 500
    assert len(_StubInfoBar.calls) == 1
