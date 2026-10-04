"""``updater_app/main.py`` 内部工具函数的单元测试。

只覆盖纯逻辑函数，不涉及网络与文件系统的实际更新流程。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _ensure_path():
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    yield


def _get_module():
    import importlib
    return importlib.import_module("updater_app.main")


# ───────────────────────── _retry_on_permission_error ─────────────────────────


class TestRetryOnPermissionError:
    """验证关键文件操作的重试逻辑（应对 Windows 文件锁延迟释放）。"""

    def test_succeeds_first_try(self):
        mod = _get_module()
        log = logging.getLogger("test")
        calls = []

        def op():
            calls.append("ok")
            return 42

        result = mod._retry_on_permission_error("test", op, log, max_retries=3, interval=0.01)
        assert result == 42
        assert calls == ["ok"]

    def test_recovers_after_permission_error(self):
        mod = _get_module()
        log = logging.getLogger("test")
        counter = {"n": 0}

        def op():
            counter["n"] += 1
            if counter["n"] < 3:
                raise PermissionError("locked")
            return "done"

        result = mod._retry_on_permission_error("test", op, log, max_retries=5, interval=0.01)
        assert result == "done"
        assert counter["n"] == 3

    def test_recovers_after_winerror_5(self):
        """模拟 Windows 拒绝访问 (WinError 5)。"""
        mod = _get_module()
        log = logging.getLogger("test")
        counter = {"n": 0}

        def op():
            counter["n"] += 1
            if counter["n"] < 2:
                exc = OSError("access denied")
                exc.winerror = 5  # type: ignore[attr-defined]
                raise exc
            return None

        result = mod._retry_on_permission_error("test", op, log, max_retries=4, interval=0.01)
        assert result is None
        assert counter["n"] == 2

    def test_recovers_after_winerror_32(self):
        """模拟 Windows 文件被占用 (WinError 32)。"""
        mod = _get_module()
        log = logging.getLogger("test")
        counter = {"n": 0}

        def op():
            counter["n"] += 1
            if counter["n"] < 2:
                exc = OSError("file in use")
                exc.winerror = 32  # type: ignore[attr-defined]
                raise exc
            return None

        result = mod._retry_on_permission_error("test", op, log, max_retries=4, interval=0.01)
        assert result is None
        assert counter["n"] == 2

    def test_does_not_retry_on_other_oserror(self):
        """非 PermissionError / WinError 5/32 的 OSError 不重试，直接抛出。"""
        mod = _get_module()
        log = logging.getLogger("test")
        counter = {"n": 0}

        def op():
            counter["n"] += 1
            exc = OSError("no such file")
            exc.winerror = 2  # type: ignore[attr-defined]
            raise exc

        with pytest.raises(OSError) as excinfo:
            mod._retry_on_permission_error("test", op, log, max_retries=5, interval=0.01)
        assert excinfo.value.winerror == 2
        assert counter["n"] == 1

    def test_exhausts_retries_and_raises(self):
        mod = _get_module()
        log = logging.getLogger("test")
        counter = {"n": 0}

        def op():
            counter["n"] += 1
            raise PermissionError("always locked")

        with pytest.raises(PermissionError):
            mod._retry_on_permission_error(
                "test", op, log, max_retries=3, interval=0.01,
            )
        assert counter["n"] == 3


class TestModuleConstants:
    def test_constants_sane(self):
        mod = _get_module()
        assert mod.WAIT_PID_TIMEOUT > 0
        assert mod.POST_EXIT_GRACE_SECONDS > 0
        assert mod.FILE_LOCK_RETRY_COUNT >= 3
        assert mod.FILE_LOCK_RETRY_INTERVAL > 0


# ───────────────────────── _cleanup_temp_workdir（parts/ 复用） ─────────────────────────


class TestCleanupTempWorkdirParts:
    """启动期清理必须保留当前版本的 parts/（主程序自更新预下载的增量复用 zip）。"""

    def _make_workdir(self, tmp_path: Path) -> Path:
        parts = tmp_path / "parts"
        parts.mkdir()
        (parts / "StrangeUtaGame-v2.0.0-app.zip").write_bytes(b"x")
        (parts / "StrangeUtaGame-v1.0.0-app.zip").write_bytes(b"x")
        download = tmp_path / "download"
        download.mkdir()
        (download / "junk.bin").write_bytes(b"x")
        return tmp_path

    def test_startup_keeps_current_version_parts(self, tmp_path):
        mod = _get_module()
        work = self._make_workdir(tmp_path)
        mod._cleanup_temp_workdir(work, keep_parts_version="2.0.0")
        # 当前版本的 part zip 保留给增量复用
        assert (work / "parts" / "StrangeUtaGame-v2.0.0-app.zip").exists()
        # 其他版本的过期 part 与 download/ 仍被清理
        assert not (work / "parts" / "StrangeUtaGame-v1.0.0-app.zip").exists()
        assert not (work / "download").exists()

    def test_final_cleanup_removes_parts(self, tmp_path):
        mod = _get_module()
        work = self._make_workdir(tmp_path)
        mod._cleanup_temp_workdir(work)
        # 更新成功后的最终清理：整体删除
        assert not (work / "parts").exists()

    def test_stale_dirs_removed_in_keep_mode(self, tmp_path):
        mod = _get_module()
        work = self._make_workdir(tmp_path)
        extracted = work / "extracted"
        extracted.mkdir()
        (extracted / "f").write_bytes(b"x")
        mod._cleanup_temp_workdir(work, keep_parts_version="2.0.0")
        assert not extracted.exists()


# ───────────────────────── verify_sha256（fail-closed） ─────────────────────────


class TestVerifySha256FailClosed:
    def _file(self, tmp_path: Path) -> Path:
        f = tmp_path / "a.zip"
        f.write_bytes(b"PK\x03\x04fake")
        return f

    def test_missing_expected_refuses(self, tmp_path):
        """.sha256 拉取失败（expected 为空）必须拒绝安装，不得跳过校验。"""
        mod = _get_module()
        assert mod.verify_sha256(self._file(tmp_path), "", logging.getLogger("t")) is False

    def test_matching_digest_passes(self, tmp_path):
        import hashlib

        mod = _get_module()
        f = self._file(tmp_path)
        digest = hashlib.sha256(b"PK\x03\x04fake").hexdigest()
        assert mod.verify_sha256(f, digest, logging.getLogger("t")) is True

    def test_mismatched_digest_refuses(self, tmp_path):
        mod = _get_module()
        assert mod.verify_sha256(
            self._file(tmp_path), "ab" * 32, logging.getLogger("t")
        ) is False
