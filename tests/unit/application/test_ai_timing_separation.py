# -*- coding: utf-8 -*-
"""standalone 分离执行器：假子进程端到端测试（stdout 协议、取消、兜底轨名）。"""

import io
import json
import os
import threading
import time
from pathlib import Path

import pytest

from strange_uta_game.backend.application.ai_timing import separation as sep_mod
from strange_uta_game.backend.application.ai_timing.separation import (
    StandaloneVocalSeparator,
    _SCRIPT,
)


class _FakeProc:
    def __init__(self, lines, returncode=0):
        self.stdout = io.StringIO("".join(l + "\n" for l in lines))
        self.killed = False
        self._returncode = returncode

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return self._returncode


class _ManualStdout:
    """可编程管道替身：测试按需 push 行、close 后 EOF，模拟子进程
    静默期（模型下载/加载/推理中无换行输出）。"""

    def __init__(self):
        self._cv = threading.Condition()
        self._items = []
        self._eof = False

    def push(self, line: str) -> None:
        with self._cv:
            self._items.append(line)
            self._cv.notify_all()

    def close(self) -> None:
        with self._cv:
            self._eof = True
            self._cv.notify_all()

    def readline(self):
        with self._cv:
            while not self._items and not self._eof:
                self._cv.wait(timeout=0.2)
            if self._items:
                return self._items.pop(0)
            return ""

    def __iter__(self):
        while True:
            line = self.readline()
            if not line:
                return
            yield line


def _separator(
    tmp_path, monkeypatch, lines, *, proxy="", returncode=0, embedded=False
):
    monkeypatch.setattr(StandaloneVocalSeparator, "available", lambda self: True)
    # 子进程协议单测不得访问真实网络；预下载行为由
    # TestSeparationModelPredownload 单独覆盖。
    monkeypatch.setattr(
        StandaloneVocalSeparator,
        "_download_missing_model_files",
        lambda self, progress=None, cancel=None: [],
    )
    python = tmp_path / "python.exe"
    python.write_bytes(b"")
    vocal = tmp_path / "song_人声.wav"
    vocal.write_bytes(b"v")
    ffmpeg = tmp_path / "tools" / "ffmpeg.exe"
    ffmpeg.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.write_bytes(b"")
    monkeypatch.setattr(sep_mod, "resolve_ffmpeg_exe", lambda: str(ffmpeg))
    # ffmpeg 预检被 mock：假的空壳 ffmpeg.exe 无法真正执行 -version
    monkeypatch.setattr(sep_mod, "ffmpeg_responsive", lambda exe, **k: True)
    proc = _FakeProc(lines, returncode=returncode)
    captured = {}

    def _popen(*args, **kwargs):
        captured.update(kwargs=kwargs)
        return proc

    monkeypatch.setattr(sep_mod.subprocess, "Popen", _popen)
    sep = StandaloneVocalSeparator(
        str(python), tmp_path / "models", proxy=proxy, embedded=embedded
    )
    return sep, proc, vocal, captured


