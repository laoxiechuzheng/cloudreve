#!/usr/bin/env python3
"""Operate a fixed-version Cloudreve installation. Python 3.9+, standard library."""

import argparse
import configparser
import contextlib
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import platform
import re
import shutil
import signal
import sqlite3
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid


APP_VERSION = "3.8.3"
DB_VERSION = "3.8.1"
DATA_LINKS = ("cloudreve.db", "uploads", "avatar", "temp")
DB_SIDECARS = ("cloudreve.db-wal", "cloudreve.db-shm", "cloudreve.db-journal")
RELEASE_NAMES = {"cloudreve", "build-info.json", "deploy", "LICENSE", "README.md"}
SNAPSHOT_NAMES = {"conf.ini", "cloudreve.db", "uploads", "avatar", "temp", "cloudreve",
                  "statics", "releases", "deploy", "README.md", "AGENTS.md",
                  "REQUIREMENTS.md", ".cloudreve-manager"}


class ManagerError(Exception):
    """An operator-facing error containing no configuration values or DB contents."""


def require(condition, message):
    if not condition:
        raise ManagerError(message)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def verify_hash(path, expected):
    require(isinstance(expected, str) and re.fullmatch(r"[a-fA-F0-9]{64}", expected),
            "Expected SHA256 must be exactly 64 hexadecimal characters")
    require(sha256(path) == expected.lower(), "Archive SHA256 mismatch")


def version_name(value):
    require(isinstance(value, str) and len(value) <= 100 and
            re.fullmatch(r"3\.8\.3(?:[-+][A-Za-z0-9][A-Za-z0-9._-]*)?", value),
            "Release version must be 3.8.3 or a unique 3.8.3- / 3.8.3+ build")
    return value


def member_name(value, windows=False):
    require(isinstance(value, str) and value and "\\" not in value and
            not any(ord(c) < 32 or ord(c) == 127 for c in value) and
            not value.startswith("/") and not PureWindowsPath(value).drive,
            "Unsafe archive or storage path")
    parts = PurePosixPath(value).parts
    require(parts and ".." not in parts, "Unsafe archive or storage path")
    if windows:
        # These names are valid Linux data, but alias devices/files on Windows.
        require(all(":" not in p and not p.endswith((" ", ".")) and
                    not re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", p)
                    for p in parts), "Nonportable archive or storage path")
    return "/".join(parts)


def no_links(path):
    path = Path(path)
    for item in (path,) + tuple(path.parents):
        require(not item.is_symlink(), "Symbolic link in filesystem path is unsupported")
    return path


def check_root(root, existing=True):
    root = Path(root)
    require(root.is_absolute(), "--root must be an absolute data directory")
    require(not any(ord(c) < 32 or ord(c) == 127 for c in str(root)), "Invalid data root")
    no_links(root)
    root = root.resolve()
    require(root != Path(root.anchor), "Filesystem root cannot be a data directory")
    if existing:
        require(root.is_dir(), "Data root must already exist")
    return root


def private_dir(path):
    no_links(path)
    Path(path).mkdir(mode=0o700, parents=True, exist_ok=True)
    require(Path(path).is_dir(), "Expected an ordinary directory")
    return Path(path)


def read_json(path):
    require(Path(path).is_file() and not Path(path).is_symlink(), "Missing JSON metadata")
    require(Path(path).stat().st_size <= 32 * 1024 * 1024, "JSON metadata is too large")
    try:
        result = json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        raise ManagerError("Invalid JSON metadata") from None
    require(isinstance(result, dict), "Invalid JSON metadata object")
    return result


def write_json(path, value):
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=True, sort_keys=True, indent=2)
        stream.write("\n")


@contextlib.contextmanager
def sqlite_read(path):
    require(Path(path).is_file(), "SQLite database is missing; initialization is forbidden")
    no_links(path)
    connection = None
    try:
        connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        connection.execute("PRAGMA query_only=ON")
        yield connection
    except sqlite3.Error:
        raise ManagerError("SQLite read or integrity check failed") from None
    finally:
        if connection is not None:
            connection.close()


def load_config(root):
    root = check_root(root)
    no_links(root / "conf.ini")
    require((root / "conf.ini").is_file(), "Existing conf.ini is required; credentials are never generated")
    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    try:
        parser.read_string((root / "conf.ini").read_text(encoding="utf-8-sig"))
    except (configparser.Error, UnicodeError):
        raise ManagerError("Cannot parse conf.ini; use ordinary INI syntax") from None

    def value(section, key, default=""):
        text = parser.get(section, key, fallback=default).strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'`":
            text = text[1:-1]
        return text

    require(value("System", "Mode", "master") == "master", "Only master mode is supported")
    require(value("Database", "Type", "UNSET") in ("UNSET", "sqlite", "sqlite3"),
            "Unsupported database; only local SQLite is supported")
    require(value("Database", "DBFile", "cloudreve.db") in ("cloudreve.db", "./cloudreve.db"),
            "Unsupported DBFile; use the existing relative cloudreve.db layout")
    require(not value("Redis", "Server"), "External Redis cache requires a separate migration plan")
    require(not value("SSL", "CertPath") and not value("UnixSocket", "Listen"),
            "TLS/Unix listeners are unsupported; use HTTP behind the documented proxy")
    prefix = value("Database", "TablePrefix")
    require(re.fullmatch(r"[A-Za-z0-9_]*", prefix), "Unsupported database table prefix")
    listen = value("System", "Listen", ":5212")
    match = re.fullmatch(r"(\[[0-9A-Fa-f:]+\]|[0-9.]*|localhost):(\d{1,5})", listen)
    require(match is not None, "Unsupported HTTP Listen setting")
    host, port = match.groups()
    port = int(port)
    require(0 < port <= 65535, "Invalid HTTP listen port")
    host = host.strip("[]")
    if host in ("", "0.0.0.0", "localhost"):
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    else:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            raise ManagerError("Invalid HTTP listen address") from None
    for entry in DATA_LINKS + DB_SIDECARS:
        no_links(root / entry)
    overrides = {key: value("OptionOverwrite", key) for key in ("temp_path", "avatar_path", "thumb_file_suffix")
                 if parser.has_option("OptionOverwrite", key)}
    return {"port": port, "host": host, "prefix": prefix, "overrides": overrides}


