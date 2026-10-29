"""按大小滚动的 JSONL 文件。

每写一行都 fsync：Worker 被 OOM 杀掉时 1.2.3 的守护进程直接退出、不再拉起，插件 Runner 随之结束，
没落盘的样本就丢了，而 OOM 前最后几分钟恰恰最有用。写入是同步阻塞调用，调用方要放进线程里执行。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple

import json
import os
import re


class RotatingJsonl:
    """stem.jsonl 写满 max_bytes 后依次改名为 stem.1.jsonl、stem.2.jsonl……，最多保留 backups 个旧文件。"""

    def __init__(self, directory: Path, stem: str, max_bytes: int, backups: int) -> None:
        if max_bytes <= 0 or backups < 0:
            raise ValueError(f"max_bytes 必须为正、backups 不能为负: {max_bytes}, {backups}")
        self.directory = directory
        self.stem = stem
        self.max_bytes = max_bytes
        self.backups = backups
        self.path = directory / f"{stem}.jsonl"
        # 首次写入前、以及任何一次写入失败后，要先确认文件以换行结尾。
        self._tail_unchecked = True

    def append(self, record: Dict[str, Any]) -> None:
        data = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size + len(data) > self.max_bytes:
            self._rotate()
        try:
            with self.path.open("ab") as handle:
                if self._tail_unchecked:
                    self._terminate_torn_line(handle)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            self._tail_unchecked = True
            raise
        self._tail_unchecked = False

    def _terminate_torn_line(self, handle: Any) -> None:
        # 磁盘写满或断电会留下没有换行的半行；不先补换行，下一条记录会接在半行后面，两条一起坏掉。
        if self.path.stat().st_size == 0:
            return
        with self.path.open("rb") as reader:
            reader.seek(-1, os.SEEK_END)
            if reader.read(1) != b"\n":
                handle.write(b"\n")

    def _backup(self, index: int) -> Path:
        return self.directory / f"{self.stem}.{index}.jsonl"

    def _rotate(self) -> None:
        if self.backups == 0:
            self.path.unlink()
            return
        # 从最低的空位往下挪，而不是每次先删最旧的：Windows 上被别的句柄占着的文件改名会失败（例如正在打包），
        # 上一次轮转停在半路时留下的空位会在重试时补上，不会每重试一次就多删一代备份。
        gap = next((index for index in range(1, self.backups + 1) if not self._backup(index).exists()), None)
        if gap is None:
            self._backup(self.backups).unlink()
            gap = self.backups
        for index in range(gap - 1, 0, -1):
            os.replace(self._backup(index), self._backup(index + 1))
        os.replace(self.path, self._backup(1))

    def remove_excess_backups(self) -> Tuple[List[Path], List[Tuple[Path, OSError]]]:
        """删除编号超过 backups 的旧文件（把 backups 调小之后留下的），返回 (删掉的, 删不掉的及原因)。

        删不掉（Windows 上正被别的程序打开）只多占磁盘，不影响采集，交给调用方记日志，下次启动再删。
        """
        if not self.directory.is_dir():
            return [], []
        pattern = re.compile(rf"{re.escape(self.stem)}\.(\d+)\.jsonl")
        removed: List[Path] = []
        failed: List[Tuple[Path, OSError]] = []
        for path in sorted(self.directory.iterdir()):
            match = pattern.fullmatch(path.name)
            if not match or int(match.group(1)) <= self.backups:
                continue
            try:
                path.unlink()
            except OSError as exc:
                failed.append((path, exc))
                continue
            removed.append(path)
        return removed, failed

    def files(self) -> List[Path]:
        """现存文件，从旧到新。"""
        olds = [self._backup(index) for index in range(self.backups, 0, -1)]
        return [path for path in [*olds, self.path] if path.exists()]