class TestStandaloneSeparator:
    def test_success_flow_and_normalized_output(self, tmp_path, monkeypatch):
        vocal = tmp_path / "song_人声.wav"
        lines = [
            "stage:engine:初始化分离引擎",
            "stage:model:下载/加载分离模型",
            "stage:separate:分离处理中",
            "done:" + str(vocal),
        ]
        sep, proc, _, _ = _separator(tmp_path, monkeypatch, lines)
        events = []
        out = sep.separate(
            tmp_path / "song.flac", lambda *a: events.append(a), lambda: False
        )
        assert out == vocal
        assert not proc.killed
        assert events[-1][1] == 100

    def test_stage_ladder_engine_model(self, tmp_path, monkeypatch):
        """加载阶段细分：引擎初始化 12%、模型下载/加载 15%。"""
        lines = [
            "stage:engine:初始化分离引擎",
            "stage:model:下载/加载分离模型",
            "done:" + str(tmp_path / "song_人声.wav"),
        ]
        sep, _, _, _ = _separator(tmp_path, monkeypatch, lines)
        events = []
        sep.separate(
            tmp_path / "song.flac", lambda *a: events.append(a), lambda: False
        )
        pcts = {e[2]: e[1] for e in events}
        assert pcts["初始化分离引擎"] == 12
        assert pcts["下载/加载分离模型"] == 15

    def test_download_progress_parsed(self, tmp_path, monkeypatch):
        """模型下载的 tqdm 字节条（_Auto runtime 实测格式：单位带 iB、
        大小写混用）折算到 15-55% 进度（含量/速度/ETA）。"""
        lines = [
            "stage:model:下载/加载分离模型",
            "  0%|          | 0.00/63.2M [00:00<?, ?iB/s]",
            " 20%|\u2588\u2588 | 12.7M/63.2M [00:12<00:48, 1.07MiB/s]",
            "done:" + str(tmp_path / "song_人声.wav"),
        ]
        sep, _, _, _ = _separator(tmp_path, monkeypatch, lines)
        events = []
        sep.separate(
            tmp_path / "song.flac", lambda *a: events.append(a), lambda: False
        )
        downloads = [e for e in events if "下载分离模型" in e[2]]
        assert len(downloads) == 2
        # 首帧无 ETA、当前量无单位（tqdm 对 0 不加单位）：只有量
        assert "0.00/63.2MB" in downloads[0][2]
        assert downloads[0][1] == 15
        stage, pct, msg = downloads[1]
        assert stage == "separation"
        assert pct == 15 + int(40 * (12.7 / 63.2))
        assert "12.7MB/63.2MB" in msg and "1.07MiB/s" in msg and "0:48" in msg

    def test_cancel_kills_and_waits_process(self, tmp_path, monkeypatch):
        lines = ["stage:load:加载分离模型", "stage:separate:分离处理中"]
        sep, proc, _, _ = _separator(tmp_path, monkeypatch, lines)
        calls = {"n": 0}

        def cancel():
            calls["n"] += 1
            return calls["n"] > 1

        with pytest.raises(RuntimeError, match="已取消"):
            sep.separate(tmp_path / "song.flac", lambda *a: None, cancel)
        assert proc.killed

    def test_no_done_line_raises(self, tmp_path, monkeypatch):
        sep, _, _, _ = _separator(
            tmp_path, monkeypatch, ["stage:load:加载分离模型"]
        )
        with pytest.raises(RuntimeError, match="人声分离失败"):
            sep.separate(tmp_path / "song.flac", lambda *a: None, lambda: False)

    def test_missing_ffmpeg_fails_fast_without_spawn(self, tmp_path, monkeypatch):
        """audio-separator 构造即探测 ffmpeg：缺失时启动前给出可操作
        中文错误（GitHub issue：只报「返回码 1」无法定位）。"""
        monkeypatch.setattr(StandaloneVocalSeparator, "available", lambda self: True)
        python = tmp_path / "python.exe"
        python.write_bytes(b"")
        monkeypatch.setattr(sep_mod, "resolve_ffmpeg_exe", lambda: "")
        spawned = []
        monkeypatch.setattr(
            sep_mod.subprocess,
            "Popen",
            lambda *a, **k: spawned.append(a) or _FakeProc([]),
        )
        sep = StandaloneVocalSeparator(str(python), tmp_path / "models")
        with pytest.raises(RuntimeError, match="FFmpeg"):
            sep.separate(tmp_path / "song.flac", lambda *a: None, lambda: False)
        assert spawned == []  # 未启动子进程即失败

    def test_child_env_injects_ffmpeg_dir_and_proxy(self, tmp_path, monkeypatch):
        """配置的 ffmpeg 路径与代理必须注入子进程环境：前者供
        audio-separator 探测，后者供模型首次下载（GitHub）使用。"""
        lines = ["done:" + str(tmp_path / "song_人声.wav")]
        sep, _, _, captured = _separator(
            tmp_path, monkeypatch, lines, proxy="http://127.0.0.1:7890"
        )
        sep.separate(tmp_path / "song.flac", lambda *a: None, lambda: False)
        env = captured["kwargs"]["env"]
        assert env["PATH"].startswith(str(tmp_path / "tools") + os.pathsep)
        assert env["HTTP_PROXY"] == "http://127.0.0.1:7890"
        assert env["HTTPS_PROXY"] == "http://127.0.0.1:7890"

    def test_failure_message_carries_child_tail_and_hint(self, tmp_path, monkeypatch):
        """失败时异常消息带上子进程输出尾部与常见原因提示，外部
        用户反馈不再只剩返回码。"""
        lines = [
            "stage:load:加载分离模型",
            "FFmpeg is not installed. Please install FFmpeg to use this package.",
            "Traceback (most recent call last):",
            "    raise",
            "FileNotFoundError: [WinError 2] 系统找不到指定的文件。",
        ]
        sep, _, _, _ = _separator(tmp_path, monkeypatch, lines, returncode=1)
        with pytest.raises(RuntimeError) as excinfo:
            sep.separate(tmp_path / "song.flac", lambda *a: None, lambda: False)
        message = str(excinfo.value)
        assert "返回码 1" in message
        assert "WinError 2" in message
        assert "FFmpeg" in message

    def test_failure_hint_for_model_download_error(self, tmp_path, monkeypatch):
        lines = [
            "Downloading file from https://github.com/TRvlvr/model_repo/...",
            "requests.exceptions.ConnectionError: Max retries exceeded",
        ]
        sep, _, _, _ = _separator(tmp_path, monkeypatch, lines, returncode=1)
        with pytest.raises(RuntimeError, match="代理"):
            sep.separate(tmp_path / "song.flac", lambda *a: None, lambda: False)

    def test_missing_ffmpeg_message_differs_by_mode(self, tmp_path, monkeypatch):
        """embedded 模式 SUG 自身的 ffmpeg 设置入口隐藏（EMBEDDING §5），
        失败提示必须引导到工作台；standalone 引导到 SUG 设置。"""
        monkeypatch.setattr(StandaloneVocalSeparator, "available", lambda self: True)
        python = tmp_path / "python.exe"
        python.write_bytes(b"")
        monkeypatch.setattr(sep_mod, "resolve_ffmpeg_exe", lambda: "")
        monkeypatch.setattr(
            sep_mod.subprocess, "Popen", lambda *a, **k: _FakeProc([])
        )

        def _make(embedded):
            return StandaloneVocalSeparator(
                str(python), tmp_path / "models", embedded=embedded
            )

        with pytest.raises(RuntimeError) as standalone_err:
            _make(embedded=False).separate(
                tmp_path / "song.flac", lambda *a: None, lambda: False
            )
        assert "设置 → 关于/语言" in str(standalone_err.value)

        with pytest.raises(RuntimeError) as embedded_err:
            _make(embedded=True).separate(
                tmp_path / "song.flac", lambda *a: None, lambda: False
            )
        assert "工作台" in str(embedded_err.value)
        assert "设置 → 关于/语言" not in str(embedded_err.value)

    def test_embedded_failure_hint_points_to_workbench(self, tmp_path, monkeypatch):
        lines = [
            "stage:load:加载分离模型",
            "FFmpeg is not installed. Please install FFmpeg to use this package.",
            "Traceback (most recent call last):",
            "FileNotFoundError: [WinError 2] 系统找不到指定的文件。",
        ]
        sep, _, _, _ = _separator(
            tmp_path, monkeypatch, lines, returncode=1, embedded=True
        )
        with pytest.raises(RuntimeError) as excinfo:
            sep.separate(tmp_path / "song.flac", lambda *a: None, lambda: False)
        message = str(excinfo.value)
        assert "工作台" in message
        assert "设置 → 关于/语言" not in message

    def test_cancel_during_silence_is_immediate(self, tmp_path, monkeypatch):
        """真静默期（模型下载/加载，无任何换行输出）取消也必须在轮询
        周期级延迟内生效——旧行为是阻塞在 readline 上直到子进程输出。"""
        monkeypatch.setattr(StandaloneVocalSeparator, "available", lambda self: True)
        python = tmp_path / "python.exe"
        python.write_bytes(b"")
        ffmpeg = tmp_path / "tools" / "ffmpeg.exe"
        ffmpeg.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg.write_bytes(b"")
        monkeypatch.setattr(sep_mod, "resolve_ffmpeg_exe", lambda: str(ffmpeg))
        monkeypatch.setattr(sep_mod, "ffmpeg_responsive", lambda exe, **k: True)
        stdout = _ManualStdout()  # 永不输出，直到测试结束
        proc = _FakeProc([], returncode=1)
        proc.stdout = stdout
        monkeypatch.setattr(sep_mod.subprocess, "Popen", lambda *a, **k: proc)
        sep = StandaloneVocalSeparator(str(python), tmp_path / "models")
        monkeypatch.setattr(sep, "_download_missing_model_files", lambda **k: [])
        t0 = time.monotonic()

        with pytest.raises(RuntimeError, match="已取消"):
            sep.separate(
                tmp_path / "song.flac",
                lambda *a: None,
                lambda: time.monotonic() - t0 > 0.5,
            )
        assert time.monotonic() - t0 < 3  # 轮询周期级，而非等到输出
        assert proc.killed
        stdout.close()  # 释放读取线程

    def test_cancel_no_longer_bypassed_by_tqdm_lines(
        self, tmp_path, monkeypatch
    ):
        """tqdm 进度行不得绕过取消检查——旧实现里它们 continue 在
        cancel 之前，推理期（输出全是 tqdm 行）取消要等整段推理结束。"""
        monkeypatch.setattr(StandaloneVocalSeparator, "available", lambda self: True)
        python = tmp_path / "python.exe"
        python.write_bytes(b"")
        ffmpeg = tmp_path / "tools" / "ffmpeg.exe"
        ffmpeg.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg.write_bytes(b"")
        monkeypatch.setattr(sep_mod, "resolve_ffmpeg_exe", lambda: str(ffmpeg))
        monkeypatch.setattr(sep_mod, "ffmpeg_responsive", lambda exe, **k: True)
        stdout = _ManualStdout()
        proc = _FakeProc([], returncode=1)
        proc.stdout = stdout
        monkeypatch.setattr(sep_mod.subprocess, "Popen", lambda *a, **k: proc)
        sep = StandaloneVocalSeparator(str(python), tmp_path / "models")
        monkeypatch.setattr(sep, "_download_missing_model_files", lambda **k: [])
        state = {"clicked": False}

        def progress(stage, pct, msg):
            if "块" in msg:
                state["clicked"] = True  # 首块进度到达后用户点取消

        outcome = {}

        def _run():
            try:
                sep.separate(
                    tmp_path / "song.flac", progress, lambda: state["clicked"]
                )
            except RuntimeError as exc:
                outcome["exc"] = exc

        worker = threading.Thread(target=_run)
        worker.start()
        stdout.push("  0%|    | 1/8 [00:01<00:07, 1.0s/it]\n")
        for _ in range(60):
            if state["clicked"]:
                break
            time.sleep(0.05)
        assert state["clicked"], "首块进度未到达"
        # 翻转取消后送入下一条 tqdm 行：必须在处理该行时即取消
        stdout.push(" 12%|█▎  | 2/8 [00:02<00:06, 1.0s/it]\n")
        worker.join(timeout=3)
        assert not worker.is_alive()
        assert "已取消" in str(outcome.get("exc"))
        assert proc.killed
        stdout.close()

    def test_corrupt_model_deleted_before_spawn(self, tmp_path, monkeypatch):
        """分离启动前的模型体检：残缺模型自动删除并提示重下
        （audio-separator 非原子下载的中断残留，实测打包版复现）。"""
        vocal = tmp_path / "song_人声.wav"
        lines = ["done:" + str(vocal)]
        sep, _, _, _ = _separator(tmp_path, monkeypatch, lines)
        models = tmp_path / "models"
        model = models / sep_mod.SEPARATION_MODEL
        model.parent.mkdir(parents=True, exist_ok=True)
        model.write_bytes(b"truncated-payload")
        (models / "mdx_model_data.json").write_text(
            json.dumps({"00000000000000000000000000000000": {}}),
            encoding="utf-8",
        )
        messages = []
        predownload = []

        def _download(*, progress=None, cancel=None):
            # 镜像预下载开始前，体检必须已经删除残缺模型；否则它会因
            # 文件存在而跳过，后续子进程重新退回 GitHub 直连。
            predownload.append((model.exists(), progress, cancel))
            return []

        monkeypatch.setattr(sep, "_download_missing_model_files", _download)

        def cancel():
            return False

        sep.separate(
            tmp_path / "song.flac",
            lambda s, p, m: messages.append(m),
            cancel,
        )
        assert predownload and predownload[0][0] is False
        assert callable(predownload[0][1])
        assert predownload[0][2] is cancel
        assert any("重新下载" in m for m in messages)
        assert not model.exists()

    def test_cancel_during_predownload_stops_before_spawn(
        self, tmp_path, monkeypatch
    ):
        """预下载收到取消后不得继续启动 audio-separator 子进程。"""
        sep, _, _, _ = _separator(tmp_path, monkeypatch, [])
        spawned = []

        def _cancelled_download(*, progress=None, cancel=None):
            assert callable(progress) and callable(cancel)
            raise RuntimeError("已取消")

        monkeypatch.setattr(sep, "_download_missing_model_files", _cancelled_download)
        monkeypatch.setattr(
            sep_mod.subprocess,
            "Popen",
            lambda *a, **k: spawned.append((a, k)),
        )

        with pytest.raises(RuntimeError, match="已取消"):
            sep.separate(
                tmp_path / "song.flac", lambda *a: None, lambda: True
            )
        assert spawned == []

    def test_separate_skips_redundant_available_probe(
        self, tmp_path, monkeypatch
    ):
        """执行期不再重复 available() 导入探测：那是又一次完整的
        torch/onnxruntime 冷导入（慢机器 30s+ 静默，且超时会误报
        「分离环境未安装」）。组件真缺失由子进程失败带 traceback。"""
        lines = ["done:" + str(tmp_path / "song_人声.wav")]
        sep, _, _, _ = _separator(tmp_path, monkeypatch, lines)
        calls = []
        monkeypatch.setattr(
            sep, "available", lambda: calls.append(1) or True
        )
        sep.separate(tmp_path / "song.flac", lambda *a: None, lambda: False)
        assert calls == []

    def test_missing_python_blocks_with_install_hint(self, tmp_path, monkeypatch):
        """解释器不存在时仍给出安装指引（快检即可，无需导入探测）。"""
        spawned = []
        monkeypatch.setattr(
            sep_mod.subprocess, "Popen", lambda *a, **k: spawned.append(a)
        )
        sep = StandaloneVocalSeparator(
            str(tmp_path / "nope" / "python.exe"), tmp_path / "models"
        )
        with pytest.raises(RuntimeError, match="分离环境未安装"):
            sep.separate(
                tmp_path / "song.flac", lambda *a: None, lambda: False
            )
        assert spawned == []

    def test_unresponsive_ffmpeg_fails_fast_without_spawn(
        self, tmp_path, monkeypatch
    ):
        """audio-separator 构造里的 ffmpeg 探测无超时：坏 ffmpeg 会让
        子进程永久挂起。宿主侧预检必须提前拦截且不启动子进程。"""
        python = tmp_path / "python.exe"
        python.write_bytes(b"")
        ffmpeg = tmp_path / "tools" / "ffmpeg.exe"
        ffmpeg.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg.write_bytes(b"")
        monkeypatch.setattr(sep_mod, "resolve_ffmpeg_exe", lambda: str(ffmpeg))
        monkeypatch.setattr(sep_mod, "ffmpeg_responsive", lambda exe, **k: False)
        spawned = []
        monkeypatch.setattr(
            sep_mod.subprocess,
            "Popen",
            lambda *a, **k: spawned.append(a) or _FakeProc([]),
        )
        sep = StandaloneVocalSeparator(str(python), tmp_path / "models")
        with pytest.raises(RuntimeError, match="FFmpeg 无响应"):
            sep.separate(tmp_path / "song.flac", lambda *a: None, lambda: False)
        assert spawned == []

    def test_stall_watchdog_kills_silent_child(self, tmp_path, monkeypatch):
        """子进程长时间零输出（CUDA/驱动死锁、坏 ffmpeg 卡住构造探测）
        由看门狗终止并给出指向性报错，而不是永远挂在「分离中」。"""
        monkeypatch.setattr(sep_mod, "_STALL_KILL_S", 0.5)
        monkeypatch.setattr(StandaloneVocalSeparator, "available", lambda self: True)
        python = tmp_path / "python.exe"
        python.write_bytes(b"")
        ffmpeg = tmp_path / "tools" / "ffmpeg.exe"
        ffmpeg.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg.write_bytes(b"")
        monkeypatch.setattr(sep_mod, "resolve_ffmpeg_exe", lambda: str(ffmpeg))
        monkeypatch.setattr(sep_mod, "ffmpeg_responsive", lambda exe, **k: True)
        stdout = _ManualStdout()  # 永不输出
        proc = _FakeProc([], returncode=1)
        proc.stdout = stdout
        monkeypatch.setattr(sep_mod.subprocess, "Popen", lambda *a, **k: proc)
        sep = StandaloneVocalSeparator(str(python), tmp_path / "models")
        monkeypatch.setattr(sep, "_download_missing_model_files", lambda **k: [])
        t0 = time.monotonic()
        with pytest.raises(RuntimeError, match="无响应"):
            sep.separate(tmp_path / "song.flac", lambda *a: None, lambda: False)
        assert time.monotonic() - t0 < 5  # 看门狗周期级，而非等到天荒地老
        assert proc.killed
        stdout.close()  # 释放读取线程