def validate_data(root, full=False):
    root = check_root(root)
    config = load_config(root)
    prefix = config["prefix"]
    with sqlite_read(root / "cloudreve.db") as db:
        result = db.execute("PRAGMA " + ("integrity_check" if full else "quick_check")).fetchall()
        require(result == [("ok",)], "SQLite integrity check failed")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {prefix + t for t in ("settings", "policies", "files", "users", "groups", "folders", "shares", "nodes")}
        require(required <= tables, "SQLite schema is incomplete; automatic migration is forbidden")
        settings = dict(db.execute('SELECT name,value FROM "' + prefix + 'settings" WHERE deleted_at IS NULL'))
        require(settings.get("db_version_" + DB_VERSION) == "installed", "Required database version marker is missing")
        for key in settings:
            if key.startswith("db_version_"):
                match = re.fullmatch(r"db_version_(\d+)\.(\d+)\.(\d+)", key)
                require(match and tuple(map(int, match.groups())) <= (3, 8, 1), "Unsupported database version marker")
        for key, expected in (("temp_path", "temp"), ("avatar_path", "avatar")):
            configured = config["overrides"].get(key, settings.get(key, expected))
            require(configured in (expected, "./" + expected),
                    "Unsupported " + key + "; only the standard relative layout is supported")
        suffix = config["overrides"].get("thumb_file_suffix", settings.get("thumb_file_suffix", "._thumb"))
        require("/" not in suffix and "\\" not in suffix, "External thumbnail paths are unsupported")
        policies = {}
        for ident, kind, rule, server in db.execute('SELECT id,type,dir_name_rule,server FROM "' + prefix + 'policies"'):
            require(kind == "local" and not server, "External storage policy requires a separate migration plan")
            require(member_name(rule).startswith("uploads/"), "Unsupported storage rule; expected relative uploads/ path")
            policies[ident] = rule
        require(policies, "No local storage policy found")
        file_columns = {row[1] for row in db.execute('PRAGMA table_info("' + prefix + 'files")')}
        pending = "upload_session_id" if "upload_session_id" in file_columns else "NULL"
        for source, policy, deleted, session in db.execute(
                'SELECT source_name,policy_id,deleted_at,' + pending + ' FROM "' + prefix + 'files"'):
            require(policy in policies, "File references an unsupported or missing storage policy")
            name = member_name(source)
            require(name.startswith("uploads/"), "Unsupported file source path outside relative uploads/")
            if full and deleted is None and session is None:
                no_links(root / name)
                require((root / name).is_file(), "Stored upload is missing; restore/activation refused")
        for kind, aria2 in db.execute('SELECT type,aria2_enabled FROM "' + prefix + 'nodes"'):
            require(kind == 1 and not aria2, "External/aria2 nodes require a separate consistent backup plan")
    if full:
        for name in ("uploads", "temp", "avatar"):
            directory = root / name
            if directory.exists():
                require(directory.is_dir(), "Expected ordinary storage directories")
                tree_entries(directory)
    return {"port": config["port"], "host": config["host"], "db_version": DB_VERSION}


def extract_checked(archive, expected, destination):
    """Preflight every member; never delegate path or link handling to extractall."""
    verify_hash(archive, expected)
    destination = Path(destination)
    no_links(destination)
    require(not destination.exists() or (destination.is_dir() and not any(destination.iterdir())),
            "Extraction destination must be empty")
    try:
        with tarfile.open(archive, "r:gz") as tf:
            entries = {}
            for item in tf:
                if item.name in (".", "./") and item.isdir():
                    continue
                name = member_name(item.name, windows=os.name == "nt")
                require(name not in entries, "Duplicate archive member")
                require(item.isdir() or item.isfile(), "Archive links and special members are forbidden")
                require(not item.issparse() and not item.mode & 0o7000, "Special archive modes are forbidden")
                require(item.size >= 0 and (item.isfile() or item.size == 0), "Invalid archive member size")
                entries[name] = item
            for name in entries:
                for parent in PurePosixPath(name).parents:
                    if str(parent) in entries:
                        require(entries[str(parent)].isdir(), "Archive member has a file as its parent")
            destination.mkdir(mode=0o700, parents=True, exist_ok=True)
            for name, item in entries.items():
                path = destination.joinpath(*PurePosixPath(name).parts)
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if item.isdir():
                    path.mkdir(mode=0o700, exist_ok=True)
                else:
                    with tf.extractfile(item) as src, path.open("xb") as dst:
                        os.chmod(path, 0o600)
                        shutil.copyfileobj(src, dst, 1024 * 1024)
                    require(path.stat().st_size == item.size, "Truncated archive member")
            return entries
    except (tarfile.TarError, EOFError):
        raise ManagerError("Invalid or truncated tar.gz archive") from None


def host_platform():
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    require(arch, "Unsupported host architecture; only amd64 and arm64 are supported")
    return "linux/" + arch


def release_info(release):
    info = read_json(release / "build-info.json")
    version_name(info.get("version"))
    for key in ("backend_commit", "frontend_commit"):
        require(isinstance(info.get(key), str) and re.fullmatch(r"[a-fA-F0-9]{40}", info[key]),
                "Release provenance must contain full backend/frontend commit hashes")
    require(isinstance(info.get("go_version"), str) and re.fullmatch(
            r"(?:go version )?go1\.\d+(?:\.\d+)?(?: [A-Za-z0-9_]+/[A-Za-z0-9_]+)?", info["go_version"]),
            "Missing or invalid Go provenance")
    require(info.get("backend_version", APP_VERSION) == APP_VERSION, "Release backend version mismatch")
    require(info.get("platform") == host_platform(), "Release architecture/platform does not match this host")
    require(info.get("required_db_version", DB_VERSION) == DB_VERSION, "Release requires an unsupported database version")
    require(info.get("asset_version", APP_VERSION) == APP_VERSION, "Release static asset version mismatch")
    for name in ("cloudreve", "LICENSE", "README.md", "deploy/cloudreve-manager.py"):
        require((release / name).is_file() and not (release / name).is_symlink(), "Incomplete release payload")
    with (release / "cloudreve").open("rb") as stream:
        header = stream.read(64)
    machine = 183 if info["platform"] == "linux/arm64" else 62
    require(len(header) == 64 and header[:6] == b"\x7fELF\x02\x01" and
            struct.unpack_from("<H", header, 18)[0] == machine, "ELF executable architecture mismatch")
    if "binary_sha256" in info:
        require(info["binary_sha256"] == sha256(release / "cloudreve"), "Release binary hash mismatch")
    return info


