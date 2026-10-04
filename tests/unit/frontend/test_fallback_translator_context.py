"""_FallbackTranslator 的 context 语义单测（I10）。

守护：
- 同源串不同上下文有不同译法：context 精确命中的译文优先
- context 不匹配且译文不唯一 → 返回源串（宁缺毋错，不给错译文）
- context 不匹配但译文唯一 → 兜底唯一译文
- unfinished / 空译文条目不进入回退表；未知源串返回源串
"""

from __future__ import annotations

from pathlib import Path

from strange_uta_game.frontend.localization import manager as manager_module
from strange_uta_game.frontend.localization.manager import (
    Language,
    _FallbackTranslator,
)

_TS = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE TS>
<TS version="2.1">
<context>
    <name>DialogA</name>
    <message>
        <source>保存</source>
        <translation>セーブ</translation>
    </message>
</context>
<context>
    <name>DialogB</name>
    <message>
        <source>保存</source>
        <translation>保存する</translation>
    </message>
    <message>
        <source>取消</source>
        <translation>キャンセル</translation>
    </message>
</context>
<context>
    <name>DialogC</name>
    <message>
        <source>删除</source>
        <translation type="unfinished"></translation>
    </message>
</context>
</TS>
"""


def _make_translator(tmp_path: Path, monkeypatch) -> _FallbackTranslator:
    ts = tmp_path / "app.ja_JP.ts"
    ts.write_text(_TS, encoding="utf-8")
    monkeypatch.setattr(manager_module, "_translations_dir", lambda: tmp_path)
    tr = _FallbackTranslator()
    assert tr.load_from_ts(Language(code="ja_JP", native_name="日本語", qlocale_name="ja_JP"))
    return tr


class TestFallbackTranslatorContext:
    def test_exact_context_match_wins(self, tmp_path, monkeypatch):
        tr = _make_translator(tmp_path, monkeypatch)
        assert tr.translate("DialogB", "保存") == "保存する"
        assert tr.translate("DialogA", "保存") == "セーブ"

    def test_unique_translation_used_as_fallback(self, tmp_path, monkeypatch):
        # "取消" 只有 DialogB 一条译文：借别的上下文查询时兜底唯一译文
        tr = _make_translator(tmp_path, monkeypatch)
        assert tr.translate("DialogA", "取消") == "キャンセル"

    def test_ambiguous_returns_source_when_context_mismatch(self, tmp_path, monkeypatch):
        # "保存" 在两个上下文有不同译法：context 不匹配时选哪个都可能错 → 源串
        tr = _make_translator(tmp_path, monkeypatch)
        assert tr.translate("OtherContext", "保存") == "保存"

    def test_unfinished_entry_skipped(self, tmp_path, monkeypatch):
        tr = _make_translator(tmp_path, monkeypatch)
        assert tr.translate("DialogC", "删除") == "删除"

    def test_unknown_source_returns_source(self, tmp_path, monkeypatch):
        tr = _make_translator(tmp_path, monkeypatch)
        assert tr.translate("DialogA", "不存在的串") == "不存在的串"

    def test_empty_translation_skipped(self, tmp_path, monkeypatch):
        ts = tmp_path / "app.ja_JP.ts"
        ts.write_text(
            _TS.replace("<translation>キャンセル</translation>", "<translation></translation>"),
            encoding="utf-8",
        )
        monkeypatch.setattr(manager_module, "_translations_dir", lambda: tmp_path)
        tr = _FallbackTranslator()
        tr.load_from_ts(Language(code="ja_JP", native_name="日本語", qlocale_name="ja_JP"))
        # "取消" 的唯一译文为空被剔除 → 表里无该串 → 返回源串
        assert tr.translate("DialogB", "取消") == "取消"