class TestSeparationModelPreflight:
    """ensure_separation_model：与 audio-separator 子进程同口径的体检自愈。"""

    @staticmethod
    def _write_table(models: Path, hashes):
        models.mkdir(parents=True, exist_ok=True)
        (models / "mdx_model_data.json").write_text(
            json.dumps({h: {} for h in hashes}), encoding="utf-8"
        )

    def test_healthy_model_passes_silently(self, tmp_path):
        models = tmp_path / "models"
        models.mkdir(parents=True)
        model = models / sep_mod.SEPARATION_MODEL
        model.write_bytes(b"model-bytes")
        good = sep_mod._uvr_partial_md5(model)
        self._write_table(models, [good])
        assert sep_mod.ensure_separation_model(models) == ""
        assert model.is_file()

    def test_partial_hash_matches_audio_separator_algorithm(self, tmp_path):
        """末 10MB 取样口径：小于窗口时等于全文件 MD5。"""
        import hashlib

        p = tmp_path / "m.bin"
        p.write_bytes(b"abc")
        assert (
            sep_mod._uvr_partial_md5(p)
            == hashlib.md5(b"abc").hexdigest()
        )

    def test_corrupt_model_deleted_with_note(self, tmp_path):
        models = tmp_path / "models"
        models.mkdir(parents=True)
        model = models / sep_mod.SEPARATION_MODEL
        model.write_bytes(b"truncated")
        self._write_table(models, ["ffffffffffffffffffffffffffffffff"])
        note = sep_mod.ensure_separation_model(models)
        assert "不完整" in note and "重新下载" in note
        assert not model.exists()

    def test_broken_data_table_reset_model_kept(self, tmp_path):
        """数据表损坏 → 只重置表；无有效表可查时不误删模型。"""
        models = tmp_path / "models"
        models.mkdir(parents=True)
        model = models / sep_mod.SEPARATION_MODEL
        model.write_bytes(b"maybe-good")
        (models / "mdx_model_data.json").write_text(
            "{broken-json", encoding="utf-8"
        )
        note = sep_mod.ensure_separation_model(models)
        assert "数据表" in note
        assert not (models / "mdx_model_data.json").exists()
        assert model.is_file()

    def test_missing_model_or_root_is_noop(self, tmp_path):
        assert sep_mod.ensure_separation_model(tmp_path / "models") == ""
        assert sep_mod.ensure_separation_model(None) == ""

    def test_vocal_track_fallback_in_script(self):
        """UVR 轨名兜底：脚本包含排除伴奏轨的回退逻辑。"""
        assert "nstrumental" in _SCRIPT

    def test_identity_shape(self):
        sep = StandaloneVocalSeparator("", None)
        ident = sep.identity()
        assert ident["model"].endswith(".onnx")
        assert ident["stem"] == "人声"
        assert ident["params"] == {}

    def test_callable_runtime_python_resolved_lazily(self, tmp_path, monkeypatch):
        """解释器路径惰性读取：安装完成后路径才写入设置，同一分离器
        实例的 available() 必须立即反映新值（分离环境行不再卡在未安装）。"""
        state = {"python": ""}
        probed = []

        def _fake_run(cmd, **kwargs):
            probed.append(cmd[0])
            rc = 0 if cmd[0] == state["python"] else 1

            class _C:
                returncode = rc

            return _C()

        monkeypatch.setattr(sep_mod.subprocess, "run", _fake_run)
        sep = StandaloneVocalSeparator(lambda: state["python"], None)
        assert sep.available() is False  # 未安装：路径为空
        assert probed == []  # 空路径不 spawn 子进程

        exe = tmp_path / "runtime" / "python.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
        state["python"] = str(exe)
        assert sep.available() is True
        assert probed == [str(exe)]

        # 字符串形式（旧用法）仍然可用
        sep2 = StandaloneVocalSeparator(str(exe), None)
        assert sep2.available() is True


