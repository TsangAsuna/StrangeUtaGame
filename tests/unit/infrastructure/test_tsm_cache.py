"""TSMRenderCache 单元测试。"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from strange_uta_game.backend.infrastructure.audio.tsm_cache import (
    ChunkInfo,
    SpeedTask,
    TSMRenderCache,
    _get_cache_path,
    _quantize,
    clear_cache_for_song,
)


def _make_pcm(seconds: float = 1.0, sr: int = 22050, channels: int = 2) -> np.ndarray:
    # 注意：seconds 不能太短，WSOLA 有固定的启动/flush 开销，太短的片段
    # 输出长度会显著偏离 input/speed 的理论值（例如 0.2s 实测只有 ~52%）。
    # 1s 起步可使比例 ≥ 0.9，足以稳定断言。
    n = int(seconds * sr)
    t = np.linspace(0, seconds, n, endpoint=False, dtype=np.float32)
    base = np.sin(2 * np.pi * 440 * t).astype(np.float32) * 0.2
    if channels == 1:
        return base.reshape(-1, 1)
    return np.stack([base, base * 0.8], axis=1)


class TestTSMRenderCacheBasic:
    def test_quantize(self):
        assert _quantize(1.0) == 1.0
        assert _quantize(1.234) == 1.23
        assert _quantize(1.235) in (1.23, 1.24)  # 依赖银行家舍入；两种都合法

    def test_empty_before_source(self):
        c = TSMRenderCache()
        assert c.get(1.0) is None
        assert c.get(1.5) is None

    def test_one_x_source_is_available_after_mp3_roundtrip(self):
        c = TSMRenderCache()
        pcm = _make_pcm()
        c.set_source("a.wav", pcm, 22050)
        ret = c.get(1.0)
        assert ret is not None
        assert ret.dtype == np.float32
        assert ret.shape[1] == pcm.shape[1]
        assert abs(ret.shape[0] / c._sample_rate - pcm.shape[0] / 22050) < 0.1

    def test_render_blocking_get_then_cached(self):
        c = TSMRenderCache()
        pcm = _make_pcm()
        c.set_source("a.wav", pcm, 22050)

        done = threading.Event()
        c.ensure(1.5, done_cb=lambda s: done.set())

        # 非阻塞返回
        assert c.get(1.5) is None

        assert done.wait(timeout=15), "渲染应在合理时间内完成"
        rendered = c.get(1.5)
        assert rendered is not None
        assert rendered.dtype == np.float32
        assert rendered.shape[1] == pcm.shape[1]
        # 1.5x 理论输出长度 ≈ 1x 源长度 / 1.5。
        # 基准必须取「实际 1x 源」（解码后的源 MP3）长度，而非重采样前的输入帧数：
        # 22050Hz 输入会被重采样到 MP3 支持档 32000Hz，源长度随之变化。切点表也是
        # 在这份解码源上规划的，故渲染输出覆盖完整源、长度以它为准。
        src_1x = c.get(1.0)
        expected = src_1x.shape[0] / 1.5
        assert abs(rendered.shape[0] - expected) / expected < 0.2

    def test_lru_evicts_oldest(self):
        c = TSMRenderCache()
        c._MAX_MEM_CACHE = 3
        pcm = _make_pcm(seconds=0.1)
        c.set_source("a.wav", pcm, 22050)

        done = threading.Event()
        pending = {"count": 0}
        lock = threading.Lock()

        def done_cb(speed):
            with lock:
                pending["count"] += 1
                if pending["count"] >= 4:
                    done.set()

        # 顺序渲染 4 个速度，每次等完成再发下一个（避免互相取消）
        for s in (0.75, 1.25, 1.5, 1.75):
            ev = threading.Event()
            c.ensure(s, done_cb=lambda _s, ev=ev: ev.set())
            assert ev.wait(timeout=15)

        # LRU 只保留 3，最早的 0.75 被踢
        with c._mem_cache_lock:
            assert 0.75 not in c._memory_cache
            assert len(c._memory_cache) <= c._MAX_MEM_CACHE
        # 内存淘汰不删除磁盘缓存，仍可按需重新载入。
        assert c.get(0.75) is not None
        assert c.get(1.25) is not None
        assert c.get(1.5) is not None
        assert c.get(1.75) is not None

    def test_set_source_clears_cache(self):
        c = TSMRenderCache()
        pcm = _make_pcm()
        c.set_source("a.wav", pcm, 22050)
        ev = threading.Event()
        c.ensure(1.5, done_cb=lambda _s: ev.set())
        assert ev.wait(timeout=15)
        assert c.get(1.5) is not None

        pcm2 = _make_pcm(seconds=0.1)
        c.set_source("b.wav", pcm2, 22050)
        assert c.get(1.5) is None

    def test_duplicate_ensure_merged(self):
        c = TSMRenderCache()
        pcm = _make_pcm()
        c.set_source("a.wav", pcm, 22050)

        done_called = []
        ev = threading.Event()

        def done_cb(speed):
            done_called.append(speed)
            ev.set()

        c.ensure(1.5, done_cb=done_cb)
        # 立刻再发同一速度，不应新开 worker
        c.ensure(1.5, done_cb=done_cb)
        assert ev.wait(timeout=15)
        # 最终一定有至少一次完成
        assert len(done_called) >= 1


# ──────────────────────────────────────────────
# D2：_finalize_task 取消/版本复查（切歌不得把旧歌 PCM 写成新歌缓存）
# ──────────────────────────────────────────────


class TestFinalizeTaskRechecks:
    @pytest.fixture
    def cache(self, tmp_path, monkeypatch):
        """缓存目录重定向到会话内临时目录（cache_dir() 调用时求值）。"""
        monkeypatch.setenv("SUG_CACHE_DIR", str(tmp_path / "cache"))
        c = TSMRenderCache()
        pcm = _make_pcm(seconds=1.0)
        c.set_source("song_a", pcm, 22050)
        return c

    def _make_task(self, c, speed=1.5, cancelled=False, version=None):
        """手工构造一个"全部块已完成"的任务（绕过调度，直接测 finalize）。"""
        src = c.get(1.0)
        task = SpeedTask(
            speed=speed,
            priority=0,
            progress_cb=None,
            done_cb=None,
            version=c._render_version if version is None else version,
        )
        task.chunks = [
            ChunkInfo(index=0, src_start=0, src_end=len(src),
                      core_start=0, core_end=len(src))
        ]
        task.results = {0: src.copy()}
        task.completed.set()
        task.cancelled = cancelled
        return task

    def test_cancelled_task_not_written(self, cache):
        task = self._make_task(cache, cancelled=True)
        fired = []
        task.done_cb = lambda s: fired.append(s)

        cache._finalize_task(task)

        assert not _get_cache_path("song_a", 1.5).exists(), "取消任务不得写盘"
        assert fired == []

    def test_stale_version_task_not_written(self, cache):
        """切歌后 _render_version 已推进：旧版本任务丢弃，不得写成新歌缓存。"""
        task = self._make_task(cache, version=cache._render_version + 1)
        fired = []
        task.done_cb = lambda s: fired.append(s)

        cache._finalize_task(task)

        assert not _get_cache_path("song_a", 1.5).exists()
        assert fired == []

    def test_version_bumped_during_merge_not_written(self, cache, monkeypatch):
        """写盘前二次复查：merge 期间发生切歌（版本推进）→ 丢弃结果。"""
        task = self._make_task(cache)

        def merge_and_bump_version(t):
            # 模拟 merge 期间切歌：set_source 会推进 _render_version
            with cache._lock:
                cache._render_version += 1
            return cache.get(1.0)[:100]

        monkeypatch.setattr(cache, "_merge_chunks", merge_and_bump_version)
        fired = []
        task.done_cb = lambda s: fired.append(s)

        cache._finalize_task(task)

        assert not _get_cache_path("song_a", 1.5).exists(), "merge 后过期任务不得写盘"
        assert fired == []

    def test_valid_task_writes_cache_and_fires_done(self, cache):
        """正例：未取消、版本匹配 → 正常写盘并触发换源回调。"""
        task = self._make_task(cache)
        fired = []
        task.done_cb = lambda s: fired.append(s)

        cache._finalize_task(task)

        assert _get_cache_path("song_a", 1.5).exists(), "有效任务应写入缓存"
        assert fired == [1.5]


# ──────────────────────────────────────────────
# D7：ensure(check_only=True) 只查存在性，绝不在调用线程整曲解码
# ──────────────────────────────────────────────


class TestEnsureCheckOnly:
    @pytest.fixture
    def cache(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SUG_CACHE_DIR", str(tmp_path / "cache"))
        c = TSMRenderCache()
        pcm = _make_pcm(seconds=1.0)
        c.set_source("song_a", pcm, 22050)
        return c

    def test_check_only_dispatches_render_without_decode(self, cache):
        ret = cache.ensure(1.5, check_only=True)
        assert ret is None, "未渲染速度应返回 None"
        with cache._mem_cache_lock:
            assert 1.5 not in cache._memory_cache, "check_only 不得整曲解码进内存"

        # 派发的渲染任务在后台完成
        deadline = time.time() + 20
        while time.time() < deadline and cache.get(1.5) is None:
            time.sleep(0.05)
        assert cache.get(1.5) is not None

    def test_check_only_disk_hit_returns_true_without_decode(self, cache):
        ev = threading.Event()
        cache.ensure(1.5, done_cb=lambda _s: ev.set())
        assert ev.wait(timeout=20)
        # 清掉内存缓存，只留磁盘文件
        with cache._mem_cache_lock:
            cache._memory_cache.clear()

        ret = cache.ensure(1.5, check_only=True)
        assert ret is True, "磁盘缓存存在即视为就绪"
        with cache._mem_cache_lock:
            assert 1.5 not in cache._memory_cache, "就绪检查不得触发整曲解码"

    def test_check_only_one_x(self, cache):
        assert cache.ensure(1.0, check_only=True) is True

    def test_check_only_before_source(self):
        c = TSMRenderCache()
        assert c.ensure(1.5, check_only=True) is None


# ──────────────────────────────────────────────
# D11：clear_cache_for_song 歌名含 glob 元字符
# ──────────────────────────────────────────────


class TestClearCacheForSongGlobMetachars:
    def test_bracket_song_name_cleared(self, tmp_path, monkeypatch):
        """歌名含 [] 时旧实现拼 glob 模式永不匹配 → 缓存永不清理。"""
        monkeypatch.setenv("SUG_CACHE_DIR", str(tmp_path / "cache"))
        from strange_uta_game.backend.infrastructure.audio import tsm_cache as tc

        cache_dir = tc._get_cache_dir()
        names = [
            "a[b]_1.5x.mp3",
            "a[b]_source.mp3",
            "other_1.5x.mp3",  # 对照：其它歌曲不受影响
        ]
        for n in names:
            (cache_dir / n).write_bytes(b"x")

        clear_cache_for_song("a[b]")

        assert not (cache_dir / "a[b]_1.5x.mp3").exists()
        assert not (cache_dir / "a[b]_source.mp3").exists()
        assert (cache_dir / "other_1.5x.mp3").exists(), "不应误删其它歌曲缓存"