def tree_entries(root, skip_links=()):
    entries = {}

    def visit(path, rel):
        name = member_name(rel)
        metadata = path.lstat()
        if path.is_symlink():
            require(name in skip_links and os.readlink(path).replace("\\", "/") == "../../" + name,
                    "Unexpected filesystem link; backup or activation refused")
            return
        require(stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode), "Special filesystem object is unsupported")
        entries[name] = path
        if path.is_dir():
            for child in sorted(path.iterdir()):
                visit(child, name + "/" + child.name)

    for child in sorted(Path(root).iterdir()):
        visit(child, child.name)
    return entries


def release_files(release):
    files = tree_entries(release, skip_links=DATA_LINKS)
    return {name: sha256(path) for name, path in files.items()
            if path.is_file() and name not in (".release.json",) + DB_SIDECARS}


def verify_release(release):
    no_links(release)
    require({p.name for p in release.iterdir()} <= RELEASE_NAMES | {".release.json"} | set(DATA_LINKS + DB_SIDECARS),
            "Unexpected file or static override in release directory")
    for name in DB_SIDECARS:
        path = release / name
        require(not path.is_symlink() and (not path.exists() or path.is_file()), "Unsafe release SQLite sidecar")
    info = release_info(release)
    require(release.name == info["version"], "Release directory/version mismatch")
    seal = read_json(release / ".release.json")
    require(seal.get("files") == release_files(release), "Installed release hash mismatch")
    require(re.fullmatch(r"[a-f0-9]{64}", str(seal.get("archive_sha256", ""))), "Missing original release SHA256")
    return info


def stage_release(root, archive, expected):
    root = check_root(root)
    verify_hash(archive, expected)
    releases = private_dir(root / "releases")
    with tempfile.TemporaryDirectory(prefix=".stage-", dir=releases) as stage:
        stage = Path(stage)
        metadata = extract_checked(archive, expected, stage)
        require({PurePosixPath(n).parts[0] for n in metadata} == RELEASE_NAMES,
                "Release must contain only cloudreve, build-info.json, deploy/, LICENSE, README.md")
        info = release_info(stage)
        target = releases / info["version"]
        if target.exists() or target.is_symlink():
            require(read_json(target / ".release.json").get("archive_sha256") == expected.lower(),
                    "Release version already exists with a different archive")
            verify_release(target)
            return target
        for name, item in metadata.items():
            os.chmod(stage / name, 0o755 if item.isdir() or name == "cloudreve" else (item.mode & 0o755) | 0o600)
        write_json(stage / ".release.json", {"archive_sha256": expected.lower(), "files": release_files(stage)})
        stage.rename(target)
        return target


def bind_data(root, release):
    verify_release(release)
    for name in DATA_LINKS:
        target = root / name
        no_links(target)
        if name != "cloudreve.db":
            target.mkdir(mode=0o700, exist_ok=True)
            require(target.is_dir(), "Expected a data directory")
        link = release / name
        if link.is_symlink():
            require(os.readlink(link).replace("\\", "/") == "../../" + name, "Release data link mismatch")
        else:
            require(not link.exists(), "Release contains conflicting data files")
            link.symlink_to("../../" + name, target_is_directory=name != "cloudreve.db")


def validate_release_bindings(root, release):
    """Validate data identity without trusting or executing the current binary."""
    require(release.parent == root / "releases", "Release must be directly under this data root")
    version_name(release.name)
    no_links(release)
    require(release.is_dir(), "Current release directory is missing")
    require(not (release / "cloudreve").is_symlink(), "Unsafe current executable link")
    for name in DATA_LINKS:
        link, target = release / name, root / name
        no_links(target)
        require(link.is_symlink() and os.readlink(link).replace("\\", "/") == "../../" + name and
                link.resolve() == target.resolve(), "Release data binding mismatch")
        require(target.is_file() if name == "cloudreve.db" else target.is_dir(), "Release data binding target is missing")
    for name in DB_SIDECARS:
        path = no_links(release / name)
        require(not path.exists() or path.is_file(), "Unsafe release SQLite sidecar")


def current_release_identity(root, optional=False):
    link = root / "current"
    if optional and not link.exists() and not link.is_symlink():
        return None
    require(link.is_symlink(), "current must be a relative release symlink")
    name = os.readlink(link).replace("\\", "/")
    require(name.startswith("releases/") and name.count("/") == 1, "Unsafe current release link")
    version_name(name.split("/")[1])
    release = root / name
    validate_release_bindings(root, release)
    return release


def current_release(root, optional=False):
    release = current_release_identity(root, optional)
    if release is not None:
        verify_release(release)
    return release


def switch_current(root, version):
    current_release_identity(root, optional=True)
    if version is None:
        (root / "current").unlink(missing_ok=True)
        return
    version_name(version)
    verify_release(root / "releases" / version)
    temporary = root / (".current-" + uuid.uuid4().hex)
    try:
        temporary.symlink_to("releases/" + version, target_is_directory=True)
        os.replace(temporary, root / "current")
    finally:
        temporary.unlink(missing_ok=True)