class TestHostFirstSeparation:
    """embedded 分离编排：宿主优先，宿主未配置时回落 AI Runtime 内置分离。"""

    def _fake_host(self, available, *, busy=False, message="", fail=None):
        class _H:
            def __init__(self):
                self.calls = []

            def separation_status(self):
                return {
                    "available": available,
                    "busy": busy,
                    "model": "m",
                    "message": message,
                }

            def effective_identity(self):
                return {"model": "host-model", "stem": "人声", "params": {}}

            def separate_vocal(self, source, progress, cancel):
                self.calls.append(("host", str(source)))
                if fail:
                    raise RuntimeError(fail)
                return Path("C:/host_vocal.wav")

        return _H()

    @staticmethod
    def _fake_standalone():
        class _S:
            name = "builtin"

            def __init__(self):
                self.calls = []

            def identity(self):
                return {"model": "builtin.onnx", "stem": "人声", "params": {}}

            def available(self):
                return True

            def separate(self, source, progress, cancel):
                self.calls.append(("builtin", str(source)))
                progress("vocal", 12, "内置分离")
                return Path("C:/builtin_vocal.wav")

        return _S()

    def test_host_available_uses_host(self):
        host, sa = self._fake_host(True), self._fake_standalone()
        executor, identity, prober, follows = sep_mod.host_first_separation(
            host, sa
        )
        assert follows is True
        assert prober() is True
        assert identity() == {"model": "host-model", "stem": "人声", "params": {}}
        out = executor(Path("s.flac"), lambda *a: None, lambda: False)
        # 执行器返回 (path, identity)：缓存登记跟随实际执行者
        assert out == (
            Path("C:/host_vocal.wav"),
            {"model": "host-model", "stem": "人声", "params": {}},
        )
        assert host.calls and not sa.calls

    def test_host_unavailable_falls_back_to_builtin(self):
        host, sa = self._fake_host(False), self._fake_standalone()
        executor, identity, prober, follows = sep_mod.host_first_separation(
            host, sa
        )
        assert follows is False
        msgs = []
        out = executor(
            Path("s.flac"), lambda s, p, m: msgs.append(m), lambda: False
        )
        assert out == (
            Path("C:/builtin_vocal.wav"),
            {"model": "builtin.onnx", "stem": "人声", "params": {}},
        )
        # 一次性说明 + 后续消息持续带「内置分离」前缀（不静默换环境）
        assert any("暂不可用" in m for m in msgs)
        assert any(m.startswith("（内置分离）") for m in msgs)
        assert identity()["model"] == "builtin.onnx"
        assert prober() is True  # 内置分离可用兜底

    def test_host_busy_raises_instead_of_fallback(self):
        """宿主忙（环境是好的、只是有任务在跑）不得静默换一套 CPU
        runtime 跑 7-8 分钟——明确报错让用户稍后重试（2026-09 反馈：
        第 2 步秒级、第 4 步 7-8 分钟即此链路）。"""
        host = self._fake_host(False, busy=True, message="正在分离 a.wav")
        sa = self._fake_standalone()
        executor, _, _, _ = sep_mod.host_first_separation(host, sa)
        with pytest.raises(RuntimeError, match="工作台分离任务正在进行中"):
            executor(Path("s.flac"), lambda *a: None, lambda: False)
        assert not sa.calls  # 未回落
        assert not host.calls  # 也没调宿主分离

    def test_host_busy_without_message_still_actionable(self):
        host = self._fake_host(False, busy=True)
        sa = self._fake_standalone()
        executor, _, _, _ = sep_mod.host_first_separation(host, sa)
        with pytest.raises(RuntimeError, match="重试 AI 打轴"):
            executor(Path("s.flac"), lambda *a: None, lambda: False)
        assert not sa.calls

    def test_host_path_logs_start_and_finish(self, monkeypatch):
        """宿主分支补日志：宿主服务僵死时日志不能再只有一片空白。"""
        import strange_uta_game.backend.application.ai_timing.ailog as ailog_mod

        lines = []
        monkeypatch.setattr(
            ailog_mod, "ailog", lambda src, msg: lines.append(msg)
        )
        host = self._fake_host(True)
        executor, *_ = sep_mod.host_first_separation(host, self._fake_standalone())
        executor(Path("s.flac"), lambda *a: None, lambda: False)
        assert any("宿主人声分离开始" in m for m in lines)
        assert any("宿主人声分离完成" in m for m in lines)

    def test_host_path_logs_failure(self, monkeypatch):
        import strange_uta_game.backend.application.ai_timing.ailog as ailog_mod

        lines = []
        monkeypatch.setattr(
            ailog_mod, "ailog", lambda src, msg: lines.append(msg)
        )
        host = self._fake_host(True, fail="engine exploded")
        executor, *_ = sep_mod.host_first_separation(host, self._fake_standalone())
        with pytest.raises(RuntimeError, match="engine exploded"):
            executor(Path("s.flac"), lambda *a: None, lambda: False)
        assert any("宿主人声分离失败" in m for m in lines)

    def test_fallback_path_logged(self, monkeypatch):
        import strange_uta_game.backend.application.ai_timing.ailog as ailog_mod

        lines = []
        monkeypatch.setattr(
            ailog_mod, "ailog", lambda src, msg: lines.append(msg)
        )
        host = self._fake_host(False)
        executor, *_ = sep_mod.host_first_separation(host, self._fake_standalone())
        executor(Path("s.flac"), lambda *a: None, lambda: False)
        assert any("回落 AI Runtime 内置分离" in m for m in lines)

    def test_neither_available_reports_false(self):
        host, sa = self._fake_host(False), self._fake_standalone()
        sa.available = lambda: False
        _, _, prober, follows = sep_mod.host_first_separation(host, sa)
        assert follows is False and prober() is False

    def test_host_status_exception_treated_unavailable(self):
        class _BadHost:
            def separation_status(self):
                raise RuntimeError("boom")

            def effective_identity(self):
                return {"model": "host-model", "stem": "人声", "params": {}}

            def separate_vocal(self, *a):
                raise AssertionError("不应走到宿主分离")

        sa = self._fake_standalone()
        executor, _, prober, follows = sep_mod.host_first_separation(
            _BadHost(), sa
        )
        assert follows is False and prober() is True
        out = executor(Path("s.flac"), lambda *a: None, lambda: False)
        assert out[0] == Path("C:/builtin_vocal.wav")
        assert out[1]["model"] == "builtin.onnx"


