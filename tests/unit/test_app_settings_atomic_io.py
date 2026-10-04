"""AppSettings 持久化健壮性：原子写盘、损坏留档、并发写竞态、路径视图。

守护（03-bug审查报告 I3 / I9 / I7 修复）：
- config.json 原子写（tmp + os.replace）：写盘中断不得留下半截文件
- 损坏 config.json 改名 ``config.json.corrupt-<ts>`` 留档，不再静默重置
- daemon 线程 set() 与 save() 并发不再因字典迭代竞态丢轮次
- 只读 ``config_paths`` 视图替代私有属性访问；``retarget_config_dir``
  原地切换存储目标并迁移共享实例缓存 key
"""

from __future__ import annotations

import json
import threading
import time

import pytest

import strange_uta_game.frontend.settings.app_settings as app_settings_module
from strange_uta_game.frontend.settings.app_settings import AppSettings


class _MemProvider:
    def load(self):
        return {}

    def save(self, data):
        pass

    def load_extra(self, key, default):
        return default

    def save_extra(self, key, data):
        pass


class TestAtomicSave:
    def test_successful_save_replaces_atomically(self, tmp_path):
        cfg = tmp_path / "config.json"
        s = AppSettings(config_path=str(cfg))
        s.set("ui.theme", "dark")
        s.save()
        assert json.loads(cfg.read_text(encoding="utf-8"))["ui"]["theme"] == "dark"
        # 原子替换成功后不留 tmp 文件
        assert not (tmp_path / "config.json.tmp").exists()

    def test_failed_dump_leaves_config_intact(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        s = AppSettings(config_path=str(cfg))
        s.set("ui.theme", "dark")
        s.save()
        original = cfg.read_text(encoding="utf-8")

        def _boom(*args, **kwargs):
            raise RuntimeError("模拟写盘中断")

        monkeypatch.setattr(app_settings_module.json, "dump", _boom)
        s.set("ui.theme", "light")
        s.save()  # 吞异常不抛
        monkeypatch.undo()

        # 磁盘上的 config.json 仍是旧的完整内容（原子替换未发生）
        assert cfg.read_text(encoding="utf-8") == original
        s.reload()
        assert s.get("ui.theme") == "dark"


class TestCorruptConfigArchive:
    def test_corrupt_config_renamed_and_defaults_used(self, tmp_path):
        cfg = tmp_path / "config.json"
        corrupt = '{"ui": {"theme": "dar'
        cfg.write_text(corrupt, encoding="utf-8")
        s = AppSettings(config_path=str(cfg))
        # 半截 JSON 解析失败 → 改名留档，本次用内嵌默认值
        archives = list(tmp_path.glob("config.json.corrupt-*"))
        assert len(archives) == 1
        assert archives[0].read_text(encoding="utf-8") == corrupt
        assert s.get("ui.theme") == "auto"
        # 随后的词典版本升级等路径可能以默认值重建 config.json，
        # 但绝不会把半截坏内容当成有效设置：重建文件必须是合法 JSON
        if cfg.exists():
            assert json.loads(cfg.read_text(encoding="utf-8"))["ui"]["theme"] == "auto"


class TestConcurrentSetSave:
    def test_set_during_save_does_not_lose_round(self, tmp_path):
        """daemon 线程 set() 与 save() 并发：旧实现 json.dump 迭代嵌套 dict
        期间字典被改 → RuntimeError 被吞（该轮设置丢失）；修复后统一 _io_lock。"""
        cfg = tmp_path / "config.json"
        s = AppSettings(config_path=str(cfg))
        s.set("stress.init", 0)
        s.save()

        errors: list[Exception] = []
        stop = threading.Event()

        def _writer(worker_id: int) -> None:
            i = 0
            while not stop.is_set():
                try:
                    # 键名有界（%50），字典不无限增长；i % 50 == 0 时是顶层
                    # 新键插入，恰好构成对迭代中字典的修改
                    s.set(f"stress.w{worker_id}.{i % 50}", i)
                    s.save()
                except Exception as e:
                    errors.append(e)
                    return
                i += 1

        threads = [
            threading.Thread(target=_writer, args=(w,), daemon=True)
            for w in range(3)
        ]
        for t in threads:
            t.start()
        time.sleep(0.5)
        stop.set()
        for t in threads:
            t.join(timeout=10)
        assert errors == []
        # 磁盘内容始终是合法 JSON
        disk = json.loads(cfg.read_text(encoding="utf-8"))
        assert disk["stress"]["init"] == 0


class TestConfigPathsView:
    def test_standalone_paths(self, tmp_path):
        cfg = tmp_path / "config.json"
        s = AppSettings(config_path=str(cfg))
        cp = s.config_paths
        assert cp.config == cfg
        assert cp.dictionary == tmp_path / "dictionary.json"
        assert cp.network_dictionary == tmp_path / "network_dictionary.json"
        assert cp.singers == tmp_path / "singers.json"
        # NamedTuple 只读
        with pytest.raises(AttributeError):
            cp.config = None

    def test_provider_mode_all_none(self):
        s = AppSettings(provider=_MemProvider())
        assert s.config_paths.config is None
        assert s.config_paths.dictionary is None


class TestRetargetConfigDir:
    def test_retarget_updates_paths_and_cache_key(self, tmp_path):
        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()
        key1 = ("file", str(d1 / "config.json"))
        key2 = ("file", str(d2 / "config.json"))
        AppSettings._shared_instances.pop(key1, None)
        AppSettings._shared_instances.pop(key2, None)
        try:
            s = AppSettings(config_path=str(d1 / "config.json"))
            assert AppSettings._shared_instances[key1][1] is s

            s.retarget_config_dir(d2)

            assert s.config_paths.config == d2 / "config.json"
            assert s.config_paths.dictionary == d2 / "dictionary.json"
            # 旧 key 移除、新 key 指向同一实例：之后 AppSettings() 命中同一
            # 实例，不产生新旧两个内存态互相覆盖
            assert key1 not in AppSettings._shared_instances
            assert AppSettings._shared_instances[key2][1] is s
            assert AppSettings(config_path=str(d2 / "config.json")) is s
        finally:
            AppSettings._shared_instances.pop(key1, None)
            AppSettings._shared_instances.pop(key2, None)
