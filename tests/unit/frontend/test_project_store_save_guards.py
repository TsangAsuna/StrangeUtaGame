"""ProjectStore 保存守卫与闪退恢复的单元测试。

守护（03-bug审查报告 C1 / C5 / C10 / I11 / I7 相关修复）：
- save() 在同槽位保存在进行中被跳过时必须返回 False（跳过≠成功）
- 保存窗口期（序列化之后）的新编辑（edit_epoch 递增）不得被完成回调
  清掉 dirty —— 否则被误标"已保存"，关闭不再提示，静默丢改动
- load_project / close_project 先停在途保存线程，再清理临时文件
- 闪退恢复候选按 mtime 降序（多项目并存时恢复最近编辑的）
- 更改配置位置遗留的 legacy .temp 目录纳入恢复扫描
"""

from __future__ import annotations

import os
import threading

import pytest
from PyQt6.QtCore import QThread

from strange_uta_game.frontend import project_store as project_store_module
from strange_uta_game.frontend.project_store import ProjectStore


@pytest.fixture(autouse=True)
def _qapp(qapp):
    # pytest-qt 的 qapp：保证是 QApplication（后续文件可能构造 widget）
    yield qapp


# 本文件创建的 store：测试结束后停掉其防抖/自动保存定时器，避免泄漏的
# QTimer 在后续测试的事件循环里触发 _do_periodic_save（_project 是假对象，
# 真实序列化会在 Qt 槽里抛异常，可能直接杀掉测试进程）。
_created_stores: list[ProjectStore] = []


@pytest.fixture(autouse=True)
def _stop_leaked_store_timers():
    yield
    for store in _created_stores:
        try:
            store._periodic_save_timer.stop()
            store._auto_save_timer.stop()
        except RuntimeError:
            pass
    _created_stores.clear()


def _make_store(save_path=None) -> ProjectStore:
    store = ProjectStore()
    store._project = object()  # 不需要真实 Project，仅做真值判断
    if save_path is not None:
        store._save_path = save_path
    _created_stores.append(store)
    return store


class TestSaveSlotGuards:
    def test_save_returns_false_without_project(self):
        assert ProjectStore().save() is False

    def test_save_returns_false_when_slot_busy(self):
        """同槽位已有保存在进行中：跳过必须返回 False（C5）。"""
        store = _make_store()
        busy = QThread()
        busy.start()
        store._save_thread = busy
        try:
            assert store.save() is False
        finally:
            busy.quit()
            busy.wait(3000)

    def test_save_returns_true_and_launches_thread(self, tmp_path, monkeypatch):
        store = _make_store(str(tmp_path / "song.sug"))
        monkeypatch.setattr(
            project_store_module.SugProjectParser, "serialize", lambda *a, **k: {}
        )
        release = threading.Event()
        monkeypatch.setattr(
            project_store_module.ProjectSaveWorker, "run",
            lambda self: release.wait(5),
        )
        try:
            assert store.save() is True
            assert store._save_thread is not None
            assert store._save_thread.isRunning()
        finally:
            release.set()
            store._stop_save_threads()


class TestManualSaveEpoch:
    def test_keeps_dirty_when_edit_during_save_window(self):
        """保存窗口期的新编辑不得被完成回调清掉 dirty（C1）。"""
        store = _make_store()
        store._dirty = True
        epoch_at_save = store._edit_epoch
        store.notify("lyrics")  # 保存窗口期（写盘进行中）的新编辑
        assert store._edit_epoch == epoch_at_save + 1

        store._on_manual_save_finished("song.sug", 0, epoch_at_save)
        assert store._dirty is True

    def test_clears_dirty_when_no_edit_during_window(self):
        store = _make_store()
        store._dirty = True
        store._on_manual_save_finished("song.sug", 0, store._edit_epoch)
        assert store._dirty is False

    def test_mark_dirty_bumps_epoch(self):
        store = _make_store()
        before = store._edit_epoch
        store.mark_dirty()
        assert store._edit_epoch == before + 1
        assert store._dirty is True

    def test_notify_bumps_epoch(self):
        store = _make_store()
        before = store._edit_epoch
        store.notify("timetags")
        assert store._edit_epoch == before + 1

    def test_set_original_media_path_bumps_epoch(self):
        store = _make_store()
        before = store._edit_epoch
        store.set_original_media_path("x.mp3")
        assert store._edit_epoch == before + 1


