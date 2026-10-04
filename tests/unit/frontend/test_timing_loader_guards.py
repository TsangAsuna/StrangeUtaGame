"""FileLoader 加载守卫回归测试（bug 审查 A5/A6/A13）。

- A5：视频提取线程防重入 + 完成/失败回调身份校验
- A6：load_lyrics 在途守卫 + 迟到歌词结果按提交时项目身份丢弃
- A13：视频临时音轨在换音频/载入完成时 best-effort 清理

使用与 test_recent_projects.py 相同的假 Editor 范式，不实例化 Qt 控件。
"""

from __future__ import annotations

import strange_uta_game.frontend.editor.timing.file_loader as file_loader
from strange_uta_game.frontend.editor.timing.file_loader import FileLoader


class _Store:
    def __init__(self):
        self.working_dir = ""
        self.dirty = False

    def set_working_dir(self, path):
        self.working_dir = path

    def set_original_media_path(self, path):
        pass

    def set_audio_path(self, path):
        pass


class _Editor:
    def __init__(self):
        self._project = None
        self._store = _Store()
        self._timing_service = None
        self._audio_file_path = None
        self.keysound_reloads = 0

    @staticmethod
    def tr(text):
        return text

    def window(self):
        return object()  # 无 _refresh_frameless → 刷新通知安全跳过

    def _reload_keysound_after_audio(self):
        self.keysound_reloads += 1


class _InfoBarSpy:
    """拦截 InfoBar 弹窗（假 editor 不是 QWidget，弹窗必崩）。"""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self.calls.append(name)

        return _record


class _Timeline:
    def set_audio_name(self, name):
        self.name = name


def _make_loader(monkeypatch):
    editor = _Editor()
    loader = FileLoader(editor)
    spy = _InfoBarSpy()
    monkeypatch.setattr(file_loader, "InfoBar", spy)
    return loader, editor, spy


# ── A5：视频提取线程防重入 ──


def test_video_load_reentry_guard_short_circuits(monkeypatch):
    """提取进行中（_video_thread 非空）的新请求必须直接返回，不再创建线程。"""
    loader, editor, _ = _make_loader(monkeypatch)
    monkeypatch.setattr(file_loader, "is_ffmpeg_available", lambda: True)
    loader._video_thread = object()  # 模拟提取在途

    loader._load_video_as_audio("some.mp4")

    # 未进入 FFmpeg 检查后的任何创建逻辑（状态提示未创建）
    assert loader._state_tooltip is None
    assert editor._audio_file_path is None


def test_stale_video_worker_callback_is_dropped(monkeypatch):
    """完成回调携带过期 worker 身份 → 直接丢弃，不触碰编辑器状态。"""
    loader, editor, _ = _make_loader(monkeypatch)
    loader._video_worker = object()  # 当前身份
    stale = object()

    loader._on_video_loaded("/tmp/new.m4a", "some.mp4", worker=stale)

    assert editor._audio_file_path is None
    assert loader._temp_audio_path is None
    assert editor.keysound_reloads == 0


def test_stale_video_error_callback_is_dropped(monkeypatch):
    loader, editor, spy = _make_loader(monkeypatch)
    loader._video_worker = object()
    stale = object()

    loader._on_video_error("boom", worker=stale)

    assert spy.calls == []  # 未弹错误提示（过期信号静默丢弃）


# ── A6：load_lyrics 在途守卫 + 身份校验 ──


def test_load_lyrics_ignored_while_parse_in_flight(monkeypatch):
    """已有歌词解析在途时 load_lyrics 必须忽略请求，不再替换项目。"""
    loader, editor, _ = _make_loader(monkeypatch)
    loader._lyric_thread = object()  # 模拟解析在途
    prepared = []
    monkeypatch.setattr(
        loader, "_prepare_fresh_project_for_lyrics", lambda **kw: prepared.append(1) or True
    )

    loader.load_lyrics("some.lrc", check_unsaved=False)

    assert prepared == []  # 未走到准备全新项目


