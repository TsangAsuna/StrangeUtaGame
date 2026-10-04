"""文本拆分器测试。"""

from strange_uta_game.backend.infrastructure.parsers.text_splitter import (
    AutoSplitter,
    CharType,
    EnglishSplitter,
    JapaneseSplitter,
    SplitConfig,
    get_char_type,
    split_text,
)


class TestGetCharType:
    """测试字符类型识别"""

    def test_kanji(self):
        assert get_char_type("赤") == CharType.KANJI
        assert get_char_type("日") == CharType.KANJI
        assert get_char_type("本") == CharType.KANJI

    def test_hiragana(self):
        assert get_char_type("あ") == CharType.HIRAGANA
        assert get_char_type("い") == CharType.HIRAGANA

    def test_katakana(self):
        assert get_char_type("ア") == CharType.KATAKANA
        assert get_char_type("イ") == CharType.KATAKANA

    def test_long_vowel(self):
        assert get_char_type("ー") == CharType.LONG_VOWEL

    def test_katakana_block_separators_are_symbols(self):
        # 「・」U+30FB、「゠」U+30A0 落在片假名 Unicode 块内但不表音，
        # 必须归为符号，否则补全时间戳会把 "シンフォニック・ラブ" 的中点也补轴。
        assert get_char_type("・") == CharType.SYMBOL
        assert get_char_type("゠") == CharType.SYMBOL

    def test_katakana_iteration_marks_stay_katakana(self):
        # 片假名迭字「ヽヾ」表音，仍按片假名处理（不被上面的符号特判误吞）。
        assert get_char_type("ヽ") == CharType.KATAKANA
        assert get_char_type("ヾ") == CharType.KATAKANA

    def test_sokuon(self):
        assert get_char_type("っ") == CharType.SOKUON
        assert get_char_type("ッ") == CharType.SOKUON

    def test_alphabet(self):
        assert get_char_type("A") == CharType.ALPHABET
        assert get_char_type("z") == CharType.ALPHABET

    def test_hangul(self):
        # 谚文音节块（U+AC00–U+D7A3）在 isalpha() 兜底之前拦截，
        # 不得误归英文字母。
        assert get_char_type("가") == CharType.HANGUL
        assert get_char_type("한") == CharType.HANGUL
        assert get_char_type("글") == CharType.HANGUL
        assert get_char_type("힣") == CharType.HANGUL

    def test_hangul_block_edges(self):
        # 音节块前一/后一码位不属于谚文音节块（未指派 → OTHER）
        assert get_char_type("\ua9ff") == CharType.OTHER
        assert get_char_type("\ud7a4") == CharType.OTHER

    def test_number(self):
        assert get_char_type("1") == CharType.NUMBER

    def test_full_width_space_has_distinct_type(self):
        assert get_char_type(" ") == CharType.SPACE
        assert get_char_type("\u3000") == CharType.FULL_SPACE

    def test_decorative_and_operator_symbols(self):
        # 装饰符号（♪♥★）与运算符（+ - * & ^ = ~ |）均归为符号；
        # 注音删除分类与节奏点生成无关，装饰符号一并算符号。
        for c in "♪♫♩♬♡♥★☆+-*&^=~|<>《》〈〉—·•":
            assert get_char_type(c) == CharType.SYMBOL, c


class TestJapaneseSplitter:
    """测试日文拆分器"""

    def test_split_simple_japanese(self):
        splitter = JapaneseSplitter()
        result = splitter.split("赤い花")
        assert result == ["赤", "い", "花"]

    def test_split_with_long_vowel(self):
        splitter = JapaneseSplitter(split_long_vowel=True)
        result = splitter.split("さよーなら")
        assert "ー" in result

    def test_split_with_sokuon(self):
        splitter = JapaneseSplitter(split_sokuon=True)
        result = splitter.split("こっち")
        assert "っ" in result

    def test_merge_spaces(self):
        splitter = JapaneseSplitter(merge_spaces=True)
        result = splitter.split("赤い  花")  # 两个空格
        assert result == ["赤", "い", " ", "花"]

    def test_merge_spaces_preserves_full_width_space(self):
        splitter = JapaneseSplitter(merge_spaces=True)

        assert splitter.split("赤い\u3000花") == ["赤", "い", "\u3000", "花"]
        assert splitter.split("赤い \u3000 花") == ["赤", "い", "\u3000", "花"]


