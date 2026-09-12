"""Landlock write restriction. Not a full OS sandbox; grok/codex stay unwrapped."""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

NR_CREATE = 444
NR_ADD = 445
NR_RESTRICT = 446
PR_SET_NO_NEW_PRIVS = 38
LANDLOCK_RULE_PATH_BENEATH = 1
LANDLOCK_RULE_NET_PORT = 2
ACCESS_EXECUTE = 1 << 0
ACCESS_READ = ACCESS_EXECUTE | (1 << 2) | (1 << 3)  # READ_FILE, READ_DIR
ACCESS_WRITE = (
    (1 << 1)   # WRITE_FILE
    | (1 << 4)  # REMOVE_DIR
    | (1 << 5)  # REMOVE_FILE
    | (1 << 7)  # MAKE_DIR
    | (1 << 8)  # MAKE_REG
    | (1 << 9)  # MAKE_SOCK
    | (1 << 10)  # MAKE_FIFO
    | (1 << 12)  # MAKE_SYM
    | (1 << 14)  # TRUNCATE
)
ACCESS_NET = (1 << 0) | (1 << 1)  # BIND_TCP, CONNECT_TCP
O_PATH = getattr(os, "O_PATH", 0x200000)
O_CLOEXEC = getattr(os, "O_CLOEXEC", 0x80000)

_libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
_libc.syscall.restype = ctypes.c_long
_libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]


class RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64)]


class PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _sys(*args) -> int:
    ctypes.set_errno(0)
    result = _libc.syscall(*args)
    if result < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return int(result)


def abi_version() -> int:
    try:
        return _sys(NR_CREATE, None, 0, 1)
    except OSError:
        return 0


def write_restriction_available() -> bool:
    return abi_version() >= 1


def probe() -> dict:
    return {
        "landlock_abi": abi_version(),
        "write_restriction_available": write_restriction_available(),
        "unshare": bool(shutil.which("unshare")),
        "bwrap": bool(shutil.which("bwrap")),
        "os_sandbox": False,
        "grok_landlocked": write_restriction_available(),
        "codex_landlocked": write_restriction_available(),
        "command_adapter_landlock": write_restriction_available(),
        "command_network_denied": abi_version() >= 4,
        "command_secret_read_denied": write_restriction_available(),
        "grok_network_unrestricted": True,
        "grok_arbitrary_read_denied": write_restriction_available(),
        "reads_and_network_unrestricted": False,
        "worker_env_scrubbed": True,
    }


def _add_path(ruleset: int, path: Path, access: int) -> None:
    fd = os.open(path, os.O_RDONLY | O_PATH | O_CLOEXEC)
    try:
        rule = PathBeneath(access, fd)
        _sys(NR_ADD, ruleset, LANDLOCK_RULE_PATH_BENEATH, ctypes.byref(rule), 0)
    finally:
        os.close(fd)


def restrict_writes(roots: list[Path]) -> None:
    _restrict(write_roots=roots, read_roots=[], deny_net=False)


def restrict_command(write_roots: list[Path], read_roots: list[Path]) -> None:
    _restrict(write_roots=write_roots, read_roots=read_roots, deny_net=abi_version() >= 4)


def restrict_provider(write_roots: list[Path], read_roots: list[Path]) -> None:
    _restrict(write_roots=write_roots, read_roots=read_roots, deny_net=False)


def _restrict(*, write_roots: list[Path], read_roots: list[Path], deny_net: bool) -> None:
    if not write_roots:
        raise ValueError("at least one write root is required")
    if _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    handled_fs = ACCESS_WRITE | (ACCESS_READ if read_roots else 0)
    handled_net = ACCESS_NET if deny_net else 0
    attr = RulesetAttr(handled_fs, handled_net)
    ruleset = _sys(NR_CREATE, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    try:
        write_access = ACCESS_WRITE | (ACCESS_READ if read_roots else 0)
        for root in write_roots:
            _add_path(ruleset, Path(root).resolve(), write_access)
        for root in read_roots:
            path = Path(root)
            if path.exists():
                _add_path(ruleset, path.resolve(), ACCESS_READ)
        _sys(NR_RESTRICT, ruleset, 0)
    finally:
        os.close(ruleset)


def provider_write_roots(adapter: str, worktree: Path, execution_dir: Path) -> list[Path]:
    scratch = Path(execution_dir) / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    roots = [Path(worktree).resolve(), Path(execution_dir).resolve(), scratch.resolve()]
    home = Path.home()
    extra = {
        "grok": [home / ".grok"],
        "codex": [home / ".codex", home / ".config" / "codex"],
    }.get(adapter, [])
    for path in extra:
        if path.exists():
            roots.append(path.resolve())
    # unique while stable
    seen: set[str] = set()
    ordered: list[Path] = []
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            ordered.append(root)
    return ordered


def scratch_env(execution_dir: Path) -> dict[str, str]:
    scratch = str((Path(execution_dir) / "tmp").resolve())
    return {
        "TMPDIR": scratch,
        "TEMP": scratch,
        "TMP": scratch,
        "XDG_CACHE_HOME": scratch,
        "XDG_RUNTIME_DIR": scratch,
        "PYTHONPYCACHEPREFIX": scratch,
        "PYTHONNOUSERSITE": "1",
    }


_BASE_ENV = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
})
_BLOCK_PREFIX = ("AWS_", "DJANGO_", "DATABASE_", "POSTGRES_", "MYSQL_", "REDIS_", "SHOPEE_")
_PROVIDER_PREFIX = {
    "grok": ("GROK_", "XAI_"),
    "codex": ("OPENAI_", "CODEX_", "CHATGPT_"),
    "command": (),
}
_SECRET_MARK = ("SECRET", "PASSWORD", "TOKEN", "DATABASE_URL", "PRIVATE_KEY", "API_KEY")


