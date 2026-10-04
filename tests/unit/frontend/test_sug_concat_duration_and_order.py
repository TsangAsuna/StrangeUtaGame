"""SUG 拼接时长探测 / 排序信号回归测试（bug 审查 B5/B10/B11）。

- B5：_read_duration_via_bass_probe 复用 bass_engine 的两参签名
  （旧实现 hasattr 守卫恒真 + 三参 c_int 误读 → BASS 路径恒 0）
- B10：SugConcatDialog 的 order_changed 只在初始化连接一次
- B11：MP3 帧头时长估算扣除 ID3v2 体积、Xing 帧数优先
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

from strange_uta_game.frontend.editor.timing.sug_concat_dialog import (
    SugConcatDialog,
    SugEntry,
)
from strange_uta_game.frontend.editor.timing.sug_concat_worker import (
    _read_mp3_duration,
)

# 持有 QApplication 强引用：模块夹具结束时若销毁 QApplication，其析构会
# 级联删除 theme 单例，导致后续测试模块构造控件时崩溃（既有测试同型问题）。
_APP_HOLDER = []


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    _APP_HOLDER.append(app)
    yield app


# ═══════════════════════════════════════════════
# B11：MP3 帧头时长估算
# ═══════════════════════════════════════════════

# MPEG1 Layer III 44100Hz 128kbps 立体声帧头
_M1L3_HEADER = bytes([0xFF, 0xFB, 0x90, 0x00])
_FRAME_SIZE = 417


def _build_mp3(
    path: Path,
    frames: int = 200,
    *,
    with_id3: bool = True,
    with_xing: bool = True,
    trailing_junk: int = 0,
) -> None:
    """构造最小 MPEG1 L3 MP3：ID3v2 + 首帧 Xing/Info + 静音帧。"""
    header = _M1L3_HEADER

    def first_frame() -> bytes:
        body = b"\x00" * _FRAME_SIZE
        if with_xing:
            xing = b"Xing" + struct.pack(">I", 0x03)  # FRAMES|BYTES
            xing += struct.pack(">I", frames)
            xing += struct.pack(">I", frames * _FRAME_SIZE)
            body = b"\x00" * 32 + xing  # MPEG1 立体声 side info = 32 字节
            body += b"\x00" * (_FRAME_SIZE - len(body))
        return header + body

    id3 = b""
    if with_id3:
        payload = b"\x00" * 500
        size = len(payload)
        ss = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F,
                    (size >> 7) & 0x7F, size & 0x7F])
        id3 = b"ID3\x03\x00\x00" + ss + payload

    with open(path, "wb") as f:
        f.write(id3)
        f.write(first_frame())
        for _ in range(frames - 1):
            f.write(header + b"\x00" * _FRAME_SIZE)
        f.write(b"\x00" * trailing_junk)


class TestMp3Duration:
    def test_mpeg1_no_longer_returns_zero(self, tmp_path):
        """旧实现对 MPEG1（最常见 MP3）采样率表取错列 → 恒返回 0。"""
        p = tmp_path / "m1.mp3"
        _build_mp3(p, with_id3=False, with_xing=False)
        assert _read_mp3_duration(str(p)) > 0

    def test_xing_frame_count_takes_priority(self, tmp_path):
        """可读 Xing 时按帧数估算：200 帧 @44100 = 5224ms（精确值）。"""
        p = tmp_path / "xing.mp3"
        _build_mp3(p, frames=200, with_id3=True, with_xing=True)
        assert _read_mp3_duration(str(p)) == 200 * 1152 * 1000 // 44100

    def test_xing_ignores_trailing_junk(self, tmp_path):
        """Xing 帧数估算不受文件尾部无关字节影响（旧 CBR 估算会偏高）。"""
        p = tmp_path / "junk.mp3"
        _build_mp3(p, frames=200, with_id3=False, with_xing=True, trailing_junk=20000)
        assert _read_mp3_duration(str(p)) == 200 * 1152 * 1000 // 44100

    def test_id3v2_size_subtracted_in_cbr_estimate(self, tmp_path):
        """CBR 兜底：扣除 ID3v2 体积后估算，有无 ID3 结果一致。"""
        p_with = tmp_path / "with.mp3"
        p_without = tmp_path / "without.mp3"
        _build_mp3(p_with, frames=200, with_id3=True, with_xing=False)
        _build_mp3(p_without, frames=200, with_id3=False, with_xing=False)
        dur_with = _read_mp3_duration(str(p_with))
        dur_without = _read_mp3_duration(str(p_without))
        assert dur_with == dur_without
        # 音频本体 200*421 字节 @128kbps ≈ 5262ms
        assert abs(dur_without - 84200 * 8 / 128) < 5

    def test_empty_path_returns_zero(self):
        assert _read_mp3_duration("") == 0


# ═══════════════════════════════════════════════
# B5：BASS 探测（仅 win32 + bass.dll 存在时实跑）
# ═══════════════════════════════════════════════


def _bass_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        from strange_uta_game.backend.infrastructure.audio.bass_engine import _bass  # noqa: F401

        return True
    except Exception:
        return False


@pytest.mark.skipif(not _bass_available(), reason="需要 Windows + bass.dll")
class TestBassProbe:
    def test_probe_real_mp3_returns_nonzero(self, tmp_path):
        """旧实现三参调用触发 argtypes 校验失败 → 恒 0；两参调用应得到时长。

        用 soundfile（libsndfile mp3 编码器）生成真实可解码的 MP3。
        """
        import numpy as np
        import soundfile as sf

        from strange_uta_game.frontend.editor.timing.sug_concat_worker import (
            _read_duration_via_bass_probe,
        )

        sr = 44100
        data = (0.5 * np.sin(2 * np.pi * 440 * np.arange(sr) / sr)).astype(np.float32)
        p = tmp_path / "real.mp3"
        sf.write(str(p), data, sr, format="MP3")

        expected_ms = int(sf.info(str(p)).duration * 1000)
        got = _read_duration_via_bass_probe(str(p))
        assert got > 0
        # 解码时长应与 soundfile 一致（±50ms）
        assert abs(got - expected_ms) <= 50


# ═══════════════════════════════════════════════
# B10：order_changed 只连接一次
# ═══════════════════════════════════════════════


class TestOrderChangedSingleConnect:
    def test_order_changed_connected_once(self, qapp, tmp_path):
        """添加多张卡后 order_changed → _on_order_changed 只有一条连接。"""
        dlg = SugConcatDialog()
        for i in range(3):
            entry = SugEntry(file_path=str(tmp_path / f"s{i}.sug"))
            dlg._add_card(entry)

        # 逐条断开计数：能成功断开几次 = 连接了几条
        count = 0
        while True:
            try:
                dlg._container.order_changed.disconnect(dlg._on_order_changed)
                count += 1
            except TypeError:
                break
        assert count == 1
        dlg.close()
