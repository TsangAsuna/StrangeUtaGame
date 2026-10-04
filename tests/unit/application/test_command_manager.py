"""CommandManager 测试。"""

import pytest
from strange_uta_game.backend.application import CommandManager, Command


class SimpleCommand(Command):
    """测试用简单命令"""

    def __init__(self, value: int):
        self.value = value
        self.executed = False
        self.undone = False

    def execute(self) -> None:
        self.executed = True

    def undo(self) -> None:
        self.undone = True

    @property
    def description(self) -> str:
        return f"Command {self.value}"


class TestCommandManager:
    """测试命令管理器"""

    def test_execute_command(self):
        manager = CommandManager()
        cmd = SimpleCommand(1)

        manager.execute(cmd)

        assert cmd.executed
        assert manager.can_undo()
        assert not manager.can_redo()

    def test_undo(self):
        manager = CommandManager()
        cmd = SimpleCommand(1)

        manager.execute(cmd)
        desc = manager.undo()

        assert cmd.undone
        assert desc == "Command 1"
        assert not manager.can_undo()
        assert manager.can_redo()

    def test_redo(self):
        manager = CommandManager()
        cmd = SimpleCommand(1)

        manager.execute(cmd)
        manager.undo()

        # 重新执行
        desc = manager.redo()

        assert cmd.executed  # redo 调用 execute
        assert desc == "Command 1"
        assert manager.can_undo()
        assert not manager.can_redo()

    def test_undo_empty_stack(self):
        manager = CommandManager()

        desc = manager.undo()

        assert desc is None

    def test_redo_empty_stack(self):
        manager = CommandManager()

        desc = manager.redo()

        assert desc is None

    def test_clear_redo_on_new_execute(self):
        """测试新命令执行后清空重做栈"""
        manager = CommandManager()

        cmd1 = SimpleCommand(1)
        cmd2 = SimpleCommand(2)

        manager.execute(cmd1)
        manager.undo()  # 撤销 cmd1

        assert manager.can_redo()  # 可以重做 cmd1

        manager.execute(cmd2)  # 执行新命令

        assert not manager.can_redo()  # 重做栈被清空

    def test_max_history(self):
        """测试最大历史记录限制"""
        manager = CommandManager(max_history=3)

        # 执行 5 个命令，但只保留最近 3 个
        for i in range(5):
            manager.execute(SimpleCommand(i))

        assert manager.get_undo_stack_size() == 3

    def test_clear(self):
        """测试清空所有历史"""
        manager = CommandManager()

        manager.execute(SimpleCommand(1))
        manager.undo()

        assert manager.can_undo() or manager.can_redo()

        manager.clear()

        assert not manager.can_undo()
        assert not manager.can_redo()

    def test_get_descriptions(self):
        """测试获取命令描述"""
        manager = CommandManager()

        manager.execute(SimpleCommand(1))
        manager.execute(SimpleCommand(2))

        assert manager.get_undo_description() == "Command 2"

        manager.undo()

        assert manager.get_undo_description() == "Command 1"
        assert manager.get_redo_description() == "Command 2"

    def test_state_changed_callback(self):
        """测试状态变更回调"""
        callback_count = 0

        def on_state_changed():
            nonlocal callback_count
            callback_count += 1

        manager = CommandManager()
        manager.set_on_state_changed(on_state_changed)

        manager.execute(SimpleCommand(1))

        assert callback_count == 1

        manager.undo()

        assert callback_count == 2


class FlakyCommand(Command):
    """测试用可切换成功/失败的命令（undo/redo 行为独立可控）"""

    def __init__(self, value: int):
        self.value = value
        self.undone = False
        self.executed = False
        self.fail_undo = False
        self.fail_redo = False

    def execute(self) -> None:
        self.executed = True

    def undo(self) -> None:
        if self.fail_undo:
            raise RuntimeError("undo 失败")
        self.undone = True

    def redo(self) -> None:
        if self.fail_redo:
            raise RuntimeError("redo 失败")
        self.executed = True

    @property
    def description(self) -> str:
        return f"Flaky {self.value}"


class TestTransactionalUndoRedo:
    """事务式迁移：undo/redo 执行失败时命令放回原栈（C3）。"""

    def test_undo_failure_keeps_command_on_undo_stack(self):
        manager = CommandManager()
        cmd = FlakyCommand(1)
        manager.execute(cmd)
        cmd.fail_undo = True

        with pytest.raises(RuntimeError, match="undo 失败"):
            manager.undo()

        # 命令未丢失：仍在撤销栈，可再次撤销
        assert manager.can_undo()
        assert manager.get_undo_description() == "Flaky 1"
        assert not manager.can_redo()

        cmd.fail_undo = False
        assert manager.undo() == "Flaky 1"
        assert manager.can_redo()

    def test_redo_failure_keeps_command_on_redo_stack(self):
        manager = CommandManager()
        cmd = FlakyCommand(1)
        manager.execute(cmd)
        manager.undo()
        cmd.fail_redo = True

        with pytest.raises(RuntimeError, match="redo 失败"):
            manager.redo()

        # 重做条目未丢失（如 AI 打轴 redo 抛 ProjectDriftError 的场景）：
        # 用户仍可撤销该条目回退状态，而不是永久丢失
        assert manager.can_redo()
        assert manager.get_redo_description() == "Flaky 1"
        assert not manager.can_undo()

        cmd.fail_redo = False
        assert manager.redo() == "Flaky 1"
        assert manager.can_undo()

    def test_failure_notifies_state_changed(self):
        callback_count = 0

        def on_state_changed():
            nonlocal callback_count
            callback_count += 1

        manager = CommandManager()
        manager.set_on_state_changed(on_state_changed)
        cmd = FlakyCommand(1)
        manager.execute(cmd)
        cmd.fail_undo = True

        with pytest.raises(RuntimeError):
            manager.undo()

        # execute(1) + 失败回调(2) + 无第二次——失败路径也触发状态回调
        assert callback_count == 2