def worker_env(adapter: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Copy a small allowlist. Command workers do not inherit model API keys."""
    prefixes = _PROVIDER_PREFIX.get(adapter, ())
    out: dict[str, str] = {}
    for key, value in os.environ.items():
        if key.startswith(_BLOCK_PREFIX):
            continue
        if key in _BASE_ENV:
            out[key] = value
            continue
        if prefixes and key.startswith(prefixes):
            out[key] = value
            continue
        if any(mark in key.upper() for mark in _SECRET_MARK):
            continue
    out.update(extra or {})
    return out


def unix_read_roots() -> list[Path]:
    roots: list[Path] = []
    for raw in ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc", "/proc", "/dev", "/run", "/opt"):
        path = Path(raw)
        if path.exists():
            roots.append(path)
    for raw in (sys.prefix, sys.base_prefix, sys.exec_prefix, str(Path(sys.executable).resolve().parent)):
        path = Path(raw)
        if path.exists():
            roots.append(path.resolve())
    return roots


def command_read_roots(write_roots: list[Path], command: list[str]) -> list[Path]:
    roots = unix_read_roots() + [Path(root).resolve() for root in write_roots]
    if command:
        binary = Path(command[0])
        if binary.exists():
            roots.append(binary.resolve().parent)
    seen: set[str] = set()
    ordered: list[Path] = []
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            ordered.append(root)
    return ordered


def wrap_argv(command: list[str], write_roots: list[Path], *, mode: str = "writes") -> list[str]:
    argv = [sys.executable, str(Path(__file__).resolve()), "exec", "--mode", mode]
    for root in write_roots:
        argv.extend(["--write", str(Path(root).resolve())])
    if mode in {"command", "provider"}:
        for root in command_read_roots(write_roots, command):
            argv.extend(["--read", str(root)])
    argv.append("--")
    argv.extend(command)
    return argv


def _selftest(allowed: Path, outside: Path) -> int:
    restrict_writes([allowed])
    (allowed / "inside.txt").write_text("ok", encoding="utf-8")
    try:
        (outside / "outside.txt").write_text("no", encoding="utf-8")
    except OSError:
        return 0
    return 2


def _selftest_command(allowed: Path, secret: Path) -> int:
    import socket
    restrict_command([allowed], command_read_roots([allowed], [sys.executable]))
    (allowed / "inside.txt").write_text("ok", encoding="utf-8")
    try:
        secret.read_text(encoding="utf-8")
        return 3
    except OSError:
        pass
    try:
        socket.create_connection(("127.0.0.1", 443), timeout=1)
        return 4
    except OSError:
        return 0


def _selftest_provider(allowed: Path, auth_dir: Path, secret: Path) -> int:
    import errno
    import socket
    restrict_provider([allowed, auth_dir], command_read_roots([allowed, auth_dir], [sys.executable]))
    (allowed / "inside.txt").write_text("ok", encoding="utf-8")
    auth_dir.joinpath("auth.json").read_text(encoding="utf-8")
    try:
        secret.read_text(encoding="utf-8")
        return 3
    except OSError:
        pass
    try:
        socket.create_connection(("127.0.0.1", 1), timeout=0.2)
        return 0
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.EACCES:
            return 5
        return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"probe", "-h", "--help"}:
        print(__import__("json").dumps(probe()))
        return 0
    if args[0] == "selftest":
        return _selftest(Path(args[1]), Path(args[2]))
    if args[0] == "selftest-command":
        return _selftest_command(Path(args[1]), Path(args[2]))
    if args[0] == "selftest-provider":
        return _selftest_provider(Path(args[1]), Path(args[2]), Path(args[3]))
    if args[0] != "exec":
        print("usage: isolation.py probe|selftest|selftest-command|exec --mode writes|command --write DIR --read DIR -- CMD", file=sys.stderr)
        return 2
    write_roots: list[Path] = []
    read_roots: list[Path] = []
    mode = "writes"
    i = 1
    while i < len(args):
        if args[i] == "--mode" and i + 1 < len(args):
            mode = args[i + 1]
            i += 2
            continue
        if args[i] == "--write" and i + 1 < len(args):
            write_roots.append(Path(args[i + 1]))
            i += 2
            continue
        if args[i] == "--read" and i + 1 < len(args):
            read_roots.append(Path(args[i + 1]))
            i += 2
            continue
        if args[i] == "--":
            i += 1
            break
        print("unknown isolation argument", args[i], file=sys.stderr)
        return 2
    command = args[i:]
    if not command:
        print("missing command", file=sys.stderr)
        return 2
    if mode == "command":
        restrict_command(write_roots, read_roots or command_read_roots(write_roots, command))
    elif mode == "provider":
        restrict_provider(write_roots, read_roots or command_read_roots(write_roots, command))
    else:
        restrict_writes(write_roots)
    os.execvp(command[0], command)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
