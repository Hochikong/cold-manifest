"""通用工具：日志、时间、路径处理、Windows 文件属性。"""

import ctypes
import logging
import os
import re
from datetime import datetime, timezone

# ---------- Windows 文件属性常量 ----------
FILE_ATTRIBUTE_READONLY = 0x1
FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_ARCHIVE = 0x20
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

_WIN32_GET_ATTRS = None


def _get_file_attributes(path: str) -> int:
    """获取 Windows 文件属性位掩码。非 Windows 返回 0。"""
    global _WIN32_GET_ATTRS
    if os.name != "nt":
        return 0
    if _WIN32_GET_ATTRS is None:
        _WIN32_GET_ATTRS = ctypes.windll.kernel32.GetFileAttributesW
        _WIN32_GET_ATTRS.argtypes = [ctypes.c_wchar_p]
        _WIN32_GET_ATTRS.restype = ctypes.c_uint32
    attrs = _WIN32_GET_ATTRS(path)
    if attrs == 0xFFFFFFFF:
        raise OSError(f"Cannot get attributes for {path}")
    return attrs


def is_hidden(path: str) -> bool:
    """检查是否为隐藏文件（Windows 属性 / POSIX 点文件）。"""
    if os.name == "nt":
        try:
            return bool(_get_file_attributes(path) & FILE_ATTRIBUTE_HIDDEN)
        except OSError:
            return False
    return os.path.basename(path).startswith(".")


def is_system(path: str) -> bool:
    """检查是否为系统文件。非 Windows 始终返回 False。"""
    if os.name != "nt":
        return False
    try:
        return bool(_get_file_attributes(path) & FILE_ATTRIBUTE_SYSTEM)
    except OSError:
        return False


def is_reparse_point(path: str) -> bool:
    """检查是否为重解析点（Junction / Symlink）。非 Windows 返回 False。"""
    if os.name != "nt":
        return False
    try:
        return bool(_get_file_attributes(path) & FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return False


def add_long_prefix(path: str) -> str:
    """Windows 长路径支持：给绝对路径加 \\\\?\\ 前缀。"""
    if os.name != "nt":
        return path
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\.\\"):
        return path
    return "\\\\?\\" + os.path.abspath(path)


def strip_long_prefix(path: str) -> str:
    """移除 \\\\?\\ 前缀，还原普通路径。"""
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


# ---------- 路径工具 ----------


def relative_path_components(root: str, full_path: str) -> list:
    """将绝对路径拆为相对于 root 的层级列表，统一使用 / 作为输出分隔。"""
    root = strip_long_prefix(root).replace("\\", "/").rstrip("/")
    full_path = strip_long_prefix(full_path).replace("\\", "/")
    rel = full_path[len(root):].lstrip("/")
    if not rel:
        return []
    return rel.split("/")


def path_depth(root: str, full_path: str) -> int:
    """返回相对路径的层级深度。"""
    parts = relative_path_components(root, full_path)
    return len(parts)


def match_glob_patterns(relative_path: str, patterns: list) -> bool:
    """检查 relative_path 是否匹配任一 glob 模式。使用 / 分隔。"""
    if not patterns:
        return False
    import fnmatch
    for p in patterns:
        if fnmatch.fnmatch(relative_path, p):
            return True
    return False


# ---------- 标识符清理 ----------


def sanitize_name(raw: str) -> str:
    """将字符串清理为安全的目录名 / 标识符。"""
    if not raw:
        return "UNKNOWN"
    raw = raw.strip()
    raw = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", raw)
    raw = raw.strip("_ .")
    return raw or "UNKNOWN"


# ---------- 日志 ----------


def setup_logging(verbose: bool = False):
    """配置根日志记录器。"""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%Y-%m-%d %H:%M:%S")


# ---------- 时间 ----------


def utc_now_iso() -> str:
    """返回当前 UTC 时间的 ISO8601 字符串。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def timestamp_to_utc_iso(ts: float) -> str:
    """将 POSIX 时间戳转为 UTC ISO8601 字符串。"""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
