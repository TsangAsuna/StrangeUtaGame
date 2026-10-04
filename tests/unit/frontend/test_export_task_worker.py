"""ExportTaskWorker：后台导出的原子落盘 / 失败清理 / 取消（H3）。

worker 逐任务先写 ``<目标>.tmp``，成功后 os.replace 覆盖目标 —— 正式文件
永远不会以截断状态出现；失败清理 .tmp 并记录；request_cancel 在任务间生效。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtWidgets import QApplication

from strange_uta_game.backend.application.export_service import ExportResult
from strange_uta_game.backend.domain import Project, Singer
from strange_uta_game.frontend.workers import ExportTaskWorker


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _WriterService:
    """伪导出服务：写文件后返回成功/失败可由用例控制。"""

    def __init__(self, fail_paths: set[str] | None = None):
        self.fail_paths = fail_paths or set()
        self.calls: list[str] = []

    def export(self, project, format_name, file_path, **kwargs):
        self.calls.append(file_path)
        Path(file_path).write_text("DATA", encoding="utf-8")
        if file_path in self.fail_paths:
            return ExportResult(success=False, error_message="boom")
        return ExportResult(success=True, file_path=file_path)


def _project():
    return Project(singers=[Singer(name="A", color="#FF0000", is_default=True)])


def _run_worker(worker) -> dict:
    result_box = {}
    worker.finished.connect(lambda r: result_box.update(r))
    errors = []
    worker.error.connect(errors.append)
    worker.run()  # 直接同步调用（无需起线程）
    assert errors == []
    return result_box


def test_success_writes_tmp_then_replaces(tmp_path, qapp):
    target = tmp_path / "out.txt"
    worker = ExportTaskWorker(
        _project(),
        _WriterService(),
        [{"file_path": str(target), "format_name": "X", "ext": ".txt", "kwargs": {}}],
    )
    result = _run_worker(worker)

    assert target.read_text(encoding="utf-8") == "DATA"
    assert not (tmp_path / "out.tmp.txt").exists(), "成功后临时文件应已被 replace 消费"
    assert result["exported"] == [str(target)]
    assert result["failed"] == []
    assert result["cancelled"] is False


def test_failure_removes_tmp_and_reports(tmp_path, qapp):
    target = tmp_path / "bad.txt"
    tmp = tmp_path / "bad.tmp.txt"
    service = _WriterService(fail_paths={str(tmp)})
    worker = ExportTaskWorker(
        _project(),
        service,
        [{"file_path": str(target), "format_name": "X", "ext": ".txt", "kwargs": {}}],
    )
    result = _run_worker(worker)

    assert not target.exists(), "失败的正式目标不应被写入"
    assert not tmp.exists(), "失败后临时文件应被清理"
    assert result["exported"] == []
    assert len(result["failed"]) == 1
    assert result["failed"][0][0] == str(target)
    assert "boom" in result["failed"][0][1]


def test_exception_path_also_cleans_tmp(tmp_path, qapp):
    """导出器抛异常（而非返回失败结果）时同样清理临时文件。"""
    target = tmp_path / "err.txt"
    tmp = tmp_path / "err.tmp.txt"

    class _RaisingService:
        def export(self, project, format_name, file_path, **kwargs):
            Path(file_path).write_text("PARTIAL", encoding="utf-8")
            raise RuntimeError("disk gone")

    worker = ExportTaskWorker(
        _project(),
        _RaisingService(),
        [{"file_path": str(target), "format_name": "X", "ext": ".txt", "kwargs": {}}],
    )
    result = _run_worker(worker)

    assert not target.exists()
    assert not tmp.exists()
    assert result["failed"] and "RuntimeError" in result["failed"][0][1]


def test_cancel_skips_remaining_jobs(tmp_path, qapp):
    service = _WriterService()
    jobs = [
        {"file_path": str(tmp_path / f"out{i}.txt"), "format_name": "X", "kwargs": {}}
        for i in range(3)
    ]
    worker = ExportTaskWorker(_project(), service, jobs)
    worker.request_cancel()
    result = _run_worker(worker)

    assert result["cancelled"] is True
    assert result["exported"] == []
    assert service.calls == [], "取消后不应执行任何导出任务"
