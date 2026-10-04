"""encoding.decode_lyric_bytes — 公共编码回退链测试（E1）。

历史实现只有 utf-8 → shift_jis 两级回退：GBK/Big5 歌词文件要么抛
UnicodeDecodeError，要么走 shift_jis 产生静默乱码。
"""

import codecs

import pytest

from strange_uta_game.backend.infrastructure.parsers.encoding import decode_lyric_bytes


class TestDecodeLyricBytes:
    def test_utf8_plain(self):
        text, enc = decode_lyric_bytes("あいう".encode("utf-8"))
        assert text == "あいう"
        assert enc == "utf-8-sig"

    def test_utf8_bom_stripped(self):
        text, enc = decode_lyric_bytes(codecs.BOM_UTF8 + "歌詞".encode("utf-8"))
        assert text == "歌詞"
        assert enc == "utf-8-sig"

    def test_utf16_le_bom(self):
        text, enc = decode_lyric_bytes("歌詞".encode("utf-16"))
        assert text == "歌詞"
        assert enc == "utf-16"

    def test_utf16_be_bom(self):
        data = codecs.BOM_UTF16_BE + "歌詞".encode("utf-16-be")
        text, enc = decode_lyric_bytes(data)
        assert text == "歌詞"
        assert enc == "utf-16"

    def test_shift_jis(self):
        text, enc = decode_lyric_bytes("歌詞".encode("cp932"))
        assert text == "歌詞"
        assert enc == "cp932"

    def test_empty_bytes(self):
        text, _enc = decode_lyric_bytes(b"")
        assert text == ""

    def test_undecodable_raises_unicode_decode_error(self):
        # 0x81 0x00 在 cp932/gb18030/big5 中都不是合法序列
        with pytest.raises(UnicodeDecodeError):
            decode_lyric_bytes(b"\x81\x00\x81\x01")

    def test_gbk_lyrics_no_longer_crash(self):
        """GBK 编码歌词（此前 utf-8 失败、shift_jis 也常失败 → ParseError）
        现在能被回退链解码，不再直接抛错。"""
        original = "从此我不能听见你的温柔"
        text, _enc = decode_lyric_bytes(original.encode("gbk"))
        assert text == original
