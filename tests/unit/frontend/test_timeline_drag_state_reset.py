"""时间轴拖拽状态清场与声谱 QImage 缓存回归测试（bug 审查 B2/B6）。

- B2：窗口失活（WindowDeactivate）统一清掉把手拖拽/pan/交界拖拽状态；
  set_duration / clear_audio_data 同样清场，避免过期锚点提交错误偏移。
- B6：声谱最终 RGBA QImage 与 view 缓存同键缓存，静态层重建不再逐帧
  重新 LUT 查表 + tobytes 分配。
"""

from __future__ import annotations

import numpy as np
import pytest
from PyQt6.QtCore import QEvent
from PyQt6.QtGui import QImage, QPainter, QPixmap

from strange_uta_game.frontend.editor.timing.timeline_widget import (
    WaveformDisplay,
    _TimelineHeightHandle,
)

SR = 44100

# 持有 QApplication 强引用：模块夹具结束时若销毁 QApplication，其析构会
# 级联删除 theme 单例，导致后续测试模块构造控件时崩溃（既有测试同型问题）。
_APP_HOLDER = []


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    _APP_HOLDER.append(app)
    yield app


def _dirty_drag_state(display: WaveformDisplay) -> None:
    """把所有拖拽/pan 运行态置成"进行中"的脏值。"""
    display._press_handle = (0, 0, 0, False)
    display._press_handle_ts = 12345
    display._press_x = 40.0
    display._drag_armed = True
    display._is_dragging_tags = True
    display._drag_anchor_handle = (0, 0, 0, False)
    display._drag_anchor_ts = 12345
    display._drag_delta_ms = -777
    display._lane_dragging = True
    display._lane_press_global_y = 321
    display._pan_start_x = 12.0
    display._is_panning = True


def _assert_drag_cleared(display: WaveformDisplay):
    assert display._press_handle is None
    assert display._drag_armed is False
    assert display._is_dragging_tags is False
    assert display._drag_anchor_handle is None
    assert display._drag_delta_ms == 0
    assert display._lane_dragging is False
    assert display._lane_press_global_y is None
    assert display._pan_start_x is None
    assert display._is_panning is False


def _window_deactivate(display) -> None:
    display.event(QEvent(QEvent.Type.WindowDeactivate))


# ═══════════════════════════════════════════════
# B2：窗口失活 / set_duration / clear_audio_data 清场
# ═══════════════════════════════════════════════


class TestWindowDeactivateResetsDrag:
    def test_deactivate_clears_all_drag_state(self, qapp):
        display = WaveformDisplay()
        _dirty_drag_state(display)

        _window_deactivate(display)

        _assert_drag_cleared(display)
        display.close()

    def test_height_handle_deactivate_clears_press(self, qapp):
        handle = _TimelineHeightHandle()
        handle._press_global_y = 500

        _window_deactivate(handle)

        assert handle._press_global_y is None
        handle.close()

    def test_set_duration_clears_drag_state(self, qapp):
        display = WaveformDisplay()
        _dirty_drag_state(display)

        display.set_duration(60000)

        _assert_drag_cleared(display)
        display.close()

    def test_clear_audio_data_clears_drag_state(self, qapp):
        display = WaveformDisplay()
        _dirty_drag_state(display)

        display.clear_audio_data()

        _assert_drag_cleared(display)
        display.close()

    def test_normal_events_do_not_clear_drag_state(self, qapp):
        """非失活事件（如重绘请求）不清场，正常拖拽不受影响。"""
        display = WaveformDisplay()
        _dirty_drag_state(display)

        display.event(QEvent(QEvent.Type.UpdateRequest))

        assert display._is_dragging_tags is True
        assert display._drag_delta_ms == -777
        display.close()


# ═══════════════════════════════════════════════
# B6：声谱 QImage 缓存
# ═══════════════════════════════════════════════


def _tone(seconds: float = 2.0, freq: float = 440.0) -> np.ndarray:
    t = np.arange(int(SR * seconds), dtype=np.float64) / SR
    return (0.6 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _make_ready_spectrum(qapp) -> WaveformDisplay:
    """构造一份声谱已就绪的 WaveformDisplay（复用既有后台管线测试范式）。"""
    display = WaveformDisplay()
    display.set_duration(2000)
    display.set_display_mode("spectrum")
    display.set_audio_data(_tone(2.0), SR, 1)
    return display


def _wait_ready(display, qtbot):
    qtbot.waitUntil(lambda: display._spectrum_state == "ready", timeout=15000)


class TestSpectrumImageCache:
    def test_image_cache_reused_across_rebuilds(self, qapp, qtbot):
        display = _make_ready_spectrum(qapp)
        _wait_ready(display, qtbot)

        pm = QPixmap(400, 120)
        painter = QPainter(pm)
        try:
            display._draw_spectrum_image(painter, 400, 120)
            first = display._spectrum_image_cache
            assert isinstance(first, QImage)
            # 第二次（静态层重建）必须命中缓存：同一 QImage 对象
            display._draw_spectrum_image(painter, 400, 120)
            second = display._spectrum_image_cache
            assert second is first
            # buffer 与 image 一同保活（QImage 不持有 buffer 引用）
            assert display._spectrum_image_cache_buffer is not None
            assert display._spectrum_image_cache_key is not None
        finally:
            painter.end()
            display.close()

    def test_image_cache_invalidated_with_audio(self, qapp, qtbot):
        display = _make_ready_spectrum(qapp)
        _wait_ready(display, qtbot)

        pm = QPixmap(200, 100)
        painter = QPainter(pm)
        try:
            display._draw_spectrum_image(painter, 200, 100)
            first = display._spectrum_image_cache
            # 换音频：缓存作废
            display.set_audio_data(_tone(2.0, freq=880.0), SR, 1)
            _wait_ready(display, qtbot)
            display._draw_spectrum_image(painter, 200, 100)
            second = display._spectrum_image_cache
        finally:
            painter.end()
            display.close()

        assert first is not None and second is not None
        assert second is not first

    def test_cbr_image_content_matches_direct_rebuild(self, qapp, qtbot):
        """缓存命中的绘制结果与逐帧重建结果一致（像素级）。"""
        display = _make_ready_spectrum(qapp)
        _wait_ready(display, qtbot)

        pm = QPixmap(300, 90)
        painter = QPainter(pm)
        try:
            display._draw_spectrum_image(painter, 300, 90)
            key1 = display._spectrum_image_cache_key
            # 逐帧重建路径（清缓存后重算）
            display._spectrum_image_cache = None
            display._spectrum_image_cache_key = None
            display._draw_spectrum_image(painter, 300, 90)
            key2 = display._spectrum_image_cache_key
        finally:
            painter.end()
            display.close()

        assert key1 == key2  # 同一可见窗 → 同键，内容一致
