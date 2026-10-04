"""BASS 双引擎修复回归测试（D5 / D6 / D10 / D12）。

BASS 仅 Windows 可用。测试不依赖真实设备状态转换：需要 BASS 全局会话
的地方一律 monkeypatch（BASS_Free 等绝不能真跑，否则杀掉进程级会话影响
其它测试）。
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest
import soundfile as sf

from strange_uta_game.backend.infrastructure.audio import bass_available
from strange_uta_game.backend.infrastructure.audio.base import PlaybackState

pytestmark = pytest.mark.skipif(not bass_available, reason="BASS 引擎仅 Windows 可用")

if bass_available:
    from strange_uta_game.backend.infrastructure.audio.bass_engine import (
        BassEngine,
        _bass,
    )
    from strange_uta_game.backend.infrastructure.audio.bass_tsm_engine import (
        BassTsmEngine,
    )


@pytest.fixture
def tiny_wav(tmp_path) -> str:
    sr = 44100
    t = np.linspace(0, 1.0, sr, endpoint=False)
    p = tmp_path / "tone.wav"
    sf.write(str(p), (np.sin(2 * np.pi * 440 * t) * 0.2).astype(np.float32), sr)
    return str(p)


# ═══════════════════════ D6 bass_engine._recover_device_sync ═══════════════════════


class TestRecoverSyncJoin:
    def test_releases_stream_lock_before_join(self):
        """D6：后台恢复线程需要 _stream_lock 才能推进；持锁 join 必然等满
        3s 超时。修复后先释放本线程的外层锁再 join，后台线程能完成。
        """
        engine = BassEngine()
        engine._playback_path = "fake.wav"
        engine._recovering = True
        engine._tempo_stream = 12345  # join 后按句柄判断恢复成功

        bg_got_lock = threading.Event()

        def background_recovery():
            # 模拟 _run_recovery → _do_recover：需要 _stream_lock
            with engine._stream_lock:
                bg_got_lock.set()

        t = threading.Thread(target=background_recovery, daemon=True)
        engine._recovery_thread = t
        t.start()

        acquired = threading.Event()
        result = {}

        def user_play_path():
            # 模拟 play()：持锁进入 _recover_device_sync
            with engine._stream_lock:
                acquired.set()
                result["ok"] = engine._recover_device_sync("play failed")

        u = threading.Thread(target=user_play_path, daemon=True)
        u.start()
        assert acquired.wait(timeout=2.0)

        # 修复后 join 前释放锁 → bg 拿到锁；旧实现此处 bg 要等满 3s 超时
        assert bg_got_lock.wait(timeout=2.5), "恢复线程未在 2.5s 内拿到锁（持锁 join 未修复）"
        u.join(timeout=5.0)
        assert not u.is_alive()
        assert result["ok"] is True  # _tempo_stream != 0

    def test_join_path_reports_unrestored_when_no_stream(self):
        engine = BassEngine()
        engine._playback_path = "fake.wav"
        engine._recovering = True
        engine._tempo_stream = 0

        done = threading.Event()

        def bg():
            with engine._stream_lock:
                done.set()

        t = threading.Thread(target=bg, daemon=True)
        engine._recovery_thread = t
        t.start()

        result = {}

        def user_play_path():
            with engine._stream_lock:
                result["ok"] = engine._recover_device_sync("play failed")

        u = threading.Thread(target=user_play_path, daemon=True)
        u.start()
        u.join(timeout=5.0)
        assert not u.is_alive()
        assert result["ok"] is False  # 无句柄 → 恢复未成功


# ═══════════════════════ D10 设备恢复回调 ═══════════════════════


class TestDeviceRecoveredCallback:
    def test_bass_engine_fires_callback_after_bass_free(self, monkeypatch):
        """BASS_Free 已使进程内 sample 句柄失效，恢复流程（无论成败）必须
        通知 UI 层失效并重载按键音/节拍器样本。"""
        engine = BassEngine()
        engine._playback_path = "fake.wav"
        fired = []
        engine.set_device_recovered_callback(lambda: fired.append(1))

        # 不真跑 BASS_Free/重建：全部打桩
        monkeypatch.setattr(engine, "_free_streams", lambda: None)
        monkeypatch.setattr(_bass, "BASS_Free", lambda: 1)
        monkeypatch.setattr(engine, "_ensure_initialized", lambda: False)
        assert engine._do_recover() is False
        assert fired == [1], "恢复失败也必须触发回调（BASS_Free 已执行）"

    def test_bass_engine_no_callback_by_default(self, monkeypatch):
        engine = BassEngine()
        engine._playback_path = "fake.wav"
        monkeypatch.setattr(engine, "_free_streams", lambda: None)
        monkeypatch.setattr(_bass, "BASS_Free", lambda: 1)
        monkeypatch.setattr(engine, "_ensure_initialized", lambda: False)
        engine._do_recover()  # 未注册回调：不应抛错

    def test_bass_engine_callback_error_does_not_break_recovery(self, monkeypatch):
        engine = BassEngine()
        engine._playback_path = "fake.wav"
        engine.set_device_recovered_callback(lambda: 1 / 0)  # 回调异常不得外泄
        monkeypatch.setattr(engine, "_free_streams", lambda: None)
        monkeypatch.setattr(_bass, "BASS_Free", lambda: 1)
        monkeypatch.setattr(engine, "_ensure_initialized", lambda: False)
        assert engine._do_recover() is False

    def test_bass_tsm_fires_callback_after_bass_free(self, monkeypatch):
        engine = BassTsmEngine()
        fired = []
        engine.set_device_recovered_callback(lambda: fired.append(1))

        monkeypatch.setattr(engine, "_free_stream", lambda: None)
        monkeypatch.setattr(_bass, "BASS_Free", lambda: 1)
        monkeypatch.setattr(engine, "_ensure_initialized", lambda: False)
        assert engine._do_recover_locked() is False
        assert fired == [1]

    def test_base_class_default_is_noop(self):
        from strange_uta_game.backend.infrastructure.audio.base import IAudioEngine

        class _E(IAudioEngine):
            def load(self, file_path, progress_cb=None): ...
            def play(self): ...
            def pause(self): ...
            def stop(self): ...
            def get_position_ms(self): return 0
            def set_position_ms(self, position_ms): ...
            def get_duration_ms(self): return 0
            def get_playback_state(self): ...
            def is_playing(self): return False
            def set_speed(self, speed): ...
            def get_speed(self): return 1.0
            def set_volume(self, volume): ...
            def get_volume(self): return 1.0
            def set_position_callback(self, callback): ...
            def clear_position_callback(self): ...
            def get_audio_info(self): return None
            def get_original_samples(self): return None
            def get_mono_samples(self): return None
            def release(self): ...

        e = _E()
        # 默认实现存在且为 no-op（SoundDeviceEngine 等无需 BASS 句柄管理）
        e.set_device_recovered_callback(lambda: None)
        e.set_device_recovered_callback(None)


# ═══════════════════════ D5 bass_tsm 设备恢复后台化 ═══════════════════════


class TestTsmRecoveryBackground:
    def test_recover_device_spawns_background_thread(self, monkeypatch):
        """D5：恢复绝不能在轮询线程（UI）同步执行——立即返回，后台跑。"""
        engine = BassTsmEngine()
        engine._recovering = False
        engine._last_recovery_attempt = 0.0

        called = []
        done = threading.Event()

        def fake_run(reason):
            called.append(reason)
            engine._recovering = False
            done.set()

        monkeypatch.setattr(engine, "_run_recovery", fake_run)

        t0 = time.monotonic()
        engine._recover_device("device paused")
        elapsed_s = time.monotonic() - t0
        assert elapsed_s < 0.5, "recover_device 必须立即返回，不能同步恢复"
        assert done.wait(timeout=2.0), "后台恢复线程未执行"
        assert called == ["device paused"]

    def test_recover_device_throttles_repeat_calls(self, monkeypatch):
        engine = BassTsmEngine()
        engine._recovering = False
        engine._last_recovery_attempt = time.monotonic()  # 刚刚恢复过
        called = []
        monkeypatch.setattr(engine, "_run_recovery", lambda r: called.append(r))
        engine._recover_device("device paused")
        assert called == [], "1s 节流窗口内的重复请求不应派发"

    def test_run_recovery_holds_stream_lock(self, monkeypatch):
        """D5：恢复在后台线程执行且全程持 _stream_lock，异常不外泄。"""
        engine = BassTsmEngine()

        def fake_do():
            # 由 _run_recovery 在 with self._stream_lock 内调用
            assert engine._stream_lock._is_owned(), "恢复主体必须持 _stream_lock"
            raise RuntimeError("模拟恢复异常")

        monkeypatch.setattr(engine, "_do_recover_locked", fake_do)
        engine._recovering = True
        engine._run_recovery("device lost")  # 异常被吞，_recovering 复位
        assert engine._recovering is False

    def test_switch_to_file_rolls_back_on_build_failure(self, monkeypatch):
        """D5：_switch_to_file 必须检查 _build_stream_locked 返回值；
        失败时回滚模式标志，否则 _effective_scale 与实际流不一致。"""
        engine = BassTsmEngine()
        engine._is_tempo = True
        engine._speed_scale = 1.0
        engine._tempo_speed = 1.0
        engine._current_source_path = "src_1x.mp3"
        engine._state = PlaybackState.PAUSED
        monkeypatch.setattr(engine, "_build_stream_locked", lambda **kw: False)

        engine._switch_to_file(0.5, "rendered_05.mp3")

        assert engine._is_tempo is True
        assert engine._speed_scale == 1.0
        assert engine._current_source_path == "src_1x.mp3"

    def test_switch_to_file_applies_flags_on_success(self, monkeypatch):
        engine = BassTsmEngine()
        engine._is_tempo = True
        engine._speed_scale = 1.0
        engine._current_source_path = "src_1x.mp3"
        engine._state = PlaybackState.PAUSED
        monkeypatch.setattr(engine, "_build_stream_locked", lambda **kw: True)

        engine._switch_to_file(0.5, "rendered_05.mp3")

        assert engine._is_tempo is False
        assert engine._speed_scale == 0.5
        assert engine._current_source_path == "rendered_05.mp3"

    def test_switch_to_tempo_rolls_back_on_build_failure(self, monkeypatch):
        """同类站点：_switch_to_tempo 失败同样回滚。"""
        engine = BassTsmEngine()
        engine._is_tempo = False
        engine._speed_scale = 0.5
        engine._current_source_path = "rendered_05.mp3"
        engine._tempo_speed = 1.0
        engine._state = PlaybackState.PAUSED
        monkeypatch.setattr(engine, "_build_stream_locked", lambda **kw: False)

        engine._switch_to_tempo(1.5)

        assert engine._is_tempo is False
        assert engine._speed_scale == 0.5
        assert engine._current_source_path == "rendered_05.mp3"


# ═══════════════════════ D7 预热不整曲解码 ═══════════════════════


class TestPrewarmCheckOnly:
    def test_prewarm_speeds_dispatches_check_only(self, monkeypatch):
        """D7：预热在 UI 线程（持 _stream_lock）运行，必须走 check_only
        存在性检查——绝不做整曲 MP3 同步解码。"""
        engine = BassTsmEngine()
        calls = []

        class _StubCache:
            def ensure(self, speed, priority=99, check_only=False, **kw):
                calls.append((speed, priority, check_only))
                return None

        engine._cache = _StubCache()
        engine.prewarm_speeds(speed_min=0.2, speed_max=2.0)

        assert calls, "预热应派发速度任务"
        assert all(c[2] is True for c in calls), "预热必须使用 check_only 路径"
        speeds = [c[0] for c in calls]
        assert speeds == [0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 1.25, 1.5]
        # 范围过滤：只派发滑块范围内的速度
        calls.clear()
        engine.prewarm_speeds(speed_min=0.5, speed_max=1.0)
        assert [c[0] for c in calls] == [0.9, 0.8, 0.7, 0.6, 0.5]


# ═══════════════════════ D12 _decode_full_pcm 守卫 ═══════════════════════


class TestDecodeFullPcmGuard:
    def test_getinfo_failure_falls_back_to_soundfile(self, tiny_wav, monkeypatch):
        """GetInfo 失败 → 退回 soundfile 解码，不再用未初始化的 info。"""
        monkeypatch.setattr(_bass, "BASS_ChannelGetInfo", lambda *a: 0)

        pcm, sr, ch = BassTsmEngine._decode_full_pcm(tiny_wav)

        data, sf_sr = sf.read(tiny_wav, dtype="float32")
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        assert pcm.shape == data.shape
        assert sr == sf_sr
        assert ch == data.shape[1]

    def test_getlength_failure_falls_back_to_soundfile(self, tiny_wav, monkeypatch):
        """D12：GetLength 失败（c_uint64 下溢为 0xFFFF...）不得 np.empty(OOM)。"""
        monkeypatch.setattr(_bass, "BASS_ChannelGetInfo", lambda *a: 1)
        monkeypatch.setattr(
            _bass, "BASS_ChannelGetLength", lambda *a: (1 << 64) - 1
        )

        pcm, sr, ch = BassTsmEngine._decode_full_pcm(tiny_wav)

        data, sf_sr = sf.read(tiny_wav, dtype="float32")
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        assert pcm.shape == data.shape

    def test_decode_real_wav_matches_soundfile(self, tiny_wav):
        """无 monkeypatch 的真实解码路径（FLOAT wav 无编解码延迟，长度应一致）。"""
        pcm, sr, ch = BassTsmEngine._decode_full_pcm(tiny_wav)
        data, sf_sr = sf.read(tiny_wav, dtype="float32")
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        assert pcm.shape[0] == data.shape[0]
        assert ch == data.shape[1]
        assert sr == sf_sr