def systemctl(*args):
    try:
        result = subprocess.run(["systemctl", *args], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=330, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ManagerError("systemctl unavailable or timed out; inspect service state locally") from None
    absent = args[0] == "show" and "LoadState=not-found" in result.stdout.splitlines()
    require(result.returncode == 0 or absent, "systemctl " + args[0] + " failed; inspect the unit journal locally")
    return result.stdout


def unit_quote(value):
    value = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return '"' + value + '"'


def unit_working_directory(value):
    # This single-path directive preserves quotes; only specifiers need escaping.
    return str(value).replace("%", "%%")


def connections(proc, port):
    result = {"active": 0, "listeners": set()}
    for name in ("tcp", "tcp6"):
        path = proc / "net" / name
        if name == "tcp6" and not path.exists():
            continue
        try:
            lines = path.read_text().splitlines()[1:]
            for line in lines:
                row = line.split()
                if int(row[1].rsplit(":", 1)[1], 16) != port:
                    continue
                if row[3] == "0A":
                    result["listeners"].add(row[9])
                elif row[3] in ("01", "08"):
                    result["active"] += 1
        except (OSError, ValueError, IndexError):
            raise ManagerError("Cannot inspect TCP connections; refusing an unverified stop") from None
    return result


def process_inventory(proc, root, state):
    """Check process identity, cgroup and data writers, rather than killing by name."""
    unmanaged, managed_sockets = set(), set()
    main_verified = False
    expected_exe = (root / "current/cloudreve").resolve()
    try:
        for entry in proc.iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                exe = Path(os.readlink(entry / "exe").removesuffix(" (deleted)"))
                cwd = Path(os.readlink(entry / "cwd"))
                argv = (entry / "cmdline").read_bytes().split(b"\0")
                groups = (entry / "cgroup").read_text().splitlines()
                owned = state["active"] and any(line.split(":", 2)[-1] == state["cgroup"] for line in groups)
                uses_config = os.fsencode(root / "conf.ini") in argv or any(
                    arg == b"-c=" + os.fsencode(root / "conf.ini") for arg in argv)
                candidate = uses_config or (exe.name == "cloudreve" and cwd == root) or root in exe.parents
                if int(entry.name) == state["pid"]:
                    main_verified = owned and cwd == root and exe == expected_exe
                if candidate and not owned:
                    unmanaged.add(int(entry.name))
                fd_dir = entry / "fd"
                if not fd_dir.exists():
                    continue
                for fd in fd_dir.iterdir():
                    try:
                        target = os.readlink(fd)
                        if target.startswith("socket:[") and owned:
                            managed_sockets.add(target[8:-1])
                        elif target.startswith(str(root) + os.sep):
                            rel = Path(target).relative_to(root).parts[0]
                            if rel in DATA_LINKS or rel in ("conf.ini", "cloudreve.db-wal", "cloudreve.db-journal"):
                                flags = (entry / "fdinfo" / fd.name).read_text()
                                match = re.search(r"^flags:\s+([0-7]+)$", flags, re.M)
                                require(match, "Cannot verify open data file access")
                                if int(match[1], 8) & os.O_ACCMODE and not owned:
                                    unmanaged.add(int(entry.name))
                    except FileNotFoundError:
                        continue
            except (FileNotFoundError, ProcessLookupError):
                continue
    except PermissionError:
        raise ManagerError("Cannot inspect /proc process ownership; run as root on the host") from None
    require(not state["active"] or main_verified, "Managed unit PID/cwd/executable identity does not match data root")
    return unmanaged, managed_sockets


class Service:
    def __init__(self, root, name="cloudreve-custom", unit_dir=Path("/etc/systemd/system"), proc=Path("/proc")):
        self.root = check_root(root)
        name = name.removesuffix(".service")
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name), "Invalid service name")
        self.unit = name + ".service"
        self.unit_path = Path(unit_dir) / self.unit
        self.proc = Path(proc)

    def unit_text(self, hardened=True):
        return ("# Generated by cloudreve-manager; dedicated to this data root.\n"
                "[Unit]\nDescription=Cloudreve 3.8.3 custom distribution\n"
                "After=network-online.target\nWants=network-online.target\n\n"
                "[Service]\nType=simple\nUser=root\nGroup=root\n"
                "WorkingDirectory=" + unit_working_directory(self.root) + "\n"
                "ExecStart=:" + unit_quote(self.root / "current/cloudreve") +
                " -c " + unit_quote(self.root / "conf.ini") + "\n"
                "KillSignal=SIGTERM\nKillMode=control-group\nTimeoutStopSec=300\nSendSIGKILL=no\n"
                "Restart=on-failure\nRestartSec=5\nUMask=0077\n" +
                (self.sandbox_text() if hardened else "") +
                "StandardOutput=journal\nStandardError=journal\n\n[Install]\nWantedBy=multi-user.target\n")

    def sandbox_text(self):
        # The process stays root (the data root lives under /root), so confine what root can do instead:
        # no capabilities, read-only OS and home, writes only inside the data root. A file-write bug in
        # Cloudreve then cannot reach ~/.ssh, cron, systemd units or binaries outside the data root.
        return ("NoNewPrivileges=yes\nCapabilityBoundingSet=\nAmbientCapabilities=\n"
                "ProtectSystem=strict\nProtectHome=read-only\n"
                "ReadWritePaths=" + unit_quote(self.root) + "\n"
                "PrivateTmp=yes\nPrivateDevices=yes\nProtectKernelTunables=yes\nProtectKernelModules=yes\n"
                "ProtectKernelLogs=yes\nProtectControlGroups=yes\nProtectClock=yes\nProtectHostname=yes\n"
                "RestrictSUIDSGID=yes\nRestrictRealtime=yes\nRestrictNamespaces=yes\nLockPersonality=yes\n"
                "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK\nSystemCallArchitectures=native\n")

    def inspect(self):
        fields = "LoadState,ActiveState,SubState,MainPID,ControlGroup,FragmentPath,DropInPaths,UnitFileState"
        raw = systemctl("show", self.unit, "--no-pager", "--property=" + fields)
        props = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
        require(props.get("LoadState") in ("loaded", "not-found"), "Unsupported or masked service unit")
        exists = props["LoadState"] == "loaded"
        legacy = False
        if exists or self.unit_path.exists() or self.unit_path.is_symlink():
            no_links(self.unit_path)
            text = self.unit_path.read_text(encoding="utf-8") if exists else None
            # The unit written by managers before the sandbox is still ours; install-service upgrades it.
            require(exists and props.get("FragmentPath") == str(self.unit_path) and
                    text in (self.unit_text(), self.unit_text(hardened=False)) and
                    not props.get("DropInPaths"), "Service unit is not the exact manager-owned unit for this root")
            legacy = text != self.unit_text()
        require(props.get("ActiveState") in ("active", "inactive", "failed"), "Service unit is transitioning; retry after it settles")
        try:
            pid = int(props.get("MainPID", "0"))
        except ValueError:
            raise ManagerError("Invalid service PID") from None
        return {"exists": exists, "legacy": legacy, "active": props["ActiveState"] == "active", "pid": pid,
                "cgroup": props.get("ControlGroup", ""), "enabled": props.get("UnitFileState") == "enabled"}

    def guard(self, port, allow_active=False):
        state = self.inspect()
        unmanaged, owned_sockets = process_inventory(self.proc, self.root, state)
        tcp = connections(self.proc, port)
        require(not unmanaged and not (tcp["listeners"] - owned_sockets),
                "Running unmanaged instance, data writer, or port listener; stop it manually after verification")
        require(allow_active or not tcp["active"],
                "Active TCP connections may be downloads; drain traffic or explicitly use --allow-active-connections")
        return state

    def start(self):
        systemctl("start", self.unit)

    def stop(self):
        systemctl("stop", self.unit)


