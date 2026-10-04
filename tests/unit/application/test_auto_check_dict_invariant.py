"""Phase 5 用户词典覆盖的节奏点/时间戳不变式测试。

G11 回归：词典覆盖此前直接赋值 check_count，绕过权威 setter 的
len(timestamps) <= check_count 不变式——与已有时间戳共存时，后续 pass
反复重对齐导致 ruby 分段来回摆。
"""

import pytest

from strange_uta_game.backend.application.auto_check_service import AutoCheckService
from strange_uta_game.backend.domain import Sentence
from strange_uta_game.backend.infrastructure.parsers.ruby_analyzer import (
    DummyAnalyzer,
)


def _service(reading: str) -> AutoCheckService:
    return AutoCheckService(
        DummyAnalyzer(),
        user_dictionary=[{"enabled": True, "word": "光", "reading": reading}],
    )


def _prepared_sentence(timestamps) -> Sentence:
    """「光」单字句：预置旧时间戳（apply 时按位保留到新字符上）。"""
    sentence = Sentence.from_text("光", "s1")
    sentence.characters[0].timestamps = list(timestamps)
    return sentence


class TestDictOverrideCheckCountInvariant:
    def test_override_truncates_timestamps_beyond_segments(self):
        """旧时间戳多于词典段数：超出部分按明确策略截断。"""
        service = _service("{光||ひ|か}")  # 2 段
        sentence = _prepared_sentence([100, 200, 300])

        service.apply_to_sentence(sentence)

        ch = sentence.characters[0]
        assert [p.text for p in ch.ruby.parts] == ["ひ", "か"]
        assert ch.check_count == 2
        # 不变式：len(timestamps) <= check_count（旧实现残留 3 个）
        assert len(ch.timestamps) <= ch.check_count
        assert ch.timestamps == [100, 200]

    def test_override_grow_keeps_shorter_timestamps(self):
        """旧时间戳少于词典段数：原样保留（放大方向本就合法）。"""
        service = _service("{光||ひ|か|り}")  # 3 段
        sentence = _prepared_sentence([100])

        service.apply_to_sentence(sentence)

        ch = sentence.characters[0]
        assert ch.check_count == 3
        assert len(ch.ruby.parts) == 3
        assert ch.timestamps == [100]

    def test_override_ruby_parts_match_check_count(self):
        """权威 setter 收口：离开覆盖点时 len(ruby.parts) == check_count。"""
        service = _service("{光||ひ|か}")
        sentence = _prepared_sentence([100, 200, 300])

        service.apply_to_sentence(sentence)

        ch = sentence.characters[0]
        assert len(ch.ruby.parts) == ch.check_count

    def test_repeated_override_does_not_swing(self):
        """连续两次 Phase 5：分段与 cc 稳定（不再来回摆）。"""
        service = _service("{光||ひ|か}")
        sentence = _prepared_sentence([100, 200, 300])
        service.apply_to_sentence(sentence)
        first = (
            sentence.characters[0].check_count,
            [p.text for p in sentence.characters[0].ruby.parts],
            list(sentence.characters[0].timestamps),
        )

        service.apply_to_sentence(sentence)
        second = (
            sentence.characters[0].check_count,
            [p.text for p in sentence.characters[0].ruby.parts],
            list(sentence.characters[0].timestamps),
        )
        assert first == second
