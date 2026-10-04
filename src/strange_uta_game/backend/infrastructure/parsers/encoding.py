"""歌词文件字节流解码（公共编码回退链）。

历史实现只在 utf-8 失败后回退 shift_jis，GBK/Big5 歌词要么抛
UnicodeDecodeError，要么被错误解码成静默乱码。本模块提供统一的
``decode_lyric_bytes``，供所有歌词解析入口共用：

    BOM（UTF-16/32）→ utf-8-sig → cp932（Shift-JIS 超集）→ gb18030 → big5

按顺序尝试，成功即返回；全部失败时抛最后一次的 UnicodeDecodeError，
由调用方包装成各自的异常类型（ParseError / ProjectImportError）。
"""

from __future__ import annotations

import codecs

# 无 BOM 时的候选编码（按优先级）。
_CANDIDATE_ENCODINGS = ("utf-8-sig", "cp932", "gb18030", "big5")


def decode_lyric_bytes(data: bytes) -> tuple[str, str]:
    """把歌词文件字节流按回退链解码为文本。

    Args:
        data: 文件原始字节。

    Returns:
        (text, encoding_name)：解码后的文本与实际使用的编码名。
        UTF-8 BOM 由 utf-8-sig 自动剥离。

    Raises:
        UnicodeDecodeError: 所有候选编码都无法解码时（抛最后一次的错误）。
    """
    if not data:
        return "", "utf-8-sig"

    # 带 BOM 的 UTF-16/UTF-32（Windows 记事本"Unicode"保存常见）直接按
    # BOM 解码。注意 UTF-32-LE 的 BOM 以 UTF-16-LE 的 BOM 为前缀，必须先判长 BOM。
    for bom, encoding in (
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    ):
        if data.startswith(bom):
            return data.decode(encoding), encoding

    last_error: UnicodeDecodeError | None = None
    for encoding in _CANDIDATE_ENCODINGS:
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError as exc:
            last_error = exc
    assert last_error is not None
    raise last_error
