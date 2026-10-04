"""中文拼音分析器测试。

G3 回归：pypinyin 按条目返回（每汉字一条、连续非汉字合并一条），
条目数与字符数不等——按字符下标直接索引会把拼音挂错字（如
"ab中文字" 的 中→wén）。
"""

import pytest

from strange_uta_game.backend.infrastructure.parsers.ruby_analyzer import (
    PinyinAnalyzer,
)


@pytest.fixture(scope="module")
def analyzer():
    return PinyinAnalyzer()


def _reading_of(results, ch):
    return next(r for r in results if r.text == ch)


class TestPinyinAnalyzerAlignment:
    def test_pure_han_chars_get_word_context_pinyin(self, analyzer):
        results = analyzer.analyze("中文")
        assert [r.text for r in results] == ["中", "文"]
        assert _reading_of(results, "中").reading == "zhōng"
        assert _reading_of(results, "文").reading == "wén"

    def test_english_prefix_does_not_shift_han_readings(self, analyzer):
        """英文前缀后汉字拼音不得错位（此前 中→wén 错配）。"""
        results = analyzer.analyze("ab中文字")
        assert [r.text for r in results] == list("ab中文字")
        assert _reading_of(results, "中").reading == "zhōng"
        assert _reading_of(results, "文").reading == "wén"
        assert _reading_of(results, "字").reading == "zì"
        # 非汉字字符 reading 回退为原字符
        assert _reading_of(results, "a").reading == "a"
        assert _reading_of(results, "b").reading == "b"

    def test_digits_do_not_shift_han_readings(self, analyzer):
        results = analyzer.analyze("123歌")
        assert _reading_of(results, "歌").reading == "gē"

    def test_trailing_english_after_han(self, analyzer):
        results = analyzer.analyze("中文ab")
        assert _reading_of(results, "中").reading == "zhōng"
        assert _reading_of(results, "文").reading == "wén"
        assert _reading_of(results, "a").reading == "a"
        assert _reading_of(results, "b").reading == "b"

    def test_indices_cover_full_text(self, analyzer):
        text = "A4纸上ab的中文字"
        results = analyzer.analyze(text)
        assert [(r.start_idx, r.end_idx) for r in results] == [
            (i, i + 1) for i in range(len(text))
        ]
        # 每个汉字都拿到独立拼音（不允许多个汉字共享同一条）
        han_readings = [
            r.reading
            for r in results
            if PinyinAnalyzer._is_chinese_char(r.text)
        ]
        assert all(han_readings), "汉字必须拿到拼音"
        assert len(set(han_readings)) == len(han_readings), (
            f"汉字拼音出现重复，疑似错位：{han_readings}"
        )

    def test_punctuation_only_line(self, analyzer):
        results = analyzer.analyze("！？")
        assert len(results) == 2
        assert all(r.reading == r.text for r in results)

    def test_empty_text(self, analyzer):
        assert analyzer.analyze("") == []

    def test_get_reading_joins_han_pinyin(self, analyzer):
        assert "zhōng" in analyzer.get_reading("中文")
