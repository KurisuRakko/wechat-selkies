"""One-shot Linux WeChat key extraction for the single allowed account.

The /proc scanning and SQLCipher candidate verification are adapted from
huohuoer/wechat-cli commit a3789232. Unlike the upstream CLI, this module never
prints keys, salts, memory addresses, or absolute database paths.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Callable, Iterable

from .constants import (
    KEY_SCHEMA_VERSION,
    KEYS_FILE,
    TARGET_ACCOUNT_DIR,
    TARGET_ACCOUNT_MASK,
    TARGET_DB_DIR,
    UPSTREAM_COMMIT,
    is_allowed_database,
)
from .crypto import PAGE_SIZE, verify_encryption_key
from .errors import HistoryError, fail


_HEX_PATTERN = re.compile(rb"x'([0-9a-fA-F]{64,192})'")
_KNOWN_PROCESS_NAMES = {"wechat", "wechatappex", "weixin"}
_INTERPRETER_PREFIXES = ("python", "bash", "sh", "zsh", "node", "perl", "ruby")
_SKIP_MAPPINGS = {"[vdso]", "[vsyscall]", "[vvar]"}
_SKIP_LIBRARY_PREFIXES = ("/usr/lib/", "/lib/", "/usr/share/")
_CHUNK_SIZE = 4 * 1024 * 1024
_CHUNK_OVERLAP = 256


@dataclass(frozen=True, slots=True)
class DatabasePage:
    relative_path: str
    size: int
    salt_hex: str
    first_page: bytes


def _safe_readlink(path: Path) -> str:
    try:
        return os.path.realpath(os.readlink(path))
    except OSError:
        return ""


def _is_wechat_process(pid: int) -> bool:
    if pid == os.getpid():
        return False
    try:
        comm = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip().lower()
        if comm in _KNOWN_PROCESS_NAMES:
            return True
        executable = Path(_safe_readlink(Path(f"/proc/{pid}/exe"))).name.lower()
        if any(executable.startswith(prefix) for prefix in _INTERPRETER_PREFIXES):
            return False
        return "wechat" in executable or "weixin" in executable
    except (OSError, ValueError):
        return False


def process_start_ticks(pid: int) -> int:
    """Read Linux /proc start ticks without depending on ps output."""

    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields_after_comm = raw[raw.rfind(")") + 2 :].split()
        return int(fields_after_comm[19])
    except (OSError, ValueError, IndexError) as exc:
        raise fail("WECHAT_NOT_RUNNING", "无法确认微信进程启动时间") from exc


def find_wechat_processes() -> list[int]:
    candidates: list[tuple[int, int]] = []
    page_size = os.sysconf("SC_PAGE_SIZE")
    for item in Path("/proc").iterdir():
        if not item.name.isdigit():
            continue
        pid = int(item.name)
        if not _is_wechat_process(pid):
            continue
        try:
            resident_pages = int((item / "statm").read_text().split()[1])
        except (OSError, ValueError, IndexError):
            continue
        candidates.append((pid, resident_pages * page_size))
    candidates.sort(key=lambda value: value[1], reverse=True)
    if not candidates:
        raise fail("WECHAT_NOT_RUNNING", "没有检测到运行中的 Linux 微信进程")
    return [pid for pid, _ in candidates]


def _effective_capabilities() -> int:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("CapEff:"):
                return int(line.split(":", 1)[1].strip(), 16)
    except (OSError, ValueError):
        pass
    return 0


def require_ptrace_capability() -> None:
    if not (_effective_capabilities() & (1 << 19)):
        raise fail(
            "PTRACE_DENIED",
            "密钥扫描器缺少 SYS_PTRACE；不要把该权限加到主微信容器",
        )


def require_target_account_active(database_dir: Path = TARGET_DB_DIR) -> None:
    """Fail closed unless the fixed account has the newest login marker."""

    source_root = database_dir.parent.parent
    account_times: dict[str, int] = {}
    try:
        for account in source_root.glob("wxid_*"):
            if not account.is_dir():
                continue
            markers = (
                account / "config" / "login_configv2",
                account / "config" / "login_config",
            )
            times = [marker.stat().st_mtime_ns for marker in markers if marker.is_file()]
            if times:
                account_times[account.name] = max(times)
    except OSError as exc:
        raise fail("TARGET_ACCOUNT_UNCONFIRMED", "无法确认当前微信账户") from exc

    target_time = account_times.get(TARGET_ACCOUNT_DIR)
    if target_time is None or any(
        name != TARGET_ACCOUNT_DIR and timestamp >= target_time
        for name, timestamp in account_times.items()
    ):
        raise fail(
            "TARGET_ACCOUNT_NOT_ACTIVE",
            "目标旧账户当前未登录；请手动切换到该账户后再扫描",
        )


def collect_target_databases(database_dir: Path = TARGET_DB_DIR) -> list[DatabasePage]:
    """Collect only contact/session/message databases from the fixed account."""

    if database_dir.parent.name != TARGET_ACCOUNT_DIR:
        raise fail("ACCOUNT_MISMATCH", "数据库目录不是允许的旧账户")
    if not database_dir.is_dir():
        raise fail("DB_NOT_FOUND", "目标旧账户数据库目录不存在")

    records: list[DatabasePage] = []
    for path in sorted(database_dir.rglob("*.db")):
        try:
            relative_path = path.relative_to(database_dir).as_posix()
        except ValueError:
            continue
        if not is_allowed_database(relative_path) or path.is_symlink():
            continue
        try:
            size = path.stat().st_size
            if size < PAGE_SIZE:
                continue
            with path.open("rb") as handle:
                first_page = handle.read(PAGE_SIZE)
        except OSError:
            continue
        if len(first_page) != PAGE_SIZE:
            continue
        records.append(
            DatabasePage(
                relative_path=relative_path,
                size=size,
                salt_hex=first_page[:16].hex(),
                first_page=first_page,
            )
        )

    paths = {record.relative_path for record in records}
    if "contact/contact.db" not in paths or "session/session.db" not in paths:
        raise fail("DB_NOT_FOUND", "目标账户缺少联系人或会话数据库")
    if not any(path.startswith("message/message_") for path in paths):
        raise fail("DB_NOT_FOUND", "目标账户缺少消息数据库")
    return records


def _readable_regions(pid: int) -> list[tuple[int, int]]:
    regions: list[tuple[int, int]] = []
    try:
        lines = Path(f"/proc/{pid}/maps").read_text().splitlines()
    except PermissionError as exc:
        raise fail("PTRACE_DENIED", "无法读取微信进程内存映射") from exc
    except OSError as exc:
        raise fail("WECHAT_NOT_RUNNING", "微信进程已退出") from exc

    for line in lines:
        parts = line.split()
        if len(parts) < 2 or "r" not in parts[1]:
            continue
        mapping_name = parts[5] if len(parts) >= 6 else ""
        if mapping_name in _SKIP_MAPPINGS:
            continue
        lowered = mapping_name.lower()
        if (
            mapping_name.startswith(_SKIP_LIBRARY_PREFIXES)
            and "wcdb" not in lowered
            and "wechat" not in lowered
            and "weixin" not in lowered
        ):
            continue
        try:
            start_text, end_text = parts[0].split("-", 1)
            start, end = int(start_text, 16), int(end_text, 16)
        except (ValueError, IndexError):
            continue
        size = end - start
        if 0 < size < 500 * 1024 * 1024:
            regions.append((start, size))
    return regions


def _candidate_key(hex_bytes: bytes) -> bytes | None:
    try:
        text = hex_bytes.decode("ascii")
        if len(text) < 64 or len(text) % 2:
            return None
        return bytes.fromhex(text[:64])
    except (UnicodeDecodeError, ValueError):
        return None


def _match_candidates(
    data: bytes,
    pages_by_salt: dict[str, list[DatabasePage]],
    keys_by_salt: dict[str, bytes],
    seen_candidates: set[bytes],
) -> int:
    matches = 0
    for match in _HEX_PATTERN.finditer(data):
        matches += 1
        candidate = _candidate_key(match.group(1))
        if candidate is None:
            continue
        candidate_id = hashlib.sha256(candidate).digest()
        if candidate_id in seen_candidates:
            continue
        seen_candidates.add(candidate_id)
        for salt_hex, pages in pages_by_salt.items():
            if salt_hex in keys_by_salt:
                continue
            if verify_encryption_key(candidate, pages[0].first_page):
                keys_by_salt[salt_hex] = candidate
    return matches


def _scan_region(
    memory: BinaryIO,
    start: int,
    size: int,
    pages_by_salt: dict[str, list[DatabasePage]],
    keys_by_salt: dict[str, bytes],
    seen_candidates: set[bytes],
) -> tuple[int, int]:
    scanned = 0
    matches = 0
    carry = b""
    while scanned < size:
        amount = min(_CHUNK_SIZE, size - scanned)
        try:
            memory.seek(start + scanned)
            chunk = memory.read(amount)
        except (OSError, ValueError):
            break
        if not chunk:
            break
        combined = carry + chunk
        matches += _match_candidates(
            combined, pages_by_salt, keys_by_salt, seen_candidates
        )
        carry = combined[-_CHUNK_OVERLAP:]
        scanned += len(chunk)
    return scanned, matches


def scan_processes_for_keys(
    records: list[DatabasePage],
    process_ids: Iterable[int],
    progress: Callable[[str], None] = print,
) -> tuple[dict[str, bytes], int, int]:
    pages_by_salt: dict[str, list[DatabasePage]] = {}
    for record in records:
        pages_by_salt.setdefault(record.salt_hex, []).append(record)
    keys_by_salt: dict[str, bytes] = {}
    seen_candidates: set[bytes] = set()
    total_scanned = 0
    total_matches = 0
    selected_pid = 0

    for pid in process_ids:
        if not _is_wechat_process(pid):
            continue
        regions = _readable_regions(pid)
        try:
            memory = open(f"/proc/{pid}/mem", "rb", buffering=0)
        except PermissionError as exc:
            raise fail("PTRACE_DENIED", "无法打开微信进程内存") from exc
        except OSError:
            continue
        selected_pid = pid
        progress(f"正在扫描微信进程 PID={pid}，目标数据库 {len(records)} 个")
        last_progress = total_scanned
        try:
            for start, size in regions:
                scanned, matches = _scan_region(
                    memory,
                    start,
                    size,
                    pages_by_salt,
                    keys_by_salt,
                    seen_candidates,
                )
                total_scanned += scanned
                total_matches += matches
                if total_scanned - last_progress >= 512 * 1024 * 1024:
                    progress(
                        f"已扫描 {total_scanned // 1024 // 1024} MiB，"
                        f"已验证 {len(keys_by_salt)}/{len(pages_by_salt)} 组密钥"
                    )
                    last_progress = total_scanned
                if len(keys_by_salt) == len(pages_by_salt):
                    break
        finally:
            memory.close()
        if len(keys_by_salt) == len(pages_by_salt):
            break

    if not selected_pid:
        raise fail("WECHAT_NOT_RUNNING", "没有可扫描的微信主进程")
    if not keys_by_salt:
        raise fail(
            "KEY_NOT_FOUND",
            "未找到目标旧账户密钥；请先切换并保持该账户登录后重试",
        )
    return keys_by_salt, selected_pid, total_matches


def _build_key_document(
    records: list[DatabasePage], keys_by_salt: dict[str, bytes], pid: int
) -> dict:
    result: dict[str, object] = {
        "_meta": {
            "schema_version": KEY_SCHEMA_VERSION,
            "target_account_dir": TARGET_ACCOUNT_DIR,
            "target_account_mask": TARGET_ACCOUNT_MASK,
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "wechat_pid": pid,
            "wechat_start_ticks": process_start_ticks(pid),
            "upstream_commit": UPSTREAM_COMMIT,
        }
    }
    for record in records:
        key = keys_by_salt.get(record.salt_hex)
        if key is None:
            continue
        result[record.relative_path] = {
            "enc_key": key.hex(),
            "salt": record.salt_hex,
            "size": record.size,
        }
    return result


def _validate_minimum_keys(document: dict) -> None:
    paths = {key for key in document if not key.startswith("_")}
    if "contact/contact.db" not in paths or "session/session.db" not in paths:
        raise fail(
            "KEY_INCOMPLETE",
            "已找到部分密钥，但缺少联系人或会话数据库密钥",
        )
    if not any(path.startswith("message/message_") for path in paths):
        raise fail("KEY_INCOMPLETE", "已找到部分密钥，但缺少消息数据库密钥")


def save_key_document(document: dict, destination: Path = KEYS_FILE) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.parent.chmod(0o700)
    old_umask = os.umask(0o077)
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=".keys-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(document, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, destination)
        destination.chmod(0o600)
        _set_key_owner(destination)
    finally:
        os.umask(old_umask)
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


def prepare_key_directory(destination: Path = KEYS_FILE) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.parent.chmod(0o700)
    _set_key_owner(destination.parent)


def _set_key_owner(path: Path) -> None:
    """Give the unprivileged MCP user access while retaining 0700/0600 modes."""

    if os.name != "posix" or os.geteuid() != 0:
        return
    try:
        uid = int(os.environ.get("WECHAT_HISTORY_OWNER_UID", "1000"))
        gid = int(os.environ.get("WECHAT_HISTORY_OWNER_GID", "100"))
        if uid < 0 or gid < 0:
            raise ValueError
        os.chown(path, uid, gid)
    except (OSError, ValueError) as exc:
        raise fail("KEY_PERMISSIONS", "无法设置私有密钥目录的所有者") from exc


def extract_and_save(
    destination: Path = KEYS_FILE, pid: int | None = None
) -> dict:
    require_ptrace_capability()
    prepare_key_directory(destination)
    require_target_account_active()
    records = collect_target_databases()
    process_ids = [pid] if pid is not None else find_wechat_processes()
    started = time.monotonic()
    keys_by_salt, selected_pid, patterns = scan_processes_for_keys(
        records, process_ids
    )
    document = _build_key_document(records, keys_by_salt, selected_pid)
    _validate_minimum_keys(document)
    save_key_document(document, destination)
    matched = len(document) - 1
    print(
        f"密钥扫描完成：保存 {matched}/{len(records)} 个目标数据库密钥，"
        f"匹配模式 {patterns} 个，耗时 {time.monotonic() - started:.1f}s"
    )
    print("密钥已安全保存；日志中未输出密钥、salt 或数据库路径")
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="扫描固定旧账户的微信数据库密钥")
    parser.add_argument("--pid", type=int, default=None, help="指定微信 PID")
    args = parser.parse_args(argv)
    try:
        extract_and_save(pid=args.pid)
        return 0
    except HistoryError as exc:
        print(f"ERROR {exc.code}: {exc.safe_message}", file=sys.stderr)
        return 2
    except Exception as exc:  # deliberately suppress exception text/path
        print(f"ERROR INTERNAL: {type(exc).__name__}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