class TestEnglishSplitter:
    """测试英文拆分器"""

    def test_split_simple_english(self):
        splitter = EnglishSplitter()
        result = splitter.split("Hello")
        assert result == ["H", "e", "l", "l", "o"]

    def test_merge_spaces(self):
        splitter = EnglishSplitter(merge_spaces=True)
        result = splitter.split("Hello  World")
        assert "  " not in result
        assert " " in result

    def test_merge_spaces_preserves_full_width_space(self):
        splitter = EnglishSplitter(merge_spaces=True)

        assert splitter.split("Hello \u3000 World") == [*"Hello", "\u3000", *"World"]


class TestAutoSplitter:
    """测试自动拆分器"""

    def test_detect_japanese(self):
        splitter = AutoSplitter()
        lang = splitter.detect_language("赤い花")
        assert lang == "ja"

    def test_detect_english(self):
        splitter = AutoSplitter()
        lang = splitter.detect_language("Hello World")
        assert lang == "en"

    def test_split_japanese(self):
        splitter = AutoSplitter()
        result = splitter.split("赤い花")
        assert result == ["赤", "い", "花"]

    def test_split_english(self):
        splitter = AutoSplitter()
        result = splitter.split("Hello")
        assert result == ["H", "e", "l", "l", "o"]

    def test_detect_hangul_counts_as_en_style(self):
        # 纯谚文行走 en 桶 → EnglishSplitter（逐字 + 空格合并），
        # 不退化为 other 的裸 list(text)（连续空格不再合并）。
        splitter = AutoSplitter()
        assert splitter.detect_language("사랑해 너를") == "en"
        assert splitter.split("사랑해  너를") == [
            "사", "랑", "해", " ", "너", "를",
        ]


class TestSplitText:
    """测试 split_text 函数"""

    def test_split_with_check_count(self):
        config = SplitConfig()
        chars, counts = split_text("赤い花", config)
        assert len(chars) == len(counts)
        assert chars == ["赤", "い", "花"]

    def test_mixed_consecutive_spaces_merge_to_one_full_width_space(self):
        chars, counts = split_text("magic magic! \u3000君に届くよぅに")

        assert "".join(chars) == "magic magic!\u3000君に届くよぅに"
        assert len(counts) == len(chars)

    def test_hangul_syllables_get_one_checkpoint_each(self):
        # 谚文每音节固定 1 个节奏点（不受 alphabet 开关门控）
        chars, counts = split_text("사랑해")
        assert chars == ["사", "랑", "해"]
        assert counts == [1, 1, 1]


class TestHalfWidthKatakanaE11:
    """E11：半角片假名（U+FF66–FF9F）归 KATAKANA，不再被 isalpha 兜底
    归 ALPHABET；U+FF61–FF65 半角标点仍归 SYMBOL。"""

    def test_halfwidth_katakana_is_katakana(self):
        for ch in "ｦｱｲｳｴｵｶｷｸﾝﾞﾟ":
            assert get_char_type(ch) == CharType.KATAKANA, ch

    def test_halfwidth_long_vowel_still_long_vowel(self):
        assert get_char_type("ｰ") == CharType.LONG_VOWEL

    def test_halfwidth_punctuation_stays_symbol(self):
        for ch in "｡｢｣､･":
            assert get_char_type(ch) == CharType.SYMBOL, ch

    def test_halfwidth_katakana_counts_as_japanese(self):
        splitter = AutoSplitter()
        assert splitter.detect_language("ﾃｽﾄ") == "ja"
