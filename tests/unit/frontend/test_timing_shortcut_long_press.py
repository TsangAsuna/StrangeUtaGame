from types import SimpleNamespace

from PyQt6.QtCore import Qt

from strange_uta_game.frontend.editor.timing_interface import EditorInterface


class _FakeTimer:
    def __init__(self, active=True):
        self.active = active
        self.start_count = 0
        self.stop_count = 0

    def isActive(self):
        return self.active

    def start(self):
        self.active = True
        self.start_count += 1

    def stop(self):
        self.active = False
        self.stop_count += 1


class _FakeKeyEvent:
    def __init__(self, *, auto_repeat, key=Qt.Key.Key_F5):
        self._auto_repeat = auto_repeat
        self._key = key
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


def _editor_double(timer):
    executed = []
    return SimpleNamespace(
        _timing_service=None,
        _key_map_short={"F5": "short_action", "F6": "short_action_6"},
        _key_map_long={"F5": "long_action", "F6": "long_action_6"},
        _settings_loaded=True,
        # A11：pending 按键名保存（key_upper -> (short, long)）
        _pending_presses={"F5": ("short_action", "long_action")},
        _long_press_key="F5",
        _long_press_timer=timer,
        _qt_key_to_name=lambda key, *args: {
            Qt.Key.Key_F5: "F5",
            Qt.Key.Key_F6: "F6",
        }.get(key, ""),
        _execute_action=lambda action, key: executed.append((action, key)),
        _executed=executed,
    )


def test_long_press_auto_repeat_press_does_not_restart_timer():
    timer = _FakeTimer(active=True)
    editor = _editor_double(timer)
    event = _FakeKeyEvent(auto_repeat=True)

    EditorInterface._keyPressEvent_impl(editor, event)

    assert event.ignored is True
    assert timer.start_count == 0
    assert editor._pending_presses == {"F5": ("short_action", "long_action")}
    assert editor._executed == []


def test_long_press_auto_repeat_release_does_not_trigger_short_action():
    timer = _FakeTimer(active=True)
    editor = _editor_double(timer)
    event = _FakeKeyEvent(auto_repeat=True)

    EditorInterface.keyReleaseEvent(editor, event)

    assert event.ignored is True
    assert timer.stop_count == 0
    assert editor._pending_presses == {"F5": ("short_action", "long_action")}
    assert editor._executed == []


def test_short_only_binding_keeps_auto_repeat_behavior():
    timer = _FakeTimer(active=False)
    editor = _editor_double(timer)
    editor._key_map_long = {}
    event = _FakeKeyEvent(auto_repeat=True)

    EditorInterface._keyPressEvent_impl(editor, event)

    assert event.accepted is True
    assert editor._executed == [("short_action", Qt.Key.Key_F5)]


def test_second_key_press_does_not_overwrite_first_pending():
    """A11：F5 长按 pending 期间按下 F6，F5 的 pending 不得被覆盖。"""
    timer = _FakeTimer(active=False)
    editor = _editor_double(timer)

    EditorInterface._keyPressEvent_impl(editor, _FakeKeyEvent(auto_repeat=False))
    # F5 pending 已登记，定时器运行中
    assert editor._long_press_key == "F5"
    assert timer.start_count == 1

    EditorInterface._keyPressEvent_impl(
        editor, _FakeKeyEvent(auto_repeat=False, key=Qt.Key.Key_F6)
    )

    # 旧实现：单槽 pending 被 F6 覆盖 → F5 的短按动作被吞
    assert editor._pending_presses["F5"] == ("short_action", "long_action")
    assert editor._pending_presses["F6"] == ("short_action_6", "long_action_6")
    # 定时器仍跟踪最早未决键，不被重启
    assert editor._long_press_key == "F5"
    assert timer.start_count == 1


def test_first_key_release_still_executes_its_short_action():
    """A11：多键连按时首键在窗口内释放，短按动作必须执行。"""
    timer = _FakeTimer(active=False)
    editor = _editor_double(timer)
    EditorInterface._keyPressEvent_impl(editor, _FakeKeyEvent(auto_repeat=False))
    EditorInterface._keyPressEvent_impl(
        editor, _FakeKeyEvent(auto_repeat=False, key=Qt.Key.Key_F6)
    )

    EditorInterface.keyReleaseEvent(editor, _FakeKeyEvent(auto_repeat=False))

    assert editor._executed == [("short_action", Qt.Key.Key_F5)]
    assert "F5" not in editor._pending_presses
    # 剩余 pending 键接管判定窗口
    assert editor._long_press_key == "F6"
    assert timer.start_count == 2


def test_second_key_release_executes_its_short_action():
    timer = _FakeTimer(active=False)
    editor = _editor_double(timer)
    EditorInterface._keyPressEvent_impl(editor, _FakeKeyEvent(auto_repeat=False))
    EditorInterface._keyPressEvent_impl(
        editor, _FakeKeyEvent(auto_repeat=False, key=Qt.Key.Key_F6)
    )
    EditorInterface.keyReleaseEvent(editor, _FakeKeyEvent(auto_repeat=False))

    EditorInterface.keyReleaseEvent(
        editor, _FakeKeyEvent(auto_repeat=False, key=Qt.Key.Key_F6)
    )

    assert editor._executed == [
        ("short_action", Qt.Key.Key_F5),
        ("short_action_6", Qt.Key.Key_F6),
    ]
    assert editor._pending_presses == {}


def test_timeout_fires_tracked_long_action_then_advances_to_next_pending():
    timer = _FakeTimer(active=False)
    editor = _editor_double(timer)
    EditorInterface._keyPressEvent_impl(editor, _FakeKeyEvent(auto_repeat=False))
    EditorInterface._keyPressEvent_impl(
        editor, _FakeKeyEvent(auto_repeat=False, key=Qt.Key.Key_F6)
    )

    EditorInterface._on_long_press_timeout(editor)

    assert editor._executed == [("long_action", 0)]
    assert "F5" not in editor._pending_presses
    # 还有 F6 未决：为它重启长按窗口
    assert editor._long_press_key == "F6"
    assert timer.start_count == 2

    EditorInterface._on_long_press_timeout(editor)

    assert editor._executed == [("long_action", 0), ("long_action_6", 0)]
    assert editor._pending_presses == {}
    assert editor._long_press_key is None