class TestLoadProjectStopsThreads:
    def test_load_stops_threads_before_cleanup(self, tmp_path, monkeypatch):
        order: list[str] = []
        store = _make_store()
        monkeypatch.setattr(store, "_stop_save_threads", lambda: order.append("stop"))
        monkeypatch.setattr(store, "cleanup_temp_files", lambda: order.append("cleanup"))
        store.load_project(object(), save_path=str(tmp_path / "p.sug"))
        # 必须先停在途保存线程，否则其回调把旧项目写回刚删掉的 .temp（C10）
        assert order == ["stop", "cleanup"]

    def test_close_stops_threads_before_cleanup(self, monkeypatch):
        order: list[str] = []
        store = _make_store()
        monkeypatch.setattr(store, "_stop_save_threads", lambda: order.append("stop"))
        monkeypatch.setattr(store, "cleanup_temp_files", lambda: order.append("cleanup"))
        store.close_project()
        assert order == ["stop", "cleanup"]


class TestWaitForManualSave:
    def test_noop_without_thread(self):
        store = ProjectStore()
        store.wait_for_manual_save(1000)  # 不抛、不卡

    def test_waits_for_running_thread(self):
        store = ProjectStore()
        thread = QThread()
        thread.start()
        store._save_thread = thread
        try:
            store.wait_for_manual_save(5000)
            assert not thread.isRunning()
        finally:
            thread.wait(1000)


class TestCrashRecoveryOrdering:
    def test_picks_newest_candidate(self, tmp_path, monkeypatch):
        old = tmp_path / ".old.sug.temp"
        new = tmp_path / ".new.sug.temp"
        old.write_bytes(b"x")
        new.write_bytes(b"x")
        os.utime(old, (1000000, 1000000))
        os.utime(new, (2000000, 2000000))
        monkeypatch.setattr(
            ProjectStore, "_crash_recovery_dirs", staticmethod(lambda: [tmp_path])
        )
        monkeypatch.setattr(
            project_store_module.SugProjectParser,
            "load_with_extras",
            lambda path: ("proj", {}),
        )
        result = ProjectStore.load_crash_recovery()
        assert result is not None
        # 多项目并存时恢复最近编辑的，而不是 glob 顺序第一个（I11）
        assert result[1] == str(new)

    def test_untitled_still_has_priority(self, tmp_path, monkeypatch):
        untitled = tmp_path / ".untitled.sug.temp"
        untitled.write_bytes(b"x")
        newer = tmp_path / ".newer.sug.temp"
        newer.write_bytes(b"x")
        os.utime(newer, (2000000, 2000000))
        monkeypatch.setattr(
            ProjectStore, "_crash_recovery_dirs", staticmethod(lambda: [tmp_path])
        )
        monkeypatch.setattr(
            project_store_module.SugProjectParser,
            "load_with_extras",
            lambda path: ("proj", {}),
        )
        result = ProjectStore.load_crash_recovery()
        assert result is not None
        assert result[1] == str(untitled)


class TestLegacyTempDirs:
    def test_crash_recovery_dirs_include_legacy(self, tmp_path, monkeypatch):
        from strange_uta_game.frontend.settings.app_settings import AppSettings

        legacy = tmp_path / "legacy" / "ProjectBackup" / ".temp"
        legacy.mkdir(parents=True)
        monkeypatch.setattr(project_store_module, "_temp_dir", lambda: tmp_path / "cur")
        monkeypatch.setattr(project_store_module, "_cache_dir", lambda: tmp_path / "cache")
        monkeypatch.setattr(
            project_store_module.app_dirs, "default_backup_dir",
            lambda: tmp_path / "backup",
        )
        s = AppSettings()
        old = s.get("auto_save.legacy_temp_dirs", [])
        # 一个真实存在、一个不存在：缺失路径应被过滤
        s.set("auto_save.legacy_temp_dirs", [str(legacy), str(tmp_path / "gone")])
        try:
            dirs = ProjectStore._crash_recovery_dirs()
        finally:
            s.set("auto_save.legacy_temp_dirs", old if isinstance(old, list) else [])
        assert legacy in dirs
        assert tmp_path / "gone" not in dirs
        # 常规三处扫描位置仍保留
        assert tmp_path / "cur" in dirs
        assert tmp_path / "backup" / ".temp" in dirs
