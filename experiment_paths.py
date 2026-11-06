"""实验目录布局：每场实验一个文件夹，所有产物放在一起。

experiments/
  <实验名称>/
    experiment.db            OASIS SQLite（回放侧按同名 .json 找阶段配置）
    experiment.json          运行元数据与阶段记录
    status.json              实时进度
    cancel.flag              停止请求
    run.log                  worker 输出
    profile_snapshot.json    本次实验实际使用的 profile 快照
  .active.json               全局单跑锁（不属于任何一场实验）
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXPERIMENTS_DIR = ROOT / "experiments"
ACTIVE_FILE = EXPERIMENTS_DIR / ".active.json"

DB_NAME = "experiment.db"
META_NAME = "experiment.json"
STATUS_NAME = "status.json"
CANCEL_NAME = "cancel.flag"
LOG_NAME = "run.log"
SNAPSHOT_NAME = "profile_snapshot.json"

_WINDOWS_FORBIDDEN = '<>:"/\\|?*'
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def db_path(name: str) -> Path:
    return EXPERIMENTS_DIR / name / DB_NAME


def atomic_json(path: Path, obj: dict, attempts: int = 12) -> None:
    """原子写 JSON。

    Windows 上若目标文件正被另一进程打开读取（网页端每 2 秒轮询 status.json），
    os.replace 会抛 PermissionError；这类冲突存在时间极短，短暂重试即可越过。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    payload = json.dumps(obj, ensure_ascii=False, indent=2)
    for i in range(attempts):
        try:
            temp.write_text(payload, encoding="utf-8")
            os.replace(temp, path)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.02 * (i + 1))


def experiment_dir(name: str) -> Path:
    return EXPERIMENTS_DIR / name


def is_experiment_dir(path: Path) -> bool:
    return path.is_dir() and (path / DB_NAME).is_file()


def list_experiments() -> list[Path]:
    """返回含有 experiment.db 的实验目录，按名称排序。"""
    if not EXPERIMENTS_DIR.is_dir():
        return []
    return sorted((p for p in EXPERIMENTS_DIR.iterdir() if is_experiment_dir(p)),
                  key=lambda p: p.name)


def list_experiment_names() -> list[str]:
    return [p.name for p in list_experiments()]


def normalize_name(raw: object) -> str:
    """校验并清洗实验名称，使其能直接作为目录名。"""
    text = str(raw or "").strip()
    if not text:
        raise ValueError("实验名称不能为空。")
    if len(text) > 80:
        raise ValueError("实验名称过长，请控制在 80 个字符以内。")
    if any(ch in _WINDOWS_FORBIDDEN for ch in text) or any(ord(ch) < 32 for ch in text):
        raise ValueError(f"实验名称不能包含这些字符：{_WINDOWS_FORBIDDEN}")
    if text in {".", ".."} or text.endswith((".", " ")):
        raise ValueError("实验名称不能以点或空格结尾，也不能是 . 或 ..。")
    if text.split(".")[0].lower() in _WINDOWS_RESERVED:
        raise ValueError("实验名称撞上了 Windows 保留名（如 CON、NUL、COM1）。")
    cleaned = re.sub(r"\s+", "_", text)
    if any(p.name == cleaned for p in EXPERIMENTS_DIR.iterdir() if EXPERIMENTS_DIR.is_dir()):
        raise ValueError(f"实验 `{cleaned}` 已存在；请换一个名称，实验不会被覆盖。")
    return cleaned
