"""LyricReadWorker / LyricParseWorker 编码回退链（H12）。

两处歌词读取此前只做 utf-8 → shift_jis 回退：GBK/Big5 歌词要么报错、
要么被 shift_jis 解成静默乱码。现统一走
``backend.infrastructure.parsers.encoding.decode_lyric_bytes``
（BOM → utf-8-sig → cp932 → gb18030 → big5），与歌词解析器/项目导入一致。
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QObject
from PyQt6.QtWidgets import QApplication

from strange_uta_game.frontend.workers import LyricParseWorker, LyricReadWorker


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _run_worker(worker: QObject):
    """同步调用 worker.run()，经信号收集结果（无需起线程）。"""
    box = {"result": None, "error": None, "progress": []}
    if hasattr(worker, "finished"):
        worker.finished.connect(lambda *args: box.__setitem__("result", args))
    worker.error.connect(box.__setitem__ if False else lambda msg: box.__setitem__("error", msg))
    if hasattr(worker, "progress"):
        worker.progress.connect(lambda *args: box["progress"].append(args))
    worker.run()
    return box


def test_read_utf8_bom(tmp_path, qapp):
    path = tmp_path / "lyric.txt"
    path.write_bytes(b"\xef\xbb\xbf" + "大冒険".encode("utf-8"))
    worker = LyricReadWorker(str(path))
    box = _run_worker(worker)
    assert box["error"] is None
    assert box["result"][0] == "大冒険"


def test_read_gb18030_fallback(tmp_path, qapp):
    """GBK 字节（cp932 无法解码）回退到 gb18030，不再乱码。

    选字有讲究：常见词（如「歌词文件」）的 GBK 字节恰好也是合法 cp932，
    会按契约优先被 cp932 解出（乱码碰撞属契约既定顺序）；这里选用
    GBK 字节含非法 Shift-JIS 前导的字（0xCD 0xD3 0xB3），确保真正走到
    gb18030 回退分支。
    """
    path = tmp_path / "lyric.txt"
    text = "万与丑"
    path.write_bytes(text.encode("gb18030"))
    worker = LyricReadWorker(str(path))
    box = _run_worker(worker)
    assert box["error"] is None
    assert box["result"][0] == text


def test_read_shift_jis_fallback(tmp_path, qapp):
    path = tmp_path / "lyric.txt"
    path.write_bytes("あいう".encode("shift_jis"))
    worker = LyricReadWorker(str(path))
    box = _run_worker(worker)
    assert box["error"] is None
    assert box["result"][0] == "あいう"


def test_read_utf16_bom(tmp_path, qapp):
    path = tmp_path / "lyric.txt"
    path.write_bytes("メモ帳Unicode".encode("utf-16"))  # 带 BOM
    worker = LyricReadWorker(str(path))
    box = _run_worker(worker)
    assert box["error"] is None
    assert box["result"][0] == "メモ帳Unicode"


def test_read_missing_file_reports_error(tmp_path, qapp):
    worker = LyricReadWorker(str(tmp_path / "nope.txt"))
    box = _run_worker(worker)
    assert box["result"] is None
    assert box["error"] is not None


def test_parse_worker_reads_file_with_fallback(tmp_path, qapp, monkeypatch):
    """LyricParseWorker 的文件读取路径同样走统一回退链。

    parse_lyric_content 依赖注音管线，测试中以桩替换，只验证读取与解码。
    """
    import strange_uta_game.frontend.editor.timing.lyric_loader as lyric_loader

    path = tmp_path / "lyric.txt"
    text = "万与丑"  # GBK 字节含非法 cp932 前导，确保走 gb18030 回退
    path.write_bytes(text.encode("gb18030"))

    def _fake_parse(content, *args, **kwargs):
        # 桩里校验解码结果：GBK 内容必须已正确解码
        assert content == text
        return [], False, [], {}

    monkeypatch.setattr(lyric_loader, "parse_lyric_content", _fake_parse)

    worker = LyricParseWorker(
        str(path),
        default_singer_id="s1",
        project_singers=[],
        software_compensation_ms=0,
        auto_check_flags={},
        user_dict=[],
        annotate_katakana_with_english=False,
    )
    box = _run_worker(worker)
    assert box["error"] is None
    assert box["result"][0]["sentences"] == []
