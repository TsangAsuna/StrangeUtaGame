"""中文歌词模式逐句注音管线测试。

G4 回归：中文模式下 _analyzer 恒 None，analyze_and_apply_sentence_pipeline
此前不分流，逐句注音/拼音注音按钮每次必崩
'NoneType' object has no attribute 'analyze'。
"""

import pytest

from strange_uta_game.backend.application.auto_check_service import AutoCheckService
from strange_uta_game.backend.domain import Sentence


def _sentence(text="夜空中最亮的星"):
    return Sentence.from_text(text, "s1")


class TestChineseModeSentencePipeline:
    def test_sentence_pipeline_does_not_crash(self):
        """中文模式句级管线必须分流到按字符流路径（G4）。"""
        service = AutoCheckService(chinese_mode=True)
        sentence = _sentence()

        service.analyze_and_apply_sentence_pipeline(sentence)

        # 按字符流重建：字符一一对应，每字至少 1 cp
        chars = sentence.characters
        assert [c.char for c in chars] == list("夜空中最亮的星")
        assert all(c.check_count >= 1 for c in chars)

    def test_sentence_pipeline_keeps_existing_timestamps(self):
        service = AutoCheckService(chinese_mode=True)
        sentence = _sentence()
        sentence.characters[0].timestamps = [1000, 2000]

        service.analyze_and_apply_sentence_pipeline(sentence)

        assert sentence.characters[0].timestamps == [1000, 2000]

    def test_sentence_pipeline_with_pinyin_analyzer_annotates(self):
        from strange_uta_game.backend.infrastructure.parsers.ruby_analyzer import (
            PinyinAnalyzer,
        )

        service = AutoCheckService(
            chinese_mode=True, pinyin_analyzer=PinyinAnalyzer()
        )
        sentence = _sentence("中文")

        service.analyze_and_apply_sentence_pipeline(sentence)

        chars = sentence.characters
        assert [c.char for c in chars] == ["中", "文"]
        readings = [c.ruby.parts[0].text if c.ruby else "" for c in chars]
        assert readings == ["zhōng", "wén"]

    def test_sentence_pipeline_without_checkpoint_update(self):
        """update_checkpoints=False：节奏点完全不动。"""
        service = AutoCheckService(chinese_mode=True)
        sentence = _sentence()
        for c in sentence.characters:
            c.check_count = 1
            c.set_check_count(1, force=True)
        before = [c.check_count for c in sentence.characters]

        service.analyze_and_apply_sentence_pipeline(
            sentence, update_checkpoints=False
        )

        assert [c.check_count for c in sentence.characters] == before

    def test_analyze_sentence_raises_chinese_error_in_chinese_mode(self):
        """直接调用句级分析入口：给出中文提示而非 NoneType 崩溃。"""
        service = AutoCheckService(chinese_mode=True)
        sentence = _sentence()

        with pytest.raises(RuntimeError, match="中文歌词模式"):
            service.analyze_sentence(sentence)

    def test_analyze_project_raises_chinese_error_in_chinese_mode(self):
        service = AutoCheckService(chinese_mode=True)
        from strange_uta_game.backend.domain import Project

        project = Project()
        project.sentences = [_sentence()]

        with pytest.raises(RuntimeError, match="中文歌词模式"):
            service.analyze_project(project)

    def test_japanese_mode_pipeline_unaffected(self):
        """日文模式回归：句级管线照常走分析器路径。"""
        from strange_uta_game.backend.infrastructure.parsers.ruby_analyzer import (
            DummyAnalyzer,
        )

        service = AutoCheckService(DummyAnalyzer(), chinese_mode=False)
        sentence = Sentence.from_text("あか", "s1")

        service.analyze_and_apply_sentence_pipeline(sentence)

        assert [c.char for c in sentence.characters] == ["あ", "か"]
