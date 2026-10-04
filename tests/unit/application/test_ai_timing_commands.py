"""AI 打轴写回命令测试：原子应用与撤销/重做。

C3 回归：redo 直接按 execute 后快照恢复、跳过业务校验——撤销后用户
改注音（标注漂移）再重做，此前必抛 ProjectDriftError 且重做条目永久
丢失；快照即执行时校验通过的权威状态。
"""

import pytest

from strange_uta_game.backend.application.ai_timing.alignment import (
    AlignmentResult,
    EmissionSpan,
    build_alignment_request,
)
from strange_uta_game.backend.application.ai_timing.commands import (
    ApplyAiTimingCommand,
)
from strange_uta_game.backend.application.ai_timing.resolver import (
    PronunciationResolver,
)
from strange_uta_game.backend.domain import (
    Character,
    Project,
    Ruby,
    RubyPart,
    Sentence,
)
from strange_uta_game.backend.infrastructure.parsers.ruby_analyzer import (
    DummyAnalyzer,
)


def _project():
    """两行全假名工程（假名自读 → 无缺口）。"""
    project = Project()
    s1 = Sentence(
        singer_id="s1",
        characters=[
            Character(char="あ", check_count=1, ruby=None, singer_id="s1"),
            Character(char="か", check_count=1, ruby=None, singer_id="s1"),
        ],
    )
    s2 = Sentence(
        singer_id="s1",
        characters=[Character(char="さ", check_count=1, ruby=None, singer_id="s1")],
    )
    project.sentences = [s1, s2]
    return project


def _make_command(project):
    resolver = PronunciationResolver(analyzer=DummyAnalyzer(), chinese_mode=False)
    plan = resolver.resolve_project(project)
    request = build_alignment_request(plan)
    result = AlignmentResult(
        annotation_digest=plan.annotation_digest,
        model_id="fake",
        spans=[
            EmissionSpan(t.index, i * 100, i * 100 + 50)
            for i, t in enumerate(request.tokens)
        ],
    )
    return ApplyAiTimingCommand(project, plan, request, result)


class TestApplyAiTimingCommandRedo:
    def test_redo_after_annotation_change_skips_validation(self):
        """撤销后改注音再重做：直接按快照恢复，不抛 ProjectDriftError。"""
        project = _project()
        command = _make_command(project)

        command.execute()
        applied_ts = [
            list(ch.timestamps)
            for s in project.sentences
            for ch in s.characters
        ]
        command.undo()
        restored_ts = [
            list(ch.timestamps) for s in project.sentences for ch in s.characters
        ]
        assert applied_ts != restored_ts, "撤销应恢复执行前时间轴"

        # 撤销后用户修改注音 → 标注与 plan 的摘要漂移
        project.sentences[0].characters[0].set_ruby(
            Ruby(parts=[RubyPart(text="え")])
        )

        # 重做不得抛 ProjectDriftError，且时间轴恢复到执行后状态
        command.redo()
        redone_ts = [
            list(ch.timestamps) for s in project.sentences for ch in s.characters
        ]
        assert redone_ts == applied_ts

    def test_first_execute_still_validates(self):
        """首跑校验不被绕过：标注漂移时 execute 仍必须阻断。"""
        project = _project()
        command = _make_command(project)
        # 执行前就漂移
        project.sentences[0].characters[0].set_ruby(
            Ruby(parts=[RubyPart(text="え")])
        )
        from strange_uta_game.backend.application.ai_timing.resolver import (
            ProjectDriftError,
        )

        with pytest.raises(ProjectDriftError):
            command.execute()

    def test_undo_redo_roundtrip_restores_snapshots(self):
        """undo/redo 多轮往返：数据严格回到对应快照。"""
        project = _project()
        command = _make_command(project)
        before = [
            list(ch.timestamps) for s in project.sentences for ch in s.characters
        ]
        command.execute()
        after = [
            list(ch.timestamps) for s in project.sentences for ch in s.characters
        ]
        command.undo()
        assert [
            list(ch.timestamps) for s in project.sentences for ch in s.characters
        ] == before
        command.redo()
        assert [
            list(ch.timestamps) for s in project.sentences for ch in s.characters
        ] == after