def wait_healthy(config, timeout=45):
    host = config["host"]
    if ":" in host:
        host = "[" + host + "]"
    url = "http://" + host + ":" + str(config["port"]) + "/api/v3/site/ping"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while True:
        try:
            with opener.open(url, timeout=2) as response:
                payload = json.loads(response.read(16385))
                if (response.status == 200 and response.geturl() == url and isinstance(payload, dict) and
                        type(payload.get("code")) is int and payload["code"] == 0 and payload.get("data") == APP_VERSION):
                    return
        except (OSError, ValueError, urllib.error.URLError):
            pass
        if time.monotonic() >= deadline:
            raise ManagerError("Health probe failed: expected JSON code=0 and data=3.8.3")
        time.sleep(min(1, max(0, deadline - time.monotonic())))


@contextlib.contextmanager
def stopped_service(service, config, allow_active=False):
    state = service.guard(config["port"], allow_active)
    try:
        if state["active"]:
            service.stop()
        require(not service.guard(config["port"])["active"], "Service did not stop; data operation refused")
        yield state
    finally:
        # This includes stop timeouts, archive failures, Ctrl-C and handled SIGTERM.
        if state["active"]:
            try:
                service.start()
                wait_healthy(config)
            except Exception:
                raise ManagerError("Service restart/recovery failed; inspect systemctl status and journal locally") from None


