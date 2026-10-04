"""SoundDeviceEngine 位置/竞态修复回归测试（D3 / D4 / D8 / D9 / D14）。

不触碰真实音频设备：`_start_streaming` 被替换为空操作，流用 Fake 对象。
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from strange_uta_game.backend.infrastructure.audio.ring_buffer import RingBuffer
from strange_uta_game.backend.infrastructure.audio.sounddevice_engine import (
    SoundDeviceEngine,
)
from strange_uta_game.backend.infrastructure.audio.base import PlaybackState

_SR = 44100


def _make_engine(seconds: float = 2.0) -> tuple[SoundDeviceEngine, np.ndarray]:
    engine = SoundDeviceEngine()
    # 测试不碰音频设备
    engine._start_streaming = lambda: None
    n = int(_SR * seconds)
    pcm = (np.random.RandomState(7).randn(n, 2) * 0.1).astype(np.float32)
    engine._original_data = pcm
    engine._sample_rate = _SR
    engine._channels = 2
    engine._duration_ms = int(seconds * 1000)
    engine._active_pcm = pcm
    engine._active_speed = 1.0
    engine._speed = 1.0
    engine._pending_speed = 1.0
    engine._ring = RingBuffer(int(0.5 * _SR), 2)
    return engine, pcm


class _FakeStream:
    """记录 stop/close 调用的假 OutputStream。"""

    def __init__(self):
        self.stop_calls = 0
        self.close_calls = 0
        self.active = True

    def stop(self):
        self.stop_calls += 1

    def close(self):
        self.close_calls += 1


class _AlwaysFullRing:
    """read_into 永远返回满帧数的假 ring（用于回调锚点并发压测）。"""

    def read_into(self, out) -> int:
        return int(out.shape[0])


class TestSpeedSwapAnchor:
    def test_swap_subtracts_ring_residual_from_anchor(self):
        """D3：换源时锚点必须扣除 ring 残留帧，位置不超前实际声音。"""
        engine, _pcm = _make_engine()
        new_pcm = np.zeros((100000, 2), np.float32)

        # ring 里预置 4096 帧旧速度残留
        residual = np.ones((4096, 2), np.float32)
        engine._ring.write_from(residual)
        # 旧 PCM 已消费 1000 帧（active_speed=1.0）
        engine._consumed_in_active_pcm = 1000
        engine._last_callback_perf_time = time.perf_counter()
        engine._pending_speed = 0.5
        engine._speed = 0.5
        engine._cache.get = lambda s: new_pcm if abs(s - 0.5) < 1e-9 else None

        engine._maybe_swap_active_speed()

        # 换到 0.5x：原始位置 1000 帧 → 新 PCM 偏移 2000 帧
        assert engine._active_speed == 0.5
        assert engine._read_pos_samples == 2000
        # 锚点 = 2000 - 残留 4096 → 0（下限钳制），而非 2000
        assert engine._consumed_in_active_pcm == 0
        assert engine._pending_speed == engine._speed

    def test_swap_anchor_accounts_partial_residual(self):
        """残留小于新偏移时，锚点 = 新偏移 - 残留。"""
        engine, _pcm = _make_engine()
        new_pcm = np.zeros((100000, 2), np.float32)
        residual = np.ones((100, 2), np.float32)
        engine._ring.write_from(residual)
        engine._consumed_in_active_pcm = 1000
        engine._last_callback_perf_time = time.perf_counter()
        engine._pending_speed = 0.5
        engine._speed = 0.5
        engine._cache.get = lambda s: new_pcm if abs(s - 0.5) < 1e-9 else None

        engine._maybe_swap_active_speed()
        assert engine._consumed_in_active_pcm == 2000 - 100


class TestRelease:
    def test_release_stops_stream_and_producer(self):
        """D4：release() 必须关闭 PortAudio 流并停掉 producer 线程。"""
        engine, _pcm = _make_engine()
        fake = _FakeStream()
        engine._stream = fake
        engine._producer_stop.clear()
        t = threading.Thread(
            target=engine._producer_loop, daemon=True, name="AudioProducer-test"
        )
        engine._producer_thread = t
        t.start()
        assert t.is_alive()

        engine.release()

        assert fake.stop_calls >= 1
        assert fake.close_calls >= 1
        t.join(timeout=2.0)
        assert not t.is_alive(), "producer 线程未在 release 后退出"
        assert engine._producer_thread is None
        assert engine._stream is None
        assert engine._ring is None
        assert engine._active_pcm is None


class TestSeekProducerRace:
    def test_seek_in_flight_round_discarded(self):
        """D8：producer 快照后、写入前的窗口里发生 seek，
        在途轮次必须整轮作废——ring 不得混入旧位置音频、seek 不得被吞。
        """
        engine, pcm = _make_engine(seconds=2.0)
        engine._state = PlaybackState.PLAYING

        old_pos = {"value": None}
        seek_ms = 500

        class _SeekInjectingRing(RingBuffer):
            """第一次 available_write 时从"另一控制线程"插入 seek。"""

            def __init__(self, capacity, channels):
                super().__init__(capacity, channels)
                self.injected = False

            def available_write(self):
                if not self.injected:
                    self.injected = True
                    old_pos["value"] = engine._read_pos_samples
                    # producer 此刻不持 _state_lock，可安全模拟控制线程 seek
                    engine.set_position_ms(seek_ms)
                return super().available_write()

        ring = _SeekInjectingRing(int(0.5 * _SR), 2)
        engine._ring = ring

        t = threading.Thread(target=engine._producer_loop, daemon=True)
        engine._producer_stop.clear()
        engine._producer_thread = t
        t.start()

        # 等 producer 跑过注入点并稳定若干轮
        deadline = time.time() + 2.0
        while time.time() < deadline:
            with engine._state_lock:
                rp = engine._read_pos_samples
            if ring.injected and rp >= int(seek_ms / 1000 * _SR) and rp > (
                old_pos["value"] or 0
            ):
                time.sleep(0.05)
                break
            time.sleep(0.005)

        # 先快照 ring 内容（_stop_streaming 会 reset ring）
        drained = {"avail": 0, "data": None}
        avail = ring.available_read()
        if avail > 0:
            out = np.zeros((avail, 2), np.float32)
            ring.read_into(out)
            drained = {"avail": avail, "data": out}

        engine._stop_streaming()

        seek_samples = int(seek_ms / 1000 * _SR)
        with engine._state_lock:
            rp = engine._read_pos_samples
        # seek 未被吞：read_pos 从 seek 目标起步
        assert rp >= seek_samples
        # _stop_streaming 会 reset ring，故停机前已快照内容
        assert drained["avail"] > 0
        k = min(drained["avail"], 2048)
        # ring 内容从 seek 位置连续（旧实现在此窗口会把旧位置数据写进 ring）
        np.testing.assert_allclose(
            drained["data"][:k], pcm[seek_samples : seek_samples + k], atol=1e-6
        )

    def test_seek_resets_anchor_and_epoch(self):
        """seek 递增 epoch 并重置锚点（防在途喂数据轮次覆盖新位置）。"""
        engine, _pcm = _make_engine()
        engine._consumed_in_active_pcm = 999
        before = engine._epoch
        engine.set_position_ms(500)
        assert engine._epoch == before + 1
        assert engine._consumed_in_active_pcm == int(0.5 / 1000 * _SR * 1000)
        assert engine._read_pos_samples == int(0.5 / 1000 * _SR * 1000)


class TestCallbackAnchorAtomicity:
    def test_concurrent_callbacks_do_not_lose_updates(self):
        """D9：并发回调下 _consumed_in_active_pcm += n 不得丢更新。"""
        engine, _pcm = _make_engine()
        engine._state = PlaybackState.PLAYING
        engine._ring = _AlwaysFullRing()

        n_threads, iters, frames = 8, 400, 1024
        out = np.zeros((frames, 2), np.float32)

        errors: list = []

        def worker():
            try:
                for _ in range(iters):
                    engine._audio_callback(out, frames, None, 0)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert engine._consumed_in_active_pcm == n_threads * iters * frames
        assert engine._last_callback_perf_time > 0

    def test_seek_and_callback_anchor_consistent(self):
        """seek 重置锚点后，回调继续从新锚点累加（锁序一致）。"""
        engine, _pcm = _make_engine()
        engine._state = PlaybackState.PLAYING
        engine.set_position_ms(500)
        base = engine._consumed_in_active_pcm
        out = np.zeros((1024, 2), np.float32)
        engine._ring = _AlwaysFullRing()
        engine._audio_callback(out, 1024, None, 0)
        assert engine._consumed_in_active_pcm == base + 1024


class TestEofStateGuards:
    def test_on_eof_sets_paused_when_playing(self):
        engine, _pcm = _make_engine()
        engine._state = PlaybackState.PLAYING
        engine._on_eof()
        assert engine._state == PlaybackState.PAUSED

    def test_on_eof_does_not_clobber_stopped(self):
        """D14：stop() 后回调侧 _on_eof 不得把 STOPPED 改写成 PAUSED。"""
        engine, _pcm = _make_engine()
        engine._state = PlaybackState.STOPPED
        engine._producer_stop.set()
        engine._on_eof()
        assert engine._state == PlaybackState.STOPPED

    def test_on_eof_returns_early_when_stop_requested(self):
        engine, _pcm = _make_engine()
        engine._state = PlaybackState.PLAYING
        engine._producer_stop.set()
        engine._on_eof()
        assert engine._state == PlaybackState.PLAYING, "stop 在途时 _on_eof 不应改状态"


class TestHotRecoveryStopGuard:
    def test_hot_recovery_aborts_new_stream_when_stopped(self, monkeypatch):
        """D14：_stop_streaming 后旧 producer 不得复活无人认领的新流。"""
        import strange_uta_game.backend.infrastructure.audio.sounddevice_engine as sde

        engine, _pcm = _make_engine()
        created = []

        class _FakeOutputStream(_FakeStream):
            def __init__(self, **kwargs):
                super().__init__()
                created.append(self)
                self.latency = 0.05

        monkeypatch.setattr(sde.sd, "OutputStream", _FakeOutputStream)
        monkeypatch.setattr(sde.time, "sleep", lambda *_: None)
        engine._state = PlaybackState.PLAYING
        engine._stream = None
        engine._needs_recovery.set()
        engine._producer_stop.set()  # release() 已要求停机

        engine._perform_hot_recovery()

        assert engine._stream is None, "停机后不应启动新流"


class TestPrewarmCheckOnly:
    def test_prewarm_dispatches_check_only(self):
        """D7：预热只做存在性检查/派发，不做整曲解码。"""
        engine, _pcm = _make_engine()
        calls = []

        class _StubCache:
            def ensure(self, speed, priority=99, check_only=False, **kw):
                calls.append((speed, priority, check_only))
                return None

        engine._cache = _StubCache()
        engine.prewarm_speeds(speed_min=0.5, speed_max=1.0)

        assert calls, "预热应派发速度任务"
        assert all(c[2] is True for c in calls), "预热必须使用 check_only 路径"
        # sounddevice 引擎预热表按优先级排序
        assert [c[0] for c in calls] == [0.75, 0.5, 0.9, 0.8, 0.7, 0.6]
