"""Read process-generation identity independently of creator-provided receipts."""
from __future__ import annotations

import ctypes
import ctypes.util
import platform
import re
from pathlib import Path

from .errors import JgError


def native_process_generation(pid: int) -> str:
    """Match the reviewed lease format using OS state; never fall back to lstart."""
    if type(pid) is not int or pid <= 0:
        raise JgError("invalid process generation PID")
    system = platform.system()
    if system == "Darwin":
        class ProcBsdInfo(ctypes.Structure):
            _fields_ = [
                *[(name, ctypes.c_uint32) for name in (
                    "flags", "status", "xstatus", "pid", "ppid", "uid", "gid",
                    "ruid", "rgid", "svuid", "svgid", "reserved")],
                ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
                *[(name, ctypes.c_uint32) for name in (
                    "nfiles", "pgid", "jobc", "tdev", "tpgid")],
                ("nice", ctypes.c_int32), ("start_sec", ctypes.c_uint64),
                ("start_usec", ctypes.c_uint64),
            ]
        try:
            library = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib", use_errno=True)
            query = library.proc_pidinfo
            query.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
            query.restype = ctypes.c_int
            info = ProcBsdInfo()
            size = query(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        except (OSError, AttributeError) as exc:
            raise JgError("native process generation unavailable") from exc
        if (size != ctypes.sizeof(info) or info.pid != pid
                or info.start_sec == 0 or info.start_usec >= 1_000_000):
            raise JgError("native process generation unavailable")
        return f"darwin:{pid}:{info.start_sec}:{info.start_usec}"
    if system == "Linux":
        try:
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            line = Path(f"/proc/{pid}/stat").read_text()
            if not line.startswith(f"{pid} (") or ")" not in line:
                raise ValueError("invalid stat identity")
            ticks = line[line.rfind(")") + 1:].split()[19]
            if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", boot) or not ticks.isdecimal():
                raise ValueError("invalid generation fields")
        except (OSError, IndexError, ValueError) as exc:
            raise JgError("native process generation unavailable") from exc
        return f"linux:{boot}:{ticks}"
    raise JgError("native process generation unsupported")


def receipt_generation_matches(receipt: object, pid: int, legacy_start: str) -> bool:
    if not isinstance(receipt, str):
        return False
    if receipt.startswith(("darwin:", "linux:")):
        # Unknown platform, missing process, malformed receipt or mismatch must
        # never fall back to the lower-resolution legacy comparison.
        return receipt == native_process_generation(pid)
    legacy_pattern = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+[0-9]{1,2}\s+[0-9]{2}:[0-9]{2}:[0-9]{2}\s+[0-9]{4}"
    return bool(re.fullmatch(legacy_pattern, receipt)) and receipt == legacy_start
