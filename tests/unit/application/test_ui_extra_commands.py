"""SingersSnapshotCommand（演唱者快照撤销命令）单元测试。

覆盖 H4：演唱者界面整体操作此前绕过 CommandManager，现在通过
``ui_extra_commands.SingersSnapshotCommand`` 登记为可撤销命令：
- singers-only 快照：重命名/重排等不触碰句子的操作；
- 含句子快照：删除演唱者的句子转移在撤销时一并还原。
"""

from __future__ import annotations

from copy import deepcopy

from strange_uta_game.backend.application import CommandManager
from strange_uta_game.backend.application.commands.ui_extra_commands import (
    SingersSnapshotCommand,
)
from strange_uta_game.backend.domain import Project, Sentence, Singer


def _make_project() -> tuple[Project, Singer, Singer]:
    a = Singer(name="A", color="#FF0000", is_default=True)
    b = Singer(name="B", color="#00FF00")
    project = Project(singers=[a, b])
    # 句子归属 B：删除 B 并转移后句子应改属 A
    project.sentences = [Sentence.from_text("あいう", b.id)]
    return project, a, b


def test_singers_only_snapshot_undo_redo():
    """不触碰句子的操作：undo/redo 只在 singers 上往返，句子对象不动。"""
    project, a, b = _make_project()
    before = deepcopy(project.singers)
    sentences_before = project.sentences

    b.rename("C")
    after = deepcopy(project.singers)
    manager = CommandManager()
    manager.execute(SingersSnapshotCommand(project, before, after, "编辑演唱者 C"))
    assert project.singers[1].name == "C"

    manager.undo()
    assert [s.name for s in project.singers] == ["A", "B"]
    # 未传句子快照：sentences 列表对象保持原引用（不被覆盖）
    assert project.sentences is sentences_before

    manager.redo()
    assert [s.name for s in project.singers] == ["A", "C"]


def test_snapshot_with_sentences_restores_transfer():
    """删除演唱者（句子转移）场景：undo 同时还原 singers 与句子归属。"""
    project, a, b = _make_project()
    before_singers = deepcopy(project.singers)
    before_sentences = deepcopy(project.sentences)
    b_id = b.id

    # 模拟删除 B 并把句子转移给 A
    project.remove_singer(b_id, transfer_to=a.id)
    manager = CommandManager()
    manager.execute(
        SingersSnapshotCommand(
            project,
            before_singers,
            deepcopy(project.singers),
            "删除 1 位演唱者",
            before_sentences=before_sentences,
            after_sentences=deepcopy(project.sentences),
        )
    )
    assert [s.name for s in project.singers] == ["A"]
    assert project.sentences[0].singer_id == a.id
    assert project.sentences[0].characters[0].singer_id == a.id

    manager.undo()
    assert [s.name for s in project.singers] == ["A", "B"]
    assert project.sentences[0].singer_id == b_id
    assert project.sentences[0].characters[0].singer_id == b_id

    manager.redo()
    assert [s.name for s in project.singers] == ["A"]
    assert project.sentences[0].singer_id == a.id


def test_execute_is_idempotent_reapply():
    """execute 在操作已就地生效后再次应用 after 快照（与现 UI 模式一致）。"""
    project, a, b = _make_project()
    before = deepcopy(project.singers)
    b.rename("C")
    after = deepcopy(project.singers)
    manager = CommandManager()
    manager.execute(SingersSnapshotCommand(project, before, after, "编辑"))
    # execute 内部再赋值一次 after 快照，结果一致
    assert [s.name for s in project.singers] == ["A", "C"]
