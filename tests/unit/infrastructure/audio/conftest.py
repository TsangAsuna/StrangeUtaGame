"""audio 子目录局部测试配置。

- 把 ``SUG_CACHE_DIR`` 重定向到会话隔离目录：TSM 缓存 / 视频提取缓存
  （app_dirs.cache_dir() 每次调用时读环境变量）不再读写仓库/用户真实
  缓存，避免测试互相污染与误删。必须在任何被测模块调用 cache_dir()
  之前设置（cache_dir() 是调用时求值，导入期设置即生效）。
- 不动仓库级 tests/conftest.py（多代理并行修改）。
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

_SESSION_ROOT = (
    Path(__file__).resolve().parents[3]
    / ".test_sessions"
    / f"s{os.getpid()}-{int(time.time())}"
)

os.environ.setdefault("SUG_CACHE_DIR", str(_SESSION_ROOT / "cache"))


def pytest_sessionfinish(session, exitstatus):
    """会话结束 best-effort 清理本子目录会话缓存（与仓库级 conftest 同风格）。"""
    shutil.rmtree(str(_SESSION_ROOT), ignore_errors=True)
    try:
        parent = _SESSION_ROOT.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass
