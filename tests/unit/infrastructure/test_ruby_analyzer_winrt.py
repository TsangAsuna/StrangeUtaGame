"""WinRT 注音分析器的原文位置映射测试。"""

import pytest
from types import SimpleNamespace

from strange_uta_game.backend.infrastructure.parsers.ruby_analyzer import (
    WinRTAnalyzer,
)


class _WhitespaceNormalizingJPA:
    """模拟 WinRT：若收到连续空白，会将其折叠为一个字符。"""

    calls = []

    @classmethod
    def get_words(cls, text):
        cls.calls.append(text)
        normalized = " ".join(text.split())
        reading = {
            "君に届くよぅに": "きみにとどくよぅに",
        }.get(normalized, normalized)
        return [SimpleNamespace(display_text=normalized, yomi_text=reading)]


def _make_analyzer():
    analyzer = WinRTAnalyzer.__new__(WinRTAnalyzer)
    analyzer._jpa = _WhitespaceNormalizingJPA
    analyzer._pykakasi_conv = None
    _WhitespaceNormalizingJPA.calls = []
    return analyzer


def test_get_pairs_preserves_consecutive_half_and_full_width_spaces():
    analyzer = _make_analyzer()
    text = "magic magic! 　君に届くよぅに"

    pairs = analyzer._get_pairs(text)

    assert "".join(surface for surface, _ in pairs) == text
    assert all(not any(char.isspace() for char in call) for call in analyzer._jpa.calls)
    assert (" ", " ") in pairs
    assert ("　", "　") in pairs


def test_analyze_keeps_reading_at_original_index_after_consecutive_spaces():
    analyzer = _make_analyzer()
    text = "magic magic! 　君に届くよぅに"

    results = analyzer.analyze(text)

    kimi_index = text.index("君")
    kimi = next(result for result in results if result.text == "君")
    assert kimi.start_idx == kimi_index
    assert kimi.end_idx == kimi_index + 1
    assert kimi.reading == "きみ"


class _WidthNormalizingJPA:
    """模拟真实 WinRT 的宽度归一：半角浊点假名在 display_text 中被
    合成（「ｶﾞ」2 字符 → 「ガ」1 字符），长度并非 1:1（实测
    Windows.Globalization JapanesePhoneticAnalyzer 行为）。"""

    calls = []

    def __init__(self, words):
        self._words = words

    @classmethod
    def from_words(cls, words):
        inst = cls.__new__(cls)
        inst._words = words
        return inst

    @classmethod
    def bind(cls, analyzer, words):
        analyzer._jpa = cls.from_words(words)
        return cls

    def get_words(self, text):
        type(self).calls.append(text)
        return self._words


def _make_analyzer_with_words(words):
    analyzer = WinRTAnalyzer.__new__(WinRTAnalyzer)
    analyzer._pykakasi_conv = None
    _WidthNormalizingJPA.bind(analyzer, [
        SimpleNamespace(display_text=d, yomi_text=y) for d, y in words
    ])
    return analyzer


class TestHalfWidthDakutenSurfaceAlignment:
    """E12：display_text 与原文长度并非 1:1——surface 必须按归一化
    前缀对齐回原文切片，否则浊点假名之后全部错位。"""

    def test_single_word_covers_full_segment(self):
        # 「ｶﾞのﾃｽﾄ」6 字符 → display 5 字符（实测 WinRT 会整词返回）
        analyzer = _make_analyzer_with_words([("ガのテスト", "がのてすと")])
        text = "ｶﾞのﾃｽﾄ"

        pairs = analyzer._get_pairs(text)

        assert len(pairs) == 1, f"单词应覆盖整段原文，实际切片：{pairs}"
        assert pairs[0][0] == text, (
            f"surface 应为完整原文 {text!r}，实际 {pairs[0][0]!r}"
        )
        assert pairs[0][1] == "がのてすと"

    def test_multi_word_surface_slices_stay_aligned(self):
        # 实测：get_words('ｶﾞのﾃｽﾄ') → ガ(ｶﾞ)/の/テスト(ﾃｽﾄ)
        analyzer = _make_analyzer_with_words(
            [("ガ", "が"), ("の", "の"), ("テスト", "てすと")]
        )
        text = "ｶﾞのﾃｽﾄ"

        pairs = analyzer._get_pairs(text)

        assert [(s, r) for s, r in pairs] == [
            ("ｶﾞ", "が"),
            ("の", "の"),
            ("ﾃｽﾄ", "てすと"),
        ]

    def test_lone_dakuten_grouped_with_following_kana(self):
        # 实测：get_words('abcﾞｶﾞ') → ａｂｃ / ゛ガ（3 字符原文 → 2 字符 display）
        analyzer = _make_analyzer_with_words([("ａｂｃ", "ａｂｃ"), ("゛ガ", "゛が")])
        text = "abcﾞｶﾞ"

        pairs = analyzer._get_pairs(text)

        assert [s for s, _ in pairs] == ["abc", "ﾞｶﾞ"]

    def test_real_winrt_halfwidth_dakuten(self):
        """真实 WinRT API 端到端：半角浊点假名的 surface 完整保留。"""
        pytest.importorskip("winrt.windows.globalization")
        try:
            from winrt._winrt import init_apartment, STA

            init_apartment(STA)
        except OSError:
            pytest.skip("WinRT STA apartment 激活失败")
        from winrt.windows.globalization import JapanesePhoneticAnalyzer

        analyzer = WinRTAnalyzer.__new__(WinRTAnalyzer)
        analyzer._jpa = JapanesePhoneticAnalyzer
        analyzer._pykakasi_conv = None

        text = "ｶﾞのﾃｽﾄ"
        pairs = analyzer._get_pairs(text)

        assert "".join(s for s, _ in pairs) == text
        assert pairs[0][0] == "ｶﾞ", (
            f"半角浊点假名应完整保留在首个 surface，实际 {pairs[0][0]!r}"
        )
        assert pairs[0][1] == "が"