class TestSeparationModelPredownload:
    """_download_missing_model_files：缺文件时镜像补齐到模型根，存在则跳过。

    复现 audio-separator「文件在即跳过」判据——我们预先用多源（官方 +
    gh-proxy + 代理）拉齐，避免其直连 GitHub。requests.get 被 mock。
    """

    @staticmethod
    def _fake_resp(body: bytes = b"AA" * 1000):
        class _R:
            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size=1024):
                for i in range(0, len(body), chunk_size):
                    yield body[i : i + chunk_size]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _R()

    def test_downloads_all_missing_to_root(self, tmp_path, monkeypatch):
        import requests

        monkeypatch.setattr(requests, "get", lambda *a, **k: self._fake_resp())
        models = tmp_path / "models"
        dl = sep_mod._download_missing_model_files(models, proxy="")
        assert len(dl) == 4
        for name, _ in sep_mod.SEPARATION_MODEL_FILES:
            assert (models / name).is_file()  # 直接落在模型根
        assert not [p for p in models.iterdir() if ".part" in p.name]

    def test_existing_files_skipped_no_network(self, tmp_path, monkeypatch):
        import requests

        models = tmp_path / "models"
        models.mkdir(parents=True, exist_ok=True)
        for name, _ in sep_mod.SEPARATION_MODEL_FILES:
            (models / name).write_bytes(b"present")
        calls = []
        monkeypatch.setattr(
            requests, "get", lambda *a, **k: calls.append(a) or self._fake_resp()
        )
        dl = sep_mod._download_missing_model_files(models, proxy="")
        assert dl == []
        assert calls == []

    def test_byte_progress_reported_during_download(self, tmp_path, monkeypatch):
        """大模型（~63MB）预下载期间必须有字节级进度：整文件下完才报
        一次的话，慢网络下进度条会冻结数分钟（用户侧即「卡住」）。"""
        import requests

        body = b"x" * 4096

        class _Resp:
            headers = {"content-length": str(len(body))}

            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size=1024):
                for i in range(0, len(body), 1024):
                    yield body[i : i + 1024]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
        msgs = []
        dl = sep_mod._download_missing_model_files(
            tmp_path / "models",
            progress=lambda s, p, m: msgs.append(m),
        )
        assert len(dl) == 4
        assert any("正在下载分离模型文件" in m for m in msgs)  # 文件级起始
        byte_msgs = [m for m in msgs if "：" in m and "/" in m]
        assert any("4.0KB/4.0KB" in m for m in byte_msgs)  # 字节级进行中

    def test_raw_url_gets_gh_proxy_mirror_candidates(self):
        from strange_uta_game.backend.application.ai_timing.runtime import (
            _github_mirror_candidates,
        )

        raw = (
            "https://raw.githubusercontent.com/TRvlvr/application_data/main/"
            "mdx_model_data/model_data_new.json"
        )
        cands = _github_mirror_candidates(raw)
        assert cands
        assert all("gh-proxy" in u and "raw.githubusercontent.com" in u for u in cands)

    def test_github_release_url_gets_gh_proxy_mirror_candidates(self):
        from strange_uta_game.backend.application.ai_timing.runtime import (
            _github_mirror_candidates,
        )

        cands = _github_mirror_candidates(sep_mod.SEPARATION_MODEL_URL)
        assert cands
        assert all("gh-proxy" in u and "github.com/TRvlvr" in u for u in cands)


