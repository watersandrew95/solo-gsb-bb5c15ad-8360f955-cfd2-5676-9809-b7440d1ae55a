"""运维备份：对运行中的 SQLite 库做在线一致快照并原子发布到备份目录。

流程（任一步失败都不得在备份目录留下可见半成品）：

1. 通过 SQLite 在线备份接口（sqlite3.Connection.backup）把运行中的库复制到
   备份目录下的临时文件——该接口允许在其他连接并发读写时取得事务一致视图，
   无需停服；
2. 关闭源/目标连接后打开临时文件，执行 PRAGMA integrity_check 与
   foreign_key_check，并确认五张业务表齐全，保证备份库可读、可恢复；
3. 用同目录硬链接（os.link）把临时文件原子发布为最终文件名：硬链接是
   内核级“目标不存在才创建目录项”的操作，目标名已存在时返回 EEXIST 而
   非覆盖——此时重新生成文件名重试。因此即使两次（含并发的）备份碰巧
   取得同名，后一次也只会换名发布，任何已存在备份的文件名与内容都不会
   被改写。发布成功后删除本次临时文件（同一 inode 的旧名字）并 fsync
   目录确保目录项落盘。

最终文件位于 IMD_BACKUP_DIR（容器内默认 /data/backups，与数据库同处持久卷；
本地开发默认工作目录下的 backups/），
文件名形如 intermod-YYYYmmddTHHMMSSZ-<8 位 hex>.db。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from urllib.request import pathname2url

from .database import db_path

# 备份目录：代码默认工作目录下的 backups/（本地开发）；容器通过
# Dockerfile ENV 覆盖为 /data/backups，与数据库同在 imd-data 持久卷内。
DEFAULT_BACKUP_DIR = "backups"

# 每次在线复制分块页数：-1 表示一次复制全部。复制期间被并发写入时
# sqlite3.backup 会自行重试（SQLITE_BUSY）并保留源库读锁。
BACKUP_PAGES_PER_STEP = -1
BACKUP_SLEEP_INTERVAL_MS = 250

# 业务表：备份校验时必须全部存在，避免把空壳/错库当作可恢复快照发布。
REQUIRED_TABLES = {
    "sites",
    "carriers",
    "guard_bands",
    "analyses",
    "idempotency_keys",
}

_TMP_PREFIX = ".intermod-backup-"
_TMP_SUFFIX = ".tmp"

# 发布撞名（目标文件名已存在）时的换名重试上限：每次都重新取时间戳 +
# 新随机后缀，正常情况下首轮即成功；设上限是为在异常密集的目录中给出
# 明确失败而不是无限重试。
MAX_PUBLISH_ATTEMPTS = 8


class BackupError(RuntimeError):
    """备份流程中任一步骤失败（复制、校验、发布或落盘）。"""


def backup_dir() -> str:
    return os.environ.get("IMD_BACKUP_DIR", DEFAULT_BACKUP_DIR)


def _final_name(now: datetime | None = None) -> str:
    moment = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"intermod-{moment}-{uuid.uuid4().hex[:8]}.db"


def _online_copy(source_path: str, target_path: str) -> None:
    """用 SQLite 在线备份接口把 source_path 一致复制到 target_path。

    两个连接以 uri 只读/读写方式分别打开；目标文件为本次独占的临时文件，
    复制完成后关闭全部连接再进入校验阶段。
    """
    source_uri = "file:" + pathname2url(os.path.abspath(source_path)) + "?mode=ro"
    src = sqlite3.connect(source_uri, uri=True)
    dst = sqlite3.connect(target_path)
    try:
        src.backup(
            dst,
            pages=BACKUP_PAGES_PER_STEP,
            progress=None,
            name="main",
            sleep=BACKUP_SLEEP_INTERVAL_MS / 1000.0,
        )
    finally:
        dst.close()
        src.close()


def _verify_backup(path: str) -> None:
    """校验临时备份库完整、可恢复：integrity_check、foreign_key_check、业务表齐全。"""
    backup_uri = "file:" + pathname2url(os.path.abspath(path)) + "?mode=ro"
    con = sqlite3.connect(backup_uri, uri=True)
    try:
        try:
            integrity = con.execute("PRAGMA integrity_check").fetchall()
        except sqlite3.DatabaseError as exc:
            # 文件不是合法 SQLite 库或头部损坏
            raise BackupError(f"backup integrity_check failed: {exc}") from exc
        if integrity != [("ok",)]:
            raise BackupError(
                "backup integrity_check failed: "
                + "; ".join(str(row[0]) for row in integrity[:5])
            )
        fk_violations = con.execute("PRAGMA foreign_key_check").fetchall()
        if fk_violations:
            raise BackupError(
                f"backup foreign_key_check failed: {len(fk_violations)} violating row(s)"
            )
        tables = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        missing = REQUIRED_TABLES - tables
        if missing:
            raise BackupError(
                "backup is missing required table(s): " + ", ".join(sorted(missing))
            )
    finally:
        con.close()


def _file_sha256(path: str) -> tuple[int, str]:
    """返回文件 (字节数, SHA-256 hex)，分块读取避免大文件一次性入内存。"""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _fsync_dir(path: str) -> None:
    """fsync 目录，确保新建目录项落盘（POSIX）；失败不影响原子性本身。"""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _remove_tmp(tmp_path: str) -> None:
    """仅删除本次备份自己的临时文件。

    发布成功后临时文件名与最终文件名指向同一 inode，此处 unlink 只移除
    临时名字，已发布的最终文件保留；任何情况下都不按目录通配删除，避免
    误删其他并发请求的临时文件或已发布备份。
    """
    try:
        os.unlink(tmp_path)
    except FileNotFoundError:
        pass
    except OSError:
        # 临时文件已无法删除时不改变主流程语义（发布成功时备份本身完好）
        pass


def _publish_backup(directory: str, tmp_path: str) -> str:
    """把已校验的临时文件以不覆盖既有文件的方式原子发布，返回最终文件名。

    以 os.link（同目录硬链接）创建最终名字：link(2) 在内核中以
    “目标不存在才创建目录项”的方式原子完成，目标已存在时抛
    FileExistsError，绝不会像 os.replace 那样静默替换既有快照。撞名即
    重新取时间戳与随机后缀换名重试，至多 MAX_PUBLISH_ATTEMPTS 次。
    """
    for attempt in range(MAX_PUBLISH_ATTEMPTS):
        final_name = _final_name()
        final_path = os.path.join(directory, final_name)
        try:
            # follow_symlinks=False：若最终路径恰为符号链接也不穿透覆盖其目标
            os.link(tmp_path, final_path, follow_symlinks=False)
            return final_name
        except FileExistsError:
            if attempt + 1 == MAX_PUBLISH_ATTEMPTS:
                raise BackupError(
                    f"could not publish backup: {MAX_PUBLISH_ATTEMPTS} candidate "
                    "filenames already existed"
                )
            continue
    raise BackupError("unreachable: backup publish retry exhausted")  # pragma: no cover


def create_backup() -> dict[str, str | int]:
    """生成一份可恢复快照并原子发布。

    返回 {"filename", "size_bytes", "sha256"}——三者均描述本次请求实际
    发布的那个文件；任一步失败抛出 BackupError，仅清理本次自己的临时
    文件（不动其他请求的临时文件与已发布备份），备份目录中不会留下任何
    可见的半成品文件。
    """
    source_path = db_path()
    if source_path == ":memory:":
        raise BackupError("online backup does not support :memory: databases")

    directory = backup_dir()
    os.makedirs(directory, exist_ok=True)

    # 临时文件与最终文件同目录：保证 os.link 是同文件系统的原子目录项创建。
    tmp_path = os.path.join(directory, f"{_TMP_PREFIX}{uuid.uuid4().hex}{_TMP_SUFFIX}")
    try:
        _online_copy(source_path, tmp_path)
        _verify_backup(tmp_path)
        size, checksum = _file_sha256(tmp_path)
        final_name = _publish_backup(directory, tmp_path)
        # 硬链接发布后临时名与最终名指向同一 inode：删除临时名即“改名”完成，
        # 已发布文件（含其他请求的备份）不受影响。
        _remove_tmp(tmp_path)
        try:
            _fsync_dir(directory)
        except OSError:
            pass  # 目录项已原子创建；目录 fsync 仅增强崩溃耐久性
    except BackupError:
        # 校验/发布已给出具体原因：清理本次临时文件后原样抛出
        _remove_tmp(tmp_path)
        raise
    except (sqlite3.Error, OSError) as exc:
        # 复制/校验查询/发布或落盘失败：统一转为 BackupError，由路由层返回 500。
        _remove_tmp(tmp_path)
        raise BackupError(str(exc)) from exc
    return {"filename": final_name, "size_bytes": size, "sha256": checksum}
