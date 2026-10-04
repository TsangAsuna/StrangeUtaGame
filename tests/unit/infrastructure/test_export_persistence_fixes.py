"""审查缺陷修复回归测试（导出器 + 持久化 + 网络词典）。

覆盖：
- F2  SRT 对唱重叠行负时长钳制 + 末行音频时长收尾
- F3  NicokaraWithRuby @Ruby 演唱者 per-char 过滤（不泄漏被过滤角色注音）
- F5  NicokaraWithRuby 对未知演唱者 ID 与基础版同口径（归一化为默认演唱者）
- F6  txt2ass 未打轴行不伪造 [00:00.00]、空行输出空行
- F7  损坏旧版 .sug 迁移抛 SugParseError 而非裸异常
- F8  @Ruby 负相对时间戳不再钳成 [00:00:00]
- F10 ASS 导出保留连词组尾部字符的注音
- F12 网络词典 URL 自带 query 时用 & 续接；内置源走 https
- F13 证书验证降级重试写入诊断信息且 UI 消息可见
- F14 带 BOM 的 .sug 可加载
- F16 v1 迁移缺失 cp_idx 不再填 0；lrc [ti:] 值转义 ]
- I1(持久化) .sug 写盘原子化（temp + os.replace，失败不留截断文件）
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import tempfile
import urllib.error
import urllib.request
from typing import Any, List

import pytest

from strange_uta_game.backend.domain import (
    Project,
    Sentence,
    Singer,
    Ruby,
    RubyPart,
)
from strange_uta_game.backend.infrastructure.exporters.srt_exporter import (
    SRTExporter,
)
from strange_uta_game.backend.infrastructure.exporters.txt2ass_exporter import (
    Txt2AssExporter,
    ASSDirectExporter,
)
from strange_uta_game.backend.infrastructure.exporters.lrc_exporter import (
    LRCExporter,
)
from strange_uta_game.backend.infrastructure.exporters import (
    NicokaraExporter,
    NicokaraWithRubyExporter,
)
from strange_uta_game.backend.infrastructure import network_dictionary as nd
from strange_uta_game.backend.infrastructure.persistence import sug_io as sug_io_mod
from strange_uta_game.backend.infrastructure.persistence.sug_io import (
    SugProjectParser,
    SugParseError,
)
from strange_uta_game.backend.infrastructure.parsers.ass_parser import ASSParser
from strange_uta_game.backend.infrastructure.parsers.lyric_parser import (
    parse_to_sentences,
)


def _export_to(tmpdir: str, suffix: str, exporter, project: Project, **kwargs) -> str:
    """导出到临时文件并返回文本内容。"""
    path = os.path.join(tmpdir, f"out{suffix}")
    exporter.export(project, path, **kwargs)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ──────────────────────────────────────────────
# F2 SRT
# ──────────────────────────────────────────────


class TestSRTOverlapClamp:
    def _make_overlap_project(self) -> Project:
        """B 行先于 A 行结束开始（对唱重叠），末行后音频继续 10s。"""
        project = Project()
        singer = project.get_default_singer()

        a = Sentence.from_text("あ", singer.id)
        a.characters[0].add_timestamp(10000)
        project.add_sentence(a)

        b = Sentence.from_text("い", singer.id)
        b.characters[0].add_timestamp(5000)  # 早于 A 行开始 → 旧逻辑导出负时长
        project.add_sentence(b)

        c = Sentence.from_text("う", singer.id)
        c.characters[0].add_timestamp(20000)
        project.add_sentence(c)

        project.audio_duration_ms = 30000
        return project

    def test_overlap_line_end_clamped(self, tmp_path):
        """A 行 End 不得早于本行 Start + 最小显示时长（旧逻辑 05:00 < 10:00）。"""
        content = _export_to(str(tmp_path), ".srt", SRTExporter(), self._make_overlap_project())
        assert "00:00:10,000 --> 00:00:15,000" in content, content
        # B 行 End = 下一行 Start，未触发钳制
        assert "00:00:05,000 --> 00:00:20,000" in content, content

    def test_last_line_ends_at_audio_duration(self, tmp_path):
        """末行用 audio_duration_ms 收尾（旧逻辑 Start + 5s = 25s）。"""
        content = _export_to(str(tmp_path), ".srt", SRTExporter(), self._make_overlap_project())
        assert "00:00:20,000 --> 00:00:30,000" in content, content

    def test_last_line_without_audio_falls_back(self, tmp_path):
        """audio_duration_ms 未知（0）时退回 Start + 5s（旧行为）。"""
        project = self._make_overlap_project()
        project.audio_duration_ms = 0
        content = _export_to(str(tmp_path), ".srt", SRTExporter(), project)
        assert "00:00:20,000 --> 00:00:25,000" in content, content


# ──────────────────────────────────────────────
# F3 / F5 NicokaraWithRuby 演唱者过滤
# ──────────────────────────────────────────────


class TestNicokaraRubySingerFilter:
    def _make_duet_project(self) -> Project:
        """行级 A：赤いの 属 A，青 属 B（per-char singer 覆盖）。"""
        project = Project()
        singer_a = project.singers[0]
        singer_a.name = "A"
        singer_b = Singer(name="B", color="#00FF00")
        project.add_singer(singer_b)

        sent = Sentence.from_text("赤いの青", singer_a.id)
        sent.characters[0].set_ruby(Ruby(parts=[RubyPart(text="あか")]))
        sent.characters[0].add_timestamp(1000)
        sent.characters[1].add_timestamp(1500)
        sent.characters[2].add_timestamp(2000)
        sent.characters[3].set_ruby(Ruby(parts=[RubyPart(text="あお")]))
        sent.characters[3].add_timestamp(2500)
        sent.characters[3].singer_id = singer_b.id
        project.add_sentence(sent)
        return project, singer_a, singer_b

    def test_ruby_entries_filtered_per_char(self):
        """singer_ids={A} 时 @Ruby 不得包含 B 角色的注音（旧逻辑泄漏「青,あお」）。"""
        project, singer_a, singer_b = self._make_duet_project()
        exporter = NicokaraWithRubyExporter()
        entries = exporter._collect_ruby_entries(project, {singer_a.id}, {s.id for s in project.singers})

        assert entries == ["赤,あか,[00:01:00],[00:01:50]"], entries
        assert not any("あお" in e or "青" in e for e in entries)

    def test_ruby_export_file_filtered_per_char(self, tmp_path):
        """整文件导出（含 @Ruby 段）同样不泄漏 B 的注音。"""
        project, singer_a, _ = self._make_duet_project()
        content = _export_to(
            str(tmp_path), ".lrc", NicokaraWithRubyExporter(), project,
            singer_ids={singer_a.id}, tag_data={},
        )
        ruby_lines = [l for l in content.splitlines() if l.startswith("@Ruby")]
        assert ruby_lines == ["@Ruby1=赤,あか,[00:01:00],[00:01:50]"], ruby_lines

    def test_mixed_compound_drops_filtered_chars(self):
        """连词组内混入被过滤角色：其字与注音都不进入 @Ruby 段。"""
        project = Project()
        singer_a = project.singers[0]
        singer_b = Singer(name="B", color="#00FF00")
        project.add_singer(singer_b)

        sent = Sentence.from_text("漢字", singer_a.id)
        sent.characters[0].set_ruby(Ruby(parts=[RubyPart(text="かん")]))
        sent.characters[0].add_timestamp(1000)
        sent.characters[0].linked_to_next = True
        sent.characters[1].set_ruby(Ruby(parts=[RubyPart(text="じ")]))
        sent.characters[1].singer_id = singer_b.id  # 尾字属 B，无时间戳
        project.add_sentence(sent)

        exporter = NicokaraWithRubyExporter()
        entries = exporter._collect_ruby_entries(project, {singer_a.id}, {s.id for s in project.singers})
        assert entries == ["漢,かん,[00:01:00]"], entries

    def test_unknown_singer_id_normalized_like_base(self, tmp_path):
        """F5：char 的演唱者 ID 不在项目中时，WithRuby 与基础版同口径
        归一化为默认演唱者——字符不被过滤，正文与 @Ruby 都输出。"""
        project = Project()
        singer = project.singers[0]
        sent = Sentence.from_text("赤い", singer.id)
        sent.characters[0].set_ruby(Ruby(parts=[RubyPart(text="あか")]))
        sent.characters[0].add_timestamp(1000)
        sent.characters[0].singer_id = "ghost-singer-id"  # 项目中已不存在
        sent.characters[1].add_timestamp(1500)
        project.add_sentence(sent)

        content = _export_to(
            str(tmp_path), ".lrc", NicokaraWithRubyExporter(), project,
            singer_ids={singer.id}, tag_data={},
        )
        # 旧逻辑：ghost 不在 singer_ids 且无归一化 → 该字被过滤，正文丢「赤」
        assert "[00:01:00]赤[00:01:50]い" in content, content
        assert re.search(r"@Ruby\d+=赤,あか", content), content

        # 基础版行为（对照）：同样输出
        base_content = _export_to(
            str(tmp_path), ".lrc", NicokaraExporter(), project,
            singer_ids={singer.id},
        )
        assert "[00:01:00]赤[00:01:50]い" in base_content, base_content


# ──────────────────────────────────────────────
# F6 txt2ass
# ──────────────────────────────────────────────


class TestTxt2AssUntimedLines:
    def test_untimed_and_blank_lines(self, tmp_path):
        """未打轴行输出纯文本、空白行输出空行，均不伪造 [00:00.00]。"""
        project = Project()
        singer = project.singers[0]
        project.add_sentence(Sentence.from_text("未打轴", singer.id))
        project.add_sentence(Sentence(singer_id=singer.id, characters=[]))  # 空行
        timed = Sentence.from_text("测试歌词", singer.id)
        timed.characters[0].add_timestamp(12345)
        project.add_sentence(timed)

        content = _export_to(str(tmp_path), ".txt", Txt2AssExporter(), project)
        lines = content.splitlines()
        assert "[00:00.00]" not in content, content
        assert lines[-3:] == ["未打轴", "", "[00:12.34]测试歌词"], lines

    def test_untimed_whitespace_line_exports_empty(self, tmp_path):
        """纯空白未打轴行输出真正的空行，而不是原样空白。"""
        project = Project()
        singer = project.singers[0]
        project.add_sentence(Sentence.from_text("テスト", singer.id))
        project.add_sentence(Sentence.from_text("　", singer.id))  # 全角空格行
        content = _export_to(str(tmp_path), ".txt", Txt2AssExporter(), project)
        lines = content.splitlines()
        assert lines[-1] == "テスト", lines
        assert lines[-2] == "", lines


# ──────────────────────────────────────────────
# F10 ASS 连词尾部注音
# ──────────────────────────────────────────────


class TestAssCompoundTailRuby:
    def _text_of(self, sent: Sentence) -> str:
        exporter = ASSDirectExporter()
        for ch in sent.characters:
            ch.set_offset(0)
        line_start_ms = sent.global_timing_start_ms
        line_end_ms = exporter._compute_line_end_ms(sent)
        return exporter._generate_karaoke_text(sent, line_start_ms, line_end_ms)

    def test_single_part_anchor_tail_ruby(self):
        """漢(ts,かん) + 字(无 ts,じ) → 漢字|<かんじ，旧逻辑丢「じ」。"""
        project = Project()
        singer = project.singers[0]
        sent = Sentence.from_text("漢字", singer.id)
        sent.characters[0].set_ruby(Ruby(parts=[RubyPart(text="かん")]))
        sent.characters[0].add_timestamp(1000)
        sent.characters[0].linked_to_next = True
        sent.characters[1].check_count = 0
        sent.characters[1].set_ruby(Ruby(parts=[RubyPart(text="じ")]))
        project.add_sentence(sent)

        text = self._text_of(sent)
        assert text == "{\\k0}{\\k50}漢字|<かんじ{\\k0}", text

    def test_multi_part_anchor_tail_ruby_after_last_part(self):
        """届(ts×2,と/ど) + 字(无 ts,じ) → 尾字注音挂在锚字最后一个 part 段，
        读音顺序与 Nicokara 整组拼接一致（と→ど→じ）。"""
        project = Project()
        singer = project.singers[0]
        sent = Sentence.from_text("届字", singer.id)
        ch0 = sent.characters[0]
        ch0.check_count = 2
        ch0.set_ruby(Ruby(parts=[RubyPart(text="と"), RubyPart(text="ど")]))
        ch0.add_timestamp(1000, checkpoint_idx=0)
        ch0.add_timestamp(1500, checkpoint_idx=1)
        ch0.linked_to_next = True
        sent.characters[1].check_count = 0
        sent.characters[1].set_ruby(Ruby(parts=[RubyPart(text="じ")]))
        project.add_sentence(sent)

        text = self._text_of(sent)
        assert text == "{\\k0}{\\k50}届字|<と{\\k50}#|どじ{\\k0}", text

    def test_anchor_without_ruby_uses_tail_ruby(self):
        """锚字无 ruby、尾字有注音时，以尾字注音作为首段 ruby。"""
        project = Project()
        singer = project.singers[0]
        sent = Sentence.from_text("漢字", singer.id)
        sent.characters[0].add_timestamp(1000)
        sent.characters[0].linked_to_next = True
        sent.characters[1].check_count = 0
        sent.characters[1].set_ruby(Ruby(parts=[RubyPart(text="じ")]))
        project.add_sentence(sent)

        text = self._text_of(sent)
        assert text == "{\\k0}{\\k50}漢字|<じ{\\k0}", text

    def test_tail_ruby_roundtrip(self, tmp_path):
        """导出 → ASS 解析：合并后的注音能读回（挂在连词首字、链式连词）。"""
        project = Project()
        singer = project.singers[0]
        sent = Sentence.from_text("漢字", singer.id)
        sent.characters[0].set_ruby(Ruby(parts=[RubyPart(text="かん")]))
        sent.characters[0].add_timestamp(1000)
        sent.characters[0].linked_to_next = True
        sent.characters[1].check_count = 0
        sent.characters[1].set_ruby(Ruby(parts=[RubyPart(text="じ")]))
        project.add_sentence(sent)

        path = os.path.join(str(tmp_path), "r.ass")
        ASSDirectExporter().export(project, path)
        with open(path, "r", encoding="utf-8") as f:
            ass_content = f.read()

        parsed_lines = ASSParser().parse(ass_content)
        sentences = parse_to_sentences(parsed_lines, singer.id)
        assert sentences, "ASS 应至少解析出一行"
        chars = sentences[0].characters
        assert [c.char for c in chars] == ["漢", "字"]
        assert [p.text for p in chars[0].ruby.parts] == ["かんじ"]
        assert chars[0].linked_to_next is True
        assert chars[1].linked_to_next is False


# ──────────────────────────────────────────────
# F7 / F14 / F16(cp_idx) / I1 持久化
# ──────────────────────────────────────────────


class TestSugIoFixes:
    def test_corrupt_v1_migrate_raises_sug_parse_error(self, tmp_path):
        """F7：损坏的 v1 数据在迁移中途抛 AttributeError → 转 SugParseError。"""
        file_path = tmp_path / "corrupt_v1.sug"
        file_path.write_text(
            json.dumps({"version": "1.0", "lines": ["not-a-dict"]}),
            encoding="utf-8",
        )
        with pytest.raises(SugParseError) as excinfo:
            SugProjectParser.load(str(file_path))
        assert isinstance(excinfo.value.__cause__, AttributeError)

    def test_non_dict_root_raises_sug_parse_error(self, tmp_path):
        """F7：JSON 根不是对象（如数组）也统一转 SugParseError。"""
        file_path = tmp_path / "array.sug"
        file_path.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(SugParseError):
            SugProjectParser.load(str(file_path))

    def test_load_sug_with_bom(self, tmp_path):
        """F14：带 UTF-8 BOM 的 .sug 可正常加载。"""
        project = Project()
        singer = project.get_default_singer()
        sent = Sentence.from_text("テスト", singer.id)
        sent.characters[0].add_timestamp(1000)
        project.add_sentence(sent)

        file_path = tmp_path / "bom.sug"
        SugProjectParser.save(project, str(file_path))
        raw = file_path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
        file_path.write_bytes(b"\xef\xbb\xbf" + raw)

        loaded = SugProjectParser.load(str(file_path))
        assert loaded.sentences[0].text == "テスト"

    def test_v1_migration_sparse_cp_idx_not_zero_filled(self, tmp_path):
        """F16：v1 timetag 缺失 cp_idx 不再填 0（伪造 0ms 打轴），
        按 v2 语义在首个缺口处截断；连续前缀不受影响。"""
        doc = {
            "version": "1.0",
            "id": "p1",
            "audio_duration_ms": 0,
            "singers": [{"id": "s1", "name": "S"}],
            "lines": [
                {
                    "id": "l1",
                    "singer_id": "s1",
                    "text": "あい",
                    "chars": ["あ", "い"],
                    "checkpoints": [
                        {"char_idx": 0, "check_count": 3},
                        {"char_idx": 1, "check_count": 1},
                    ],
                    "timetags": [
                        {"char_idx": 0, "checkpoint_idx": 0, "timestamp_ms": 1000},
                        # checkpoint_idx 1 缺失
                        {"char_idx": 0, "checkpoint_idx": 2, "timestamp_ms": 2000},
                        {"char_idx": 1, "checkpoint_idx": 0, "timestamp_ms": 3000},
                    ],
                    "rubies": [],
                },
                {
                    "id": "l2",
                    "singer_id": "s1",
                    "text": "かき",
                    "chars": ["か", "き"],
                    "checkpoints": [
                        {"char_idx": 0, "check_count": 2},
                        {"char_idx": 1, "check_count": 1},
                    ],
                    "timetags": [
                        {"char_idx": 0, "checkpoint_idx": 0, "timestamp_ms": 4000},
                        {"char_idx": 0, "checkpoint_idx": 1, "timestamp_ms": 4500},
                        {"char_idx": 1, "checkpoint_idx": 0, "timestamp_ms": 5000},
                    ],
                    "rubies": [],
                },
            ],
        }
        file_path = tmp_path / "v1.sug"
        file_path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

        loaded = SugProjectParser.load(str(file_path))
        s1, s2 = loaded.sentences
        # 旧逻辑：char0.timestamps == [1000, 0, 2000]（0ms 伪打轴）
        assert s1.characters[0].timestamps == [1000]
        assert s1.characters[1].timestamps == [3000]
        # 连续前缀回归：dense 情况不变
        assert s2.characters[0].timestamps == [4000, 4500]
        assert s2.characters[1].timestamps == [5000]

    def test_write_atomic_no_tmp_leftover(self, tmp_path):
        """I1：正常写盘后不残留 .tmp 临时文件。"""
        project = Project()
        singer = project.get_default_singer()
        project.add_sentence(Sentence.from_text("テスト", singer.id))
        file_path = tmp_path / "a.sug"

        SugProjectParser.save(project, str(file_path))

        assert file_path.exists()
        assert list(tmp_path.glob("*.tmp")) == []

    def test_write_failure_keeps_previous_file(self, tmp_path, monkeypatch):
        """I1：写盘中途失败 → 正式文件保持上一次的完整内容，临时文件被清理。"""
        project = Project()
        singer = project.get_default_singer()
        project.add_sentence(Sentence.from_text("テスト", singer.id))
        file_path = tmp_path / "a.sug"

        SugProjectParser.save(project, str(file_path))
        content_before = file_path.read_text(encoding="utf-8")

        def _boom(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(sug_io_mod.json, "dump", _boom)
        with pytest.raises(SugParseError):
            SugProjectParser.save(project, str(file_path))

        assert file_path.read_text(encoding="utf-8") == content_before
        assert list(tmp_path.glob("*.tmp")) == []


# ──────────────────────────────────────────────
# F8 Nicokara @Ruby 负相对时间戳
# ──────────────────────────────────────────────


class TestNicokaraNegativeRelativeTs:
    def test_negative_relative_ts_omitted(self, tmp_path):
        """乱序脏数据（后拍早于组首拍）不再伪造 [00:00:00]：
        该 part 省略相对时间戳，读音按序拼接。"""
        project = Project()
        singer = project.singers[0]
        sent = Sentence.from_text("漢", singer.id)
        ch = sent.characters[0]
        ch.check_count = 2
        ch.set_ruby(Ruby(parts=[RubyPart(text="あ"), RubyPart(text="い")]))
        ch.add_timestamp(5000, checkpoint_idx=0)
        ch.add_timestamp(4000, checkpoint_idx=1)  # 乱序：早于首拍
        project.add_sentence(sent)

        content = _export_to(str(tmp_path), ".lrc", NicokaraWithRubyExporter(), project)
        assert "@Ruby1=漢,あい,[00:05:00]" in content, content
        assert "00:00:00" not in content, content

    def test_positive_relative_ts_kept(self, tmp_path):
        """正常数据回归：相对时间戳照常输出（既有行为）。"""
        project = Project()
        singer = project.singers[0]
        sent = Sentence.from_text("漢", singer.id)
        ch = sent.characters[0]
        ch.check_count = 2
        ch.set_ruby(Ruby(parts=[RubyPart(text="あ"), RubyPart(text="い")]))
        ch.add_timestamp(5000, checkpoint_idx=0)
        ch.add_timestamp(5150, checkpoint_idx=1)
        project.add_sentence(sent)

        content = _export_to(str(tmp_path), ".lrc", NicokaraWithRubyExporter(), project)
        assert re.search(r"@Ruby\d+=漢,あ\[00:00:15\]い", content), content


# ──────────────────────────────────────────────
# F16 lrc [ti:] 转义
# ──────────────────────────────────────────────


class TestLrcIdTagEscape:
    def test_metadata_bracket_escaped(self, tmp_path):
        """标题中的半角 ] 不再提前闭合 [ti:] 标签。"""
        project = Project()
        singer = project.singers[0]
        sent = Sentence.from_text("あ", singer.id)
        sent.characters[0].add_timestamp(1000)
        project.add_sentence(sent)
        project.metadata.title = "Live]テイク"

        content = _export_to(str(tmp_path), ".lrc", LRCExporter(), project)
        assert "[ti:Live］テイク]" in content, content
        # 不应出现会被解析截断的原文形态
        assert "[ti:Live]" not in content, content


# ──────────────────────────────────────────────
# F12 / F13 network_dictionary
# ──────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _ScriptedOpener:
    """按脚本依次响应：Exception → 抛出；bytes → 返回响应体。"""

    def __init__(self, script: List[Any]):
        self.script = list(script)
        self.requests: List[Any] = []

    def open(self, req: Any, timeout: Any = None) -> _FakeResponse:
        self.requests.append(req)
        action = self.script.pop(0)
        if isinstance(action, Exception):
            raise action
        return _FakeResponse(action)


class TestNetworkDictionaryFetchFixes:
    def test_builtin_source_uses_https(self):
        """F12：内置源 URL 走 https（不再明文 HTTP）。"""
        assert nd.BUILTIN_SOURCES[0]["url"].startswith("https://")

    def test_query_separator_for_url_with_existing_query(self, monkeypatch):
        """F12：源 URL 自带 query 时用 & 续接参数，不破坏原查询串。"""
        opener = _ScriptedOpener([b"[success]\n\xe3\x81\x82\t\xe3\x81\x82\n"])
        monkeypatch.setattr(urllib.request, "build_opener", lambda *h: opener)

        nd.fetch_source_entries("http://example.net/dict.php?req=version&fmt=tsv")

        url = opener.requests[0].full_url
        assert "dict.php?req=version&fmt=tsv&req=get&dummy=" in url, url

    def test_query_separator_for_plain_url(self, monkeypatch):
        """F12：普通 URL 仍用 ? 拼接（旧行为回归）。"""
        opener = _ScriptedOpener([b"[success]\n\xe3\x81\x82\t\xe3\x81\x82\n"])
        monkeypatch.setattr(urllib.request, "build_opener", lambda *h: opener)

        nd.fetch_source_entries("http://example.net/dict.php")

        url = opener.requests[0].full_url
        assert "dict.php?req=get&dummy=" in url, url

    def test_insecure_fallback_reported_in_diagnostics(self, monkeypatch, caplog):
        """F13：证书验证失败降级重试时写入诊断信息并记录 warning 日志。"""
        cert_error = urllib.error.URLError(
            ssl.SSLError("CERTIFICATE_VERIFY_FAILED: certificate verify failed")
        )
        opener = _ScriptedOpener([cert_error, b"[success]\n\xe3\x81\x82\t\xe3\x81\x82\n"])
        monkeypatch.setattr(urllib.request, "build_opener", lambda *h: opener)

        diagnostics: dict = {}
        with caplog.at_level(
            logging.WARNING,
            logger="strange_uta_game.backend.infrastructure.network_dictionary",
        ):
            entries = nd.fetch_source_entries(
                "https://example.net/d.php", diagnostics=diagnostics
            )

        assert len(opener.requests) == 2, "应先验证失败再无验证重试"
        assert diagnostics.get("insecure_fallback") is True
        assert "CERTIFICATE_VERIFY_FAILED" in str(diagnostics.get("reason"))
        assert len(entries) == 1
        assert "降级" in caplog.text

    def test_no_diagnostics_without_fallback(self, monkeypatch):
        """F13：验证成功时不写入降级标记（不误报）。"""
        opener = _ScriptedOpener([b"[success]\n\xe3\x81\x82\t\xe3\x81\x82\n"])
        monkeypatch.setattr(urllib.request, "build_opener", lambda *h: opener)

        diagnostics: dict = {}
        nd.fetch_source_entries("https://example.net/d.php", diagnostics=diagnostics)
        assert diagnostics == {}

    def test_auto_update_surfaces_downgrade_message(self, monkeypatch):
        """F13：auto_update_enabled_sources 把降级事件放进 UI 可见的消息列表。"""

        def _fake_fetch(url, timeout=8.0, allow_insecure_fallback=True,
                        proxies=None, diagnostics=None):
            diagnostics["insecure_fallback"] = True
            return [{"word": "あ", "reading": "あ", "enabled": True}]

        monkeypatch.setattr(nd, "fetch_source_entries", _fake_fetch)
        doc = {
            "sources": [
                {"id": "s1", "name": "S1", "url": "https://example.net/d.php", "enabled": True}
            ]
        }
        ok_msgs, fail_msgs = nd.auto_update_enabled_sources(doc)

        assert not fail_msgs
        assert any("证书验证失败" in m for m in ok_msgs), ok_msgs
