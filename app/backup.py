"""运维备份：对运行中的 SQLite 库做在线一致快照并原子发布到备份目录。

流程（任一步失败都不得在备份目录留下可见半成品）：

1. 通过 SQLite 在线备份接口（sqlite3.Connection.backup）把运行中的库复制到
   备份目录下的临时文件——该接口允许在其他连接并发读写时取得事务一致视图，
   无需停服；
2. 关闭源/目标连接后打开临时文件，执行 PRAGMA integrity_check 与
   foreign_key_check，并确认五张业务表齐全，保证备份库可读、可恢复；
3. 用 os.link 在同目录把临时文件硬链接为最终文件名——硬链接在目标名已
   存在时原子失败（FileExistsError），绝不覆盖既有快照；撞名时换新随机
   后缀重试，成功后再解除临时名并 fsync 目录确保元数据落盘。发布前最终
   文件名在目录中不可见，发布后内容完整可见。

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

# 发布撞名（同秒时间戳 + 随机后缀恰好与既有文件同名）时的换名重试上限。
# 每次重试都重新取随机后缀；8 位 hex 下单次撞名概率约 1/2^32，该上限在
# 实际中不会触及，仅用于在异常情况下有界失败而非无限重试。
PUBLISH_MAX_ATTEMPTS = 8


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
    """fsync 目录，确保 link/unlink 元数据落盘（POSIX）；失败不影响原子性本身。"""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish_exclusive(directory: str, tmp_path: str) -> str:
    """把本次临时文件以"绝不覆盖"的方式发布到目录，返回实际发布的文件名。

    用同目录硬链接（os.link）发布：同文件系统上 link 是原子操作，目标名已
    存在时内核直接返回 FileExistsError，不存在则把临时文件以最终名暴露——
    既不会像 os.replace 那样静默替换旧快照，也能在多请求并发时让"恰好
    同名"的两个发布各得其所：先到者占名，后到者捕获 FileExistsError 后换
    新随机名重试。发布成功才解除临时名（临时名是本次独占的，删除它不影响
    其他请求的文件，也不影响刚发布的硬链接）。
    """
    last_exc: OSError | None = None
    for _ in range(PUBLISH_MAX_ATTEMPTS):
        final_name = _final_name()
        final_path = os.path.join(directory, final_name)
        try:
            os.link(tmp_path, final_path)
        except FileExistsError as exc:
            # 同名快照已存在（并发请求或历史文件）：换名重试，绝不覆盖。
            last_exc = exc
            continue
        # 链接成功：最终文件已发布。临时名仅指向本次文件，解除它不会
        # 删除最终硬链接；极端情况下 unlink 失败也不改变已发布的事实。
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return final_name
    raise BackupError(
        f"could not publish backup after {PUBLISH_MAX_ATTEMPTS} name attempts"
    ) from last_exc


def create_backup() -> dict[str, str | int]:
    """生成一份可恢复快照并原子发布。

    返回 {"filename", "size_bytes", "sha256"}，其中 filename 是本次请求
    实际发布到的文件名（撞名重试后可能与初始候选不同）；临时文件在校验前
    不可见，发布绝不覆盖任何既有备份。任一步失败抛出 BackupError，只删除
    本次临时文件，备份目录中不会留下可见半成品，其他请求的备份不受影响。
    """
    source_path = db_path()
    if source_path == ":memory:":
        raise BackupError("online backup does not support :memory: databases")

    directory = backup_dir()
    os.makedirs(directory, exist_ok=True)

    # 临时文件与最终文件同目录：保证 os.link 是同文件系统的原子操作。
    tmp_path = os.path.join(directory, f"{_TMP_PREFIX}{uuid.uuid4().hex}{_TMP_SUFFIX}")
    published_name: str | None = None
    try:
        _online_copy(source_path, tmp_path)
        _verify_backup(tmp_path)
        size, checksum = _file_sha256(tmp_path)
        published_name = _publish_exclusive(directory, tmp_path)
        try:
            _fsync_dir(directory)
        except OSError:
            pass  # link/unlink 已原子完成；目录 fsync 仅增强崩溃耐久性
    except BackupError:
        # 校验/发布已给出具体原因：清理本次临时文件后原样抛出。
        # 发布成功后 _publish_exclusive 已解除临时名，这里的 exists 检查
        # 自然跳过；其他请求的备份一律不动。
        if published_name is None and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise
    except (sqlite3.Error, OSError) as exc:
        # 复制/校验查询或落盘失败：统一转为 BackupError，由路由层返回 500。
        if published_name is None and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass  # 临时文件已无法删除时也不改变原始失败语义
        raise BackupError(str(exc)) from exc
    return {"filename": published_name, "size_bytes": size, "sha256": checksum}
