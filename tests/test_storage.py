import json
import os
from pathlib import Path

import pytest

from memdiag.storage import RotatingJsonl


def read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_append_writes_one_json_object_per_line(tmp_path):
    writer = RotatingJsonl(tmp_path / "out", "fast", max_bytes=1024, backups=2)
    writer.append({"seq": 1, "名称": "样本"})
    writer.append({"seq": 2})
    assert read_lines(tmp_path / "out" / "fast.jsonl") == [{"seq": 1, "名称": "样本"}, {"seq": 2}]


def test_rotation_keeps_newest_records_and_drops_oldest_backup(tmp_path):
    writer = RotatingJsonl(tmp_path, "fast", max_bytes=40, backups=2)
    for seq in range(6):
        # 每行约 20 字节，两行写满一个文件。
        writer.append({"seq": seq, "pad": "xx"})
    files = writer.files()
    assert [path.name for path in files] == ["fast.2.jsonl", "fast.1.jsonl", "fast.jsonl"]
    seqs = [record["seq"] for path in files for record in read_lines(path)]
    assert seqs == sorted(seqs)
    assert seqs[-1] == 5
    assert 0 not in seqs


def test_zero_backups_discards_full_file(tmp_path):
    writer = RotatingJsonl(tmp_path, "trend", max_bytes=30, backups=0)
    for seq in range(4):
        writer.append({"seq": seq, "pad": "xx"})
    assert writer.files() == [tmp_path / "trend.jsonl"]
    assert read_lines(tmp_path / "trend.jsonl")[-1]["seq"] == 3


def test_invalid_limits_are_rejected(tmp_path):
    with pytest.raises(ValueError):
        RotatingJsonl(tmp_path, "x", max_bytes=0, backups=1)


def test_first_append_isolates_a_torn_line(tmp_path):
    # 磁盘写满或断电留下没有换行的半行；新记录必须另起一行，不能接在半行后面一起坏掉。
    path = tmp_path / "trend.jsonl"
    path.write_bytes(b'{"seq":1}\n{"seq":2,"win')
    writer = RotatingJsonl(tmp_path, "trend", max_bytes=1024, backups=1)
    writer.append({"seq": 3})
    writer.append({"seq": 4})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines == ['{"seq":1}', '{"seq":2,"win', '{"seq":3}', '{"seq":4}']


def test_reopening_a_cleanly_terminated_file_adds_no_blank_line(tmp_path):
    # 插件每次重启都新建写入器；文件本来就以换行结尾时不能再补一个，否则每次重启都多一行空行、被当成坏行。
    RotatingJsonl(tmp_path, "trend", max_bytes=1024, backups=1).append({"seq": 1})
    RotatingJsonl(tmp_path, "trend", max_bytes=1024, backups=1).append({"seq": 2})
    assert (tmp_path / "trend.jsonl").read_bytes() == b'{"seq":1}\n{"seq":2}\n'


def test_failed_write_rechecks_the_tail(tmp_path, monkeypatch):
    writer = RotatingJsonl(tmp_path, "trend", max_bytes=1024, backups=1)
    writer.append({"seq": 1})
    assert writer._tail_unchecked is False
    monkeypatch.setattr("memdiag.storage.os.fsync", lambda fd: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    with pytest.raises(OSError):
        writer.append({"seq": 2})
    assert writer._tail_unchecked is True


@pytest.mark.parametrize("held", ["fast.jsonl", "fast.1.jsonl"])
def test_failed_rotation_resumes_without_dropping_another_backup(tmp_path, monkeypatch, held):
    # Windows 上被别的句柄占着的文件改名会失败（例如 /memdiag pack 正在读它）。重试时只能补上次留下的空位，
    # 不能再删一代：最终只丢掉本来就该丢的最旧一条。
    writer = RotatingJsonl(tmp_path, "fast", max_bytes=25, backups=3)
    for seq in range(4):
        # 每行 21 字节，一个文件只放得下一行。
        writer.append({"seq": seq, "pad": "xx"})
    real_replace = os.replace
    failures = []

    def replace_once_blocked(source, target):
        if os.path.basename(source) == held and not failures:
            failures.append(source)
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        real_replace(source, target)

    monkeypatch.setattr("memdiag.storage.os.replace", replace_once_blocked)
    with pytest.raises(PermissionError):
        writer.append({"seq": 4, "pad": "xx"})
    writer.append({"seq": 4, "pad": "xx"})
    assert failures
    assert [record["seq"] for path in writer.files() for record in read_lines(path)] == [1, 2, 3, 4]
    assert [path.name for path in writer.files()] == ["fast.3.jsonl", "fast.2.jsonl", "fast.1.jsonl", "fast.jsonl"]


def test_remove_excess_backups_after_lowering_the_limit(tmp_path):
    big = RotatingJsonl(tmp_path, "fast", max_bytes=20, backups=6)
    for seq in range(14):
        big.append({"seq": seq, "p": "x"})
    # 名字相近的其他文件（别的 stem、手工备份）不是本写入器的备份，不能删。
    for name in ("trend.9.jsonl", "fast.9.jsonl.bak", "xfast.9.jsonl"):
        (tmp_path / name).write_bytes(b"{}\n")
    assert [path.name for path in big.files()][0] == "fast.6.jsonl"
    small = RotatingJsonl(tmp_path, "fast", max_bytes=20, backups=2)
    removed, failed = small.remove_excess_backups()
    assert [path.name for path in removed] == [f"fast.{index}.jsonl" for index in range(3, 7)]
    assert failed == []
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "fast.1.jsonl", "fast.2.jsonl", "fast.9.jsonl.bak", "fast.jsonl", "trend.9.jsonl", "xfast.9.jsonl",
    ]
    assert RotatingJsonl(tmp_path / "missing", "fast", max_bytes=20, backups=1).remove_excess_backups() == ([], [])


def test_remove_excess_backups_reports_files_it_cannot_delete(tmp_path, monkeypatch):
    for name in ("fast.jsonl", "fast.1.jsonl", "fast.2.jsonl", "fast.3.jsonl"):
        (tmp_path / name).write_bytes(b"{}\n")
    real_unlink = Path.unlink

    def unlink(self, *args, **kwargs):
        if self.name == "fast.2.jsonl":
            raise PermissionError(13, "being used by another process")
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    removed, failed = RotatingJsonl(tmp_path, "fast", max_bytes=20, backups=1).remove_excess_backups()
    assert [path.name for path in removed] == ["fast.3.jsonl"]
    assert [(path.name, type(exc)) for path, exc in failed] == [("fast.2.jsonl", PermissionError)]
    assert (tmp_path / "fast.2.jsonl").exists() and not (tmp_path / "fast.3.jsonl").exists()