def backup_operations(root, service):
    directory = private_dir(root / "backups" / ("operations-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]))
    previous = current_release_identity(root, optional=True)
    binary = previous / "cloudreve" if previous else root / "cloudreve"
    sources = {}
    for source, name in ((root / "conf.ini", "conf.ini"), (binary, "cloudreve"),
                         (service.unit_path, "service.unit")):
        no_links(source)
        if source.is_file():
            shutil.copy2(source, directory / name)
            os.chmod(directory / name, 0o600)
            sources[name] = service.unit if name == "service.unit" else name
    write_json(directory / "operation.json", {"format": "cloudreve-operations-v1", "created_ns": time.time_ns(),
                                              "resolved": False, "sources": sources,
                                              "previous_release": previous.name if previous else None, "service": service.unit})
    return directory


def resolve_backup(root, directory):
    record = read_json(directory / "operation.json")
    record["resolved"] = True
    atomic_text(directory / "operation.json", json.dumps(record, sort_keys=True) + "\n")
    records = []
    references = "\n".join((root / name).read_text(encoding="utf-8", errors="replace")
                            for name in ("README.md", "AGENTS.md", "REQUIREMENTS.md")
                            if (root / name).is_file() and not (root / name).is_symlink())
    for path in (root / "backups").glob("operations-*"):
        no_links(path)
        if path.is_dir() and (path / "operation.json").is_file():
            metadata = read_json(path / "operation.json")
            if metadata.get("format") == "cloudreve-operations-v1" and metadata.get("resolved") is True:
                records.append((metadata.get("created_ns", 0), path, metadata))
    counts = {}
    for _, path, metadata in sorted(records, reverse=True):
        for name, source in metadata.get("sources", {}).items():
            require(name in ("conf.ini", "cloudreve", "service.unit"), "Invalid operational backup metadata")
            copy = no_links(path / name)
            if not copy.is_file():
                continue
            counts[source] = counts.get(source, 0) + 1
            if counts[source] > 3 and str(copy.relative_to(root)).replace("\\", "/") not in references and str(copy) not in references:
                copy.unlink()


def checkpoint_database(root, release=None):
    """Only call after the service/process guard has established exclusive downtime."""
    load_config(root)
    database = root / "cloudreve.db"
    require(database.is_file(), "SQLite database is missing")
    with database.open("rb") as stream:
        require(stream.read(16) == b"SQLite format 3\x00", "SQLite file header is invalid; initialization is forbidden")
    alias = release / "cloudreve.db" if release else None
    local = []
    if release:
        validate_release_bindings(root, release)
        local = [release / name for name in ("cloudreve.db-wal", "cloudreve.db-journal")
                 if (release / name).exists() and (release / name).stat().st_size]
    root_logs = [root / name for name in ("cloudreve.db-wal", "cloudreve.db-journal")
                 if (root / name).exists() and (root / name).stat().st_size]
    require(not (local and root_logs) and len(local) <= 1 and len(root_logs) <= 1,
            "SQLite sidecar conflict; retain both locations for manual recovery")
    temporary = None
    recovery_link = None
    restore_link = False
    try:
        target = database
        if local:
            recovery = private_dir(root / "backups" / ("sqlite-recovery-" + uuid.uuid4().hex))
            for path in [database] + [release / name for name in DB_SIDECARS if (release / name).exists()]:
                shutil.copy2(path, recovery / path.name)
                os.chmod(recovery / path.name, 0o600)
            # SQLite canonicalizes symlink filenames. A temporary hard link to the
            # SAME inode lets its normal recovery read release-local WAL/journal.
            # No DB bytes are copied back, and the original relative link is restored.
            recovery_link = release / (".checkpoint-link-" + uuid.uuid4().hex)
            recovery_link.symlink_to("../../cloudreve.db")
            temporary = release / (".checkpoint-" + uuid.uuid4().hex)
            os.link(database, temporary)
            restore_link = True
            os.replace(temporary, alias)
            target = alias
        with contextlib.closing(sqlite3.connect(target.absolute().as_uri() + "?mode=rw", uri=True, timeout=10)) as db:
            require(db.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "SQLite recovery integrity check failed")
            busy, pending, checkpointed = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            require(busy == 0 and pending == checkpointed, "SQLite checkpoint is busy or incomplete; export refused")
        require(not any(path.exists() and path.stat().st_size for path in local),
                "Release SQLite journal remains unresolved; export refused")
    except sqlite3.Error:
        raise ManagerError("SQLite checkpoint/recovery failed; any recovery copies remain under backups/") from None
    finally:
        if restore_link:
            # Allocate the recovery link before mutation; cleanup only renames it.
            # If this rename itself fails, retain it for manual recovery.
            os.replace(recovery_link, alias)
        if temporary:
            temporary.unlink(missing_ok=True)
        if recovery_link:
            recovery_link.unlink(missing_ok=True)
    if release:
        for name in DB_SIDECARS:
            path = release / name
            if path.exists():
                require(name.endswith("-shm") or path.stat().st_size == 0, "Unexpected unresolved release SQLite journal")
                path.unlink()


def activate(root, release, service, allow_active=False):
    config = validate_data(root)
    verify_release(release)
    previous = current_release_identity(root, optional=True)
    with stopped_service(service, config, allow_active) as state:
        checkpoint_database(root, previous)
        validate_data(root, full=True)
        backup = backup_operations(root, service)
        bind_data(root, release)
        switched = False
        try:
            switched = True
            switch_current(root, release.name)
            if state["exists"]:
                service.start()
                wait_healthy(config)
                service.guard(config["port"], allow_active=True)
        except BaseException:
            if switched:
                if state["exists"]:
                    service.stop()
                    require(not service.guard(config["port"])["active"], "Failed release still running; automatic rollback stopped")
                checkpoint_database(root, release)
                switch_current(root, previous.name if previous else None)
            raise ManagerError("Activation failed; release link rolled back; database was not restored") from None
    if state["exists"]:
        resolve_backup(root, backup)
    return {"version": release.name, "activated": True, "service_installed": state["exists"]}


def install(root, archive, expected, service, stage_only=False, allow_active=False):
    validate_data(root)
    release = stage_release(root, archive, expected)
    if stage_only:
        return {"version": release.name, "activated": False}
    return activate(root, release, service, allow_active)


def rollback(root, version, service, allow_active=False):
    version_name(version)
    return activate(root, root / "releases" / version, service, allow_active)


def atomic_text(path, text):
    no_links(path)
    fd, temporary = tempfile.mkstemp(prefix=".manager-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def install_service(root, service, allow_active=False):
    config = load_config(root)
    release = current_release(root)
    state = service.guard(config["port"], allow_active)
    bind_data(root, release)
    if not state["active"]:
        checkpoint_database(root, release)
    validate_data(root, full=True)
    backup = backup_operations(root, service)
    created = False
    upgraded = False
    try:
        if not state["exists"] or state["legacy"]:
            atomic_text(service.unit_path, service.unit_text())
            created = not state["exists"]
            upgraded = state["legacy"]
            systemctl("daemon-reload")
        systemctl("enable", service.unit)
        if upgraded and state["active"]:
            # The sandbox only applies to a new process.
            service.stop()
        service.start()
        wait_healthy(config)
        service.guard(config["port"], allow_active=True)
    except BaseException:
        service.stop()
        if upgraded:
            atomic_text(service.unit_path, service.unit_text(hardened=False))
            systemctl("daemon-reload")
        require(not service.guard(config["port"])["active"], "Service failed to stop; unit retained for manual recovery")
        if not state["enabled"]:
            systemctl("disable", service.unit)
        if created:
            service.unit_path.unlink()
            systemctl("daemon-reload")
        if state["active"]:
            service.start()
        raise ManagerError("Service installation failed; previous unit/enable state retained") from None
    operations = private_dir(root / ".cloudreve-manager")
    atomic_text(operations / "service.unit", service.unit_text())
    atomic_text(operations / "service.json", json.dumps({"service": service.unit, "version": release.name}) + "\n")
    resolve_backup(root, backup)
    return {"version": release.name, "service": service.unit, "active": True, "enabled": True}


def snapshot_entries(root, release):
    result = {}
    for name in sorted(SNAPSHOT_NAMES - {"releases"}):
        source = root / name
        if source.exists() or source.is_symlink():
            no_links(source)
            require(source.is_file() or source.is_dir(), "Unsupported backup object")
            result[name] = source
            if source.is_dir():
                result.update({name + "/" + key: path for key, path in tree_entries(source).items()})
    result["releases"] = root / "releases"
    result["releases/" + release.name] = release
    result.update({"releases/" + release.name + "/" + key: path
                   for key, path in tree_entries(release, skip_links=DATA_LINKS).items() if key not in DB_SIDECARS})
    return result


def fingerprint(entries):
    return {name: (p.stat().st_dev, p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_ctime_ns)
            for name, p in entries.items()}


def sqlite_fingerprint(root, release=None):
    directories = (root, release) if release else (root,)
    return tuple(sha256(path) if path.exists() and path.stat().st_size else None
                 for directory in directories for path in
                 (directory / "cloudreve.db-wal", directory / "cloudreve.db-journal"))


def backup_destination(root, output):
    output = Path(output).absolute()
    no_links(output)
    require(output.name.endswith(".tar.gz"), "Backup filename must end in .tar.gz")
    member_name(output.name, windows=os.name == "nt")
    require(not output.exists() and not Path(str(output) + ".sha256").exists(), "Backup output already exists")
    if root in output.parents:
        require(root / "backups" in output.parents, "Backups within data root must be under backups/")
    private_dir(output.parent)
    return output


def write_backup(root, release, output):
    """Internal archive writer. The export command supplies the stopped-service guard."""
    validate_data(root, full=True)
    info = verify_release(release)
    output = backup_destination(root, output)
    entries = snapshot_entries(root, release)
    before = fingerprint(entries)
    database_before = sqlite_fingerprint(root, release)
    created = False
    try:
        with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=private_dir(root / "backups")) as temporary:
            db_copy = Path(temporary) / "cloudreve.db"
            # SQLite backup handles WAL correctly without checkpointing or changing the source.
            with sqlite_read(root / "cloudreve.db") as source, contextlib.closing(sqlite3.connect(db_copy)) as target:
                source.backup(target)
                require(target.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "SQLite backup integrity failed")
            manifest = {"format": 1, "version": info["version"], "backend_version": APP_VERSION,
                        "db_version": DB_VERSION, "entries": {},
                        "root": {"mode": stat.S_IMODE(root.stat().st_mode),
                                 "uid": root.stat().st_uid, "gid": root.stat().st_gid}}
            with output.open("xb") as stream:
                created = True
                os.chmod(output, 0o600)
                with tarfile.open(fileobj=stream, mode="w:gz", format=tarfile.PAX_FORMAT, dereference=True) as tf:
                    for name, path in sorted(entries.items()):
                        actual = db_copy if name == "cloudreve.db" else path
                        item = tf.gettarinfo(str(actual), arcname=name)
                        original = path.stat()
                        item.uid, item.gid = original.st_uid, original.st_gid
                        item.mode = stat.S_IMODE(original.st_mode) & 0o777
                        item.uname = item.gname = ""
                        item.mtime = original.st_mtime
                        record = {"kind": "dir" if item.isdir() else "file", "mode": item.mode,
                                  "uid": item.uid, "gid": item.gid, "mtime": item.mtime}
                        if item.isfile():
                            record.update(size=item.size, sha256=sha256(actual))
                            with actual.open("rb") as contents:
                                tf.addfile(item, contents)
                        else:
                            tf.addfile(item)
                        manifest["entries"][name] = record
                    contents = json.dumps(manifest, ensure_ascii=True, sort_keys=True).encode("utf-8")
                    require(len(contents) <= 32 * 1024 * 1024, "Backup manifest exceeds the supported 32 MiB limit")
                    item = tarfile.TarInfo(".snapshot.json")
                    item.size, item.mode = len(contents), 0o600
                    tf.addfile(item, io.BytesIO(contents))
                stream.flush()
                os.fsync(stream.fileno())
            require(before == fingerprint(snapshot_entries(root, release)),
                    "Files changed during backup; archive discarded")
            require(database_before == sqlite_fingerprint(root, release),
                    "SQLite journal changed during backup; archive discarded")
            validate_data(root, full=True)
        return {"version": info["version"], "sha256": sha256(output)}
    except BaseException:
        if created:
            output.unlink(missing_ok=True)
        raise


def export(root, output, service, allow_active=False):
    config = load_config(root)
    release = current_release(root)
    output = backup_destination(root, output)
    with stopped_service(service, config, allow_active):
        checkpoint_database(root, release)
        result = write_backup(root, release, output)
        try:
            require(not service.guard(config["port"])["active"], "Service restarted during backup; archive discarded")
        except BaseException:
            output.unlink(missing_ok=True)
            raise
    sidecar = Path(str(output) + ".sha256")
    with sidecar.open("x", encoding="utf-8", newline="\n") as stream:
        os.chmod(sidecar, 0o600)
        stream.write(result["sha256"] + "  " + output.name + "\n")
    return result


def check_snapshot(stage, members):
    manifest = read_json(stage / ".snapshot.json")
    require(manifest.get("format") == 1 and manifest.get("backend_version") == APP_VERSION and
            manifest.get("db_version") == DB_VERSION, "Unsupported snapshot format/version")
    root_metadata = manifest.get("root", {})
    require(isinstance(root_metadata, dict) and all(
        type(root_metadata.get(key)) is int and 0 <= root_metadata[key] <= (0o777 if key == "mode" else 2**31 - 1)
        for key in ("uid", "gid", "mode")), "Invalid snapshot root metadata")
    version = version_name(manifest.get("version"))
    entries = manifest.get("entries")
    require(isinstance(entries, dict) and set(entries) == set(members) - {".snapshot.json"},
            "Snapshot manifest does not match archive members")
    for name, record in entries.items():
        require(member_name(name) == name and PurePosixPath(name).parts[0] in SNAPSHOT_NAMES,
                "Unexpected snapshot member")
        if name.startswith("releases/"):
            require(name == "releases/" + version or name.startswith("releases/" + version + "/"),
                    "Snapshot includes an unselected release")
            tail = name.removeprefix("releases/" + version + "/").split("/")[0]
            require(tail not in DATA_LINKS + DB_SIDECARS, "Snapshot must not contain release-local data or SQLite journals")
        path, item = stage / name, members[name]
        require(isinstance(record, dict) and record.get("kind") == ("dir" if item.isdir() else "file"),
                "Snapshot manifest type mismatch")
        for field in ("uid", "gid", "mode"):
            require(type(record.get(field)) is int and 0 <= record[field] <= (0o777 if field == "mode" else 2**31 - 1),
                    "Invalid snapshot ownership or permissions")
        require((item.uid, item.gid, item.mode, item.mtime) ==
                (record["uid"], record["gid"], record["mode"], record.get("mtime")), "Snapshot manifest metadata mismatch")
        if item.isfile():
            require(record.get("size") == path.stat().st_size and record.get("sha256") == sha256(path),
                    "Snapshot member hash mismatch")
    validate_data(stage, full=True)
    verify_release(stage / "releases" / version)
    require({"conf.ini", "cloudreve.db", "releases/" + version} <= set(entries), "Incomplete snapshot manifest")
    return manifest


def restore(root, archive, expected, verify_only=False):
    root = check_root(root, existing=False)
    require(not root.exists() or (root.is_dir() and not any(root.iterdir())), "Restore destination must be absent or empty")
    require(root.parent.is_dir(), "Restore parent directory must already exist")
    verify_hash(archive, expected)
    with tempfile.TemporaryDirectory(prefix=".cloudreve-restore-", dir=root.parent) as temporary:
        stage = Path(temporary) / "data"
        members = extract_checked(archive, expected, stage)
        manifest = check_snapshot(stage, members)
        version = manifest["version"]
        if verify_only:
            return {"version": version, "verified": True, "restored": False}
        require(sys.platform == "linux", "Restore publication requires Linux; --verify-only is portable")
        bind_data(stage, stage / "releases" / version)
        switch_current(stage, version)
        for name, item in sorted(manifest["entries"].items(), key=lambda pair: pair[0].count("/"), reverse=True):
            path = stage / name
            if os.geteuid() == 0:
                os.chown(path, item["uid"], item["gid"])
            os.chmod(path, item["mode"])
            os.utime(path, (item["mtime"], item["mtime"]))
        stage_root = manifest.get("root", {})
        if os.geteuid() == 0:
            os.chown(stage, stage_root["uid"], stage_root["gid"])
        os.chmod(stage, stage_root["mode"])
        (stage / ".snapshot.json").unlink()
        if root.exists():
            require(root.is_dir() and not any(root.iterdir()), "Restore destination changed; refusing overwrite")
            root.rmdir()  # Only an empty directory, never recursive deletion.
        stage.rename(root)
    return {"version": version, "verified": True, "restored": True, "service_installed": False}


@contextlib.contextmanager
def operation_lock(root):
    import fcntl
    path = root / ".cloudreve-manager.lock"
    no_links(path)
    require(not path.exists() or path.is_file(), "Operation lock must be an ordinary file")
    with path.open("a+b") as stream:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ManagerError("Another manager operation holds this data root lock") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def status(root, service):
    config = validate_data(root)
    state = service.inspect()
    unmanaged, sockets = process_inventory(service.proc, root, state)
    tcp = connections(service.proc, config["port"])
    release = current_release(root, optional=True)
    healthy = False
    if state["active"] and not unmanaged and not tcp["listeners"] - sockets:
        try:
            wait_healthy(config, timeout=0)
            healthy = True
        except ManagerError:
            pass
    return {"version": release.name if release else None, "db_version": DB_VERSION,
            "service": service.unit, "installed": state["exists"], "sandboxed": state["exists"] and not state["legacy"],
            "active": state["active"], "enabled": state["enabled"], "healthy": healthy, "active_connections": tcp["active"],
            "unmanaged": bool(unmanaged or tcp["listeners"] - sockets)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="absolute existing data root, or empty restore destination")
    parser.add_argument("--service", default="cloudreve-custom", help="dedicated systemd unit name")
    commands = parser.add_subparsers(dest="command", required=True)
    install_parser = commands.add_parser("install", help="verify/stage release, then activate unless --stage-only")
    install_parser.add_argument("--stage-only", action="store_true")
    rollback_parser = commands.add_parser("rollback", help="activate a previously verified release; never restore old DB")
    rollback_parser.add_argument("--version", required=True)
    service_parser = commands.add_parser("install-service", help="install, enable and start the dedicated unit")
    export_parser = commands.add_parser("export", help="stop managed service, write private archive, restart in finally")
    export_parser.add_argument("--output", required=True, type=Path)
    restore_parser = commands.add_parser("restore", help="validate and restore into an absent/empty destination")
    restore_parser.add_argument("--verify-only", action="store_true")
    commands.add_parser("status", help="read-only JSON status; no configuration or credentials are printed")
    for command in (install_parser, restore_parser):
        command.add_argument("--archive", required=True, type=Path)
        command.add_argument("--sha256", required=True, help="trusted expected SHA256, not a filename")
    for command in (install_parser, rollback_parser, service_parser, export_parser):
        command.add_argument("--allow-active-connections", action="store_true",
                             help="explicit maintenance override for TCP connections; never overrides unmanaged processes")
    args = parser.parse_args(argv)
    try:
        if args.command == "restore" and args.verify_only:
            result = restore(args.root, args.archive, args.sha256, verify_only=True)
        else:
            require(sys.platform == "linux" and os.geteuid() == 0, "Operations require root on Linux with systemd")
            root = check_root(args.root, existing=args.command != "restore")
            if args.command == "restore":
                result = restore(root, args.archive, args.sha256)
            else:
                service = Service(root, args.service)
                lock = contextlib.nullcontext() if args.command == "status" else operation_lock(root)
                with lock:
                    if args.command == "install":
                        result = install(root, args.archive, args.sha256, service, args.stage_only, args.allow_active_connections)
                    elif args.command == "rollback":
                        result = rollback(root, args.version, service, args.allow_active_connections)
                    elif args.command == "install-service":
                        result = install_service(root, service, args.allow_active_connections)
                    elif args.command == "export":
                        result = export(root, args.output, service, args.allow_active_connections)
                    else:
                        result = status(root, service)
        print(json.dumps(result, sort_keys=True))
        return 0
    except ManagerError as error:
        print("Error: " + str(error), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted; managed-service recovery was attempted", file=sys.stderr)
        return 130
    except (OSError, ValueError, tarfile.TarError, sqlite3.Error):
        # Exception messages can contain private filenames or configuration values.
        print("Error: filesystem/archive operation failed; check paths, free space and permissions locally", file=sys.stderr)
        return 2


if __name__ == "__main__":
    def interrupt(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupt)
    sys.exit(main())
