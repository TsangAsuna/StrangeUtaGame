"""SdSampleStreamPool 回归测试（D15：mac 回退 sd.play 后次掐前次）。

sd.play 是全局单例播放器，后一次调用会掐掉前一次。修复：轮换一小池
常驻 OutputStream。这里用 Fake 流验证轮换/兜底/回调填充逻辑，不碰真实
音频设备（构造 OutputStream 时 monkeypatch）。
"""

from __future__ import annotations

import numpy as np
import pytest

import strange_uta_game.backend.infrastructure.audio.keysound_player as ks_mod
import strange_uta_game.backend.infrastructure.audio.metronome_player as metro_mod
from strange_uta_game.backend.infrastructure.audio.keysound_player import (
    SdSampleStreamPool,
    SoundDeviceKeySoundPlayer,
)
from strange_uta_game.backend.infrastructure.audio.metronome_player import (
    SoundDeviceMetronomePlayer,
)


class _FakeOutputStream:
    created = []
    fail = False

    def __init__(self, **kwargs):
        if _FakeOutputStream.fail:
            raise RuntimeError("no device")
        self.kwargs = kwargs
        self.started = False
        self.stop_calls = 0
        self.close_calls = 0
        self.active = False
        _FakeOutputStream.created.append(self)

    def start(self):
        self.started = True
        self.active = True

    def stop(self):
        self.stop_calls += 1
        self.active = False

    def close(self):
        self.close_calls += 1
        self.active = False


@pytest.fixture
def fake_sd(monkeypatch):
    """把两个回退播放器模块里的 sounddevice 换成 Fake + 记录全局 sd.play。"""
    global_plays = []
    _FakeOutputStream.created = []
    _FakeOutputStream.fail = False

    class _SdShim:
        OutputStream = staticmethod(lambda **kw: _FakeOutputStream(**kw))

        @staticmethod
        def play(data, sr):
            global_plays.append((data, sr))

    monkeypatch.setattr(ks_mod, "_sd", _SdShim)
    monkeypatch.setattr(metro_mod, "_sd", _SdShim)
    return global_plays


def _tone(n=1000, ch=1):
    return (np.random.RandomState(3).randn(n, ch) * 0.1).astype(np.float32)


class TestSdSampleStreamPool:
    def test_rotation_uses_distinct_streams(self, fake_sd):
        """连续播放轮换不同流：后一次不掐前一次。"""
        pool = SdSampleStreamPool(size=3)
        for _ in range(3):
            pool.play(_tone(), 44100)

        streams = _FakeOutputStream.created
        assert len(streams) == 3, "3 次播放应轮换 3 个独立流"
        assert all(s.started for s in streams)
        assert fake_sd == [], "流可用时不得退回全局 sd.play"

    def test_over_capacity_reuses_oldest_slot(self, fake_sd):
        """超过池大小：复用最旧槽（对齐 BASS_SAMPLE_OVER_POS 语义）。"""
        pool = SdSampleStreamPool(size=3)
        for _ in range(4):
            pool.play(_tone(), 44100)

        assert len(_FakeOutputStream.created) == 3, "不应无限建流"
        # 第 4 次回到 slot0：pending 被新数据替换
        assert pool._pending[0] is not None

    def test_no_global_play_cutdown_between_rapid_hits(self, fake_sd):
        """连打 6 次按键音：6 份数据都挂在槽位上等待播放，而非互相掐断。"""
        pool = SdSampleStreamPool(size=3)
        for _ in range(6):
            pool.play(_tone(), 44100)
        pending_slots = [i for i, p in enumerate(pool._pending) if p is not None]
        assert len(pending_slots) == 3
        assert fake_sd == []

    def test_falls_back_to_sd_play_when_stream_creation_fails(self, fake_sd):
        _FakeOutputStream.fail = True
        pool = SdSampleStreamPool(size=3)
        data = _tone()
        pool.play(data, 44100)
        assert len(fake_sd) == 1, "设备不可用时应退回全局 sd.play（旧行为）"
        assert fake_sd[0][0] is data

    def test_falls_back_when_start_fails(self, fake_sd, monkeypatch):
        pool = SdSampleStreamPool(size=3)
        calls = []

        def bad_start(self):
            raise RuntimeError("device gone")

        monkeypatch.setattr(_FakeOutputStream, "start", bad_start)
        pool.play(_tone(), 44100)
        assert len(fake_sd) == 1

    def test_rebuilds_pool_on_sample_rate_change(self, fake_sd):
        pool = SdSampleStreamPool(size=3)
        pool.play(_tone(), 44100)
        first = list(_FakeOutputStream.created)
        pool.play(_tone(), 22050)
        assert all(s.close_calls >= 1 for s in first), "采样率变化后旧流应整池关闭"

    def test_callback_fills_and_pads(self, fake_sd):
        pool = SdSampleStreamPool(size=2)
        data = _tone(n=300)
        pool.play(data, 44100)
        out = np.zeros((1024, 1), np.float32)
        pool._cb(out, 1024, None, 0, slot=0)
        np.testing.assert_allclose(out[:300, 0], data[:, 0], atol=1e-6)
        assert np.all(out[300:] == 0), "播完后剩余块补零"
        assert pool._pending[0] is None, "播完应清槽"

    def test_callback_restarts_mid_data(self, fake_sd):
        pool = SdSampleStreamPool(size=2)
        data = _tone(n=1000)
        pool.play(data, 44100)
        out = np.zeros((400, 1), np.float32)
        pool._cb(out, 400, None, 0, slot=0)
        # 下一块从 400 帧处继续
        out2 = np.zeros((400, 1), np.float32)
        pool._cb(out2, 400, None, 0, slot=0)
        np.testing.assert_allclose(out2[:, 0], data[400:800, 0], atol=1e-6)

    def test_stereo_data_gets_stereo_stream(self, fake_sd):
        pool = SdSampleStreamPool(size=2)
        pool.play(_tone(ch=2), 44100)
        assert _FakeOutputStream.created[0].kwargs["channels"] == 2

    def test_close(self, fake_sd):
        pool = SdSampleStreamPool(size=2)
        pool.play(_tone(), 44100)
        pool.close()
        assert all(s.close_calls >= 1 for s in _FakeOutputStream.created)
        pool.play(_tone(), 44100)  # close 后再次播放应能重建流
        assert _FakeOutputStream.created[-1].started


class TestFallbackPlayersUsePool:
    def test_keysound_player_overlapping_plays(self, fake_sd, tmp_path):
        import soundfile as _sf

        press = tmp_path / "press.wav"
        _sf.write(str(press), _tone(500), 44100)
        release = tmp_path / "release.wav"
        _sf.write(str(release), _tone(500), 44100)

        player = SoundDeviceKeySoundPlayer()
        player.load(press, release)
        for _ in range(4):
            player.play_press()
        assert len(_FakeOutputStream.created) >= 2, "连打应轮换流，而非 sd.play 互掐"
        assert fake_sd == []

    def test_metronome_player_overlapping_plays(self, fake_sd, tmp_path):
        import soundfile as _sf

        beat = tmp_path / "beat.wav"
        _sf.write(str(beat), _tone(300), 44100)
        accent = tmp_path / "accent.wav"
        _sf.write(str(accent), _tone(300), 44100)

        player = SoundDeviceMetronomePlayer()
        player.load(beat, accent)
        player.set_volume(50)
        for _ in range(4):
            player.play_beat()
        assert len(_FakeOutputStream.created) >= 2
        assert fake_sd == []
        player.free()  # 应关闭流池
        assert all(s.close_calls >= 1 for s in _FakeOutputStream.created)