class TestScriptCleansInstrumental:
    """G9：内置分离脚本必须清掉本次分离的非人声输出——output_dir 是
    用户音乐目录，不删会在原曲旁留几十 MB 伴奏孤儿文件。"""

    def _run_script(self, tmp_path, outputs, capsys, monkeypatch):
        import sys
        import types

        out_dir = tmp_path / "music"
        out_dir.mkdir()
        inp = tmp_path / "song.flac"
        inp.write_bytes(b"audio")
        for name in outputs:
            (out_dir / name).write_bytes(name.encode())

        audio_sep = types.ModuleType("audio_separator")
        sep_inner = types.ModuleType("audio_separator.separator")

        class _FakeSeparator:
            def __init__(self, model_file_dir=None, output_dir=None, output_format=None):
                self.output_dir = output_dir

            def load_model(self, model_filename):
                pass

            def separate(self, inp):
                return list(outputs)

        sep_inner.Separator = _FakeSeparator
        monkeypatch.setitem(sys.modules, "audio_separator", audio_sep)
        monkeypatch.setitem(sys.modules, "audio_separator.separator", sep_inner)
        monkeypatch.setattr(
            sys, "argv", ["script", str(inp), str(out_dir), str(tmp_path / "models")]
        )
        exec(_SCRIPT, {"__name__": "__main__"})
        return out_dir

    def test_instrumental_orphan_removed(self, tmp_path, capsys, monkeypatch):
        outputs = ["song_Vocals_.wav", "song_Instrumental_.wav"]
        out_dir = self._run_script(tmp_path, outputs, capsys, monkeypatch)
        assert (out_dir / "song_人声.wav").is_file(), "人声轨应归一改名保留"
        assert not (out_dir / "song_Instrumental_.wav").exists(), (
            "伴奏孤儿文件必须被清理"
        )
        assert not (out_dir / "song_Vocals_.wav").exists(), "原人声文件应已移走"
        assert "done:" in capsys.readouterr().out

    def test_no_extra_outputs_leaves_nothing_behind(self, tmp_path, capsys, monkeypatch):
        outputs = ["song_Vocals_.wav"]
        out_dir = self._run_script(tmp_path, outputs, capsys, monkeypatch)
        assert (out_dir / "song_人声.wav").is_file()
        assert [p.name for p in out_dir.iterdir()] == ["song_人声.wav"]