def test_load_project_ignored_while_project_load_in_flight():
    """项目解析在途时再次 load_project 直接忽略（不创建状态提示）。"""
    loader = FileLoader(_Editor())
    loader._loading_thread = object()  # 模拟项目解析在途

    loader.load_project("some.sug", check_unsaved=False)

    assert loader._state_tooltip is None


def test_late_lyrics_result_dropped_when_project_changed(monkeypatch):
    """解析期间换了项目 → 迟到的歌词结果按身份校验丢弃，不覆盖新项目。"""
    loader, editor, _ = _make_loader(monkeypatch)
    project_at_submit = object()  # 提交时的项目（假对象即可比对身份）
    editor._project = object()    # 当前项目已切换
    loader._lyric_target_project = project_at_submit
    applied = []
    monkeypatch.setattr(loader, "_apply_lyrics_result", lambda *a: applied.append(a))

    result = {"sentences": ["x"], "is_nicokara": False, "new_singers": [], "parse_meta": {}}
    loader._on_lyrics_parsed(result)

    assert applied == []


def test_lyrics_result_applied_when_project_unchanged(monkeypatch):
    loader, editor, _ = _make_loader(monkeypatch)
    project = object()
    editor._project = project
    loader._lyric_target_project = project
    applied = []
    monkeypatch.setattr(loader, "_apply_lyrics_result", lambda *a: applied.append(a))

    result = {"sentences": ["x"], "is_nicokara": False, "new_singers": [], "parse_meta": {}}
    loader._on_lyrics_parsed(result)

    assert len(applied) == 1


# ── A13：视频临时音轨清理 ──


def test_cleanup_temp_audio_deletes_file(tmp_path):
    loader = FileLoader(_Editor())
    temp = tmp_path / "extracted.m4a"
    temp.write_bytes(b"\x00" * 16)
    loader._temp_audio_path = str(temp)

    loader._cleanup_temp_audio()

    assert not temp.exists()
    assert loader._temp_audio_path is None


def test_cleanup_temp_audio_keeps_path_when_os_locked(tmp_path, monkeypatch):
    """Windows 下引擎占用句柄删除失败 → 保留路径留待下次重试。"""
    loader = FileLoader(_Editor())
    temp = tmp_path / "locked.m4a"
    temp.write_bytes(b"\x00" * 16)
    loader._temp_audio_path = str(temp)

    def _raise(self):
        raise OSError("file in use")

    monkeypatch.setattr(file_loader.Path, "unlink", _raise)

    loader._cleanup_temp_audio()

    assert loader._temp_audio_path == str(temp)  # 路径保留


def test_new_video_load_removes_previous_temp(tmp_path, monkeypatch):
    """加载完成回调会先清理上一条临时音轨，再记录新路径。"""
    loader, editor, _ = _make_loader(monkeypatch)
    old_temp = tmp_path / "old.m4a"
    old_temp.write_bytes(b"\x00" * 16)
    loader._temp_audio_path = str(old_temp)
    loader._video_worker = worker = object()
    editor.timeline = _Timeline()
    new_temp = tmp_path / "new.m4a"
    new_temp.write_bytes(b"\x00" * 16)

    loader._on_video_loaded(str(new_temp), "some.mp4", worker=worker)

    assert not old_temp.exists()
    assert loader._temp_audio_path == str(new_temp)
    assert editor._audio_file_path == str(new_temp)


def test_switching_to_plain_audio_cleans_temp(tmp_path, monkeypatch):
    """拖入普通音频（换音频入口）时旧的视频临时音轨被清理。"""
    loader, editor, _ = _make_loader(monkeypatch)
    monkeypatch.setattr(
        file_loader, "classify_supported_file", lambda p: "audio"
    )
    old_temp = tmp_path / "old.m4a"
    old_temp.write_bytes(b"\x00" * 16)
    loader._temp_audio_path = str(old_temp)
    editor.loaded = []
    editor.load_audio = lambda p: editor.loaded.append(p)

    loader.handle_drop(str(tmp_path / "song.mp3"))

    assert not old_temp.exists()
    assert loader._temp_audio_path is None
    assert editor.loaded == [str(tmp_path / "song.mp3")]
