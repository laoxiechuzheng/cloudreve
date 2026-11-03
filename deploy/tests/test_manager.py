"""Standard-library tests. Run: python3 -m unittest discover -s deploy/tests -v."""
import contextlib
import errno
import hashlib
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "cloudreve-manager.py"
ARCH = "arm64" if platform.machine().lower() in ("aarch64", "arm64") else "amd64"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def put(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)


def database(root):
    db = sqlite3.connect(root / "cloudreve.db")
    db.executescript("""
        CREATE TABLE settings (name TEXT, value TEXT, deleted_at TEXT);
        INSERT INTO settings VALUES ('db_version_3.8.1','installed',NULL);
        INSERT INTO settings VALUES ('temp_path','temp',NULL);
        INSERT INTO settings VALUES ('avatar_path','avatar',NULL);
        CREATE TABLE policies (id INTEGER, type TEXT, dir_name_rule TEXT, server TEXT);
        INSERT INTO policies VALUES (1,'local','uploads/{uid}/{path}','');
        CREATE TABLE files (source_name TEXT, policy_id INTEGER, deleted_at TEXT);
        INSERT INTO files VALUES ('uploads/1/example.txt',1,NULL);
        CREATE TABLE nodes (type INTEGER, aria2_enabled INTEGER);
        INSERT INTO nodes VALUES (1,0);
        CREATE TABLE users (id INTEGER);
        CREATE TABLE groups (id INTEGER);
        CREATE TABLE folders (id INTEGER);
        CREATE TABLE shares (id INTEGER);
    """)
    db.close()


class Fixtures(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "deployment manager has not been implemented")
        spec = importlib.util.spec_from_file_location("cloudreve_manager", SCRIPT)
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        self.tmp = tempfile.TemporaryDirectory(prefix="cloudreve-test-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "data space \u4e91\u76d8"
        self.root.mkdir()
        put(self.root / "conf.ini", "[System]\nMode=master\nListen=:5212\n"
            "SessionSecret=test-only-session-value\nHashIDSalt=test-only-hash-salt\n")
        database(self.root)
        put(self.root / "uploads/1/example.txt", b"original upload\x00\xff")
        put(self.root / "uploads/1/\u4e2d\u6587 file.txt", "unicode filename")
        put(self.root / "temp/cache.gob", "cached sessions")
        put(self.root / "avatar/example.png", b"avatar")
        put(self.root / "cloudreve", b"legacy executable preserved")
        put(self.root / "statics/index.html", "legacy static override preserved")
        self.proc = self.base / "proc"
        put(self.proc / "net/tcp", "header\n")
        put(self.proc / "net/tcp6", "header\n")
        self.unit_dir = self.base / "units"
        self.unit_dir.mkdir()

    def release(self, version="3.8.3-custom.1", **changes):
        directory = self.base / ("release-" + str(len(list(self.base.glob("release-*")))))
        directory.mkdir()
        info = {"version": version, "backend_commit": "a" * 40,
                "frontend_commit": "b" * 40, "go_version": "go1.23.12",
                "platform": "linux/" + ARCH, "required_db_version": "3.8.1",
                "asset_version": "3.8.3"}
        info.update(changes)
        put(directory / "build-info.json", json.dumps(info))
        elf = bytearray(64)
        elf[:7] = b"\x7fELF\x02\x01\x01"
        struct.pack_into("<H", elf, 18, 183 if ARCH == "arm64" else 62)
        put(directory / "cloudreve", elf)
        (directory / "cloudreve").chmod(0o755)
        put(directory / "deploy/cloudreve-manager.py", "# packaged manager\n")
        put(directory / "LICENSE", "test license")
        put(directory / "README.md", "test release")
        archive = directory.with_suffix(".tar.gz")
        with tarfile.open(archive, "w:gz") as tf:
            for child in sorted(directory.iterdir()):
                tf.add(child, arcname=child.name)
        return archive

    def change_db(self, statement):
        with contextlib.closing(sqlite3.connect(self.root / "cloudreve.db")) as db:
            db.execute(statement)
            db.commit()

    def stage(self, version="3.8.3-custom.1"):
        archive = self.release(version)
        return self.m.stage_release(self.root, archive, digest(archive))

    def snapshot(self):
        release = self.stage()
        output = self.base / "private backup.tar.gz"
        self.m.write_backup(self.root, release, output)
        return output

    def service(self, active=False):
        service = self.m.Service(self.root, "cloudreve-custom", self.unit_dir, self.proc)
        state = {"active": active, "enabled": active, "fail_start": 0}

        def systemctl(*args):
            if args[0] == "show":
                exists = service.unit_path.is_file()
                return "\n".join([
                    "LoadState=" + ("loaded" if exists else "not-found"),
                    "ActiveState=" + ("active" if state["active"] else "inactive"),
                    "SubState=" + ("running" if state["active"] else "dead"),
                    "MainPID=" + ("43210" if state["active"] else "0"),
                    "ControlGroup=/system.slice/cloudreve-custom.service",
                    "FragmentPath=" + (str(service.unit_path) if exists else ""),
                    "DropInPaths=",
                    "UnitFileState=" + ("enabled" if state["enabled"] else "disabled"),
                ])
            if args[0] == "stop":
                state["active"] = False
                pid = self.proc / "43210"
                if pid.exists():
                    pid.rename(self.proc / "stopped")
            elif args[0] == "start":
                if state["fail_start"]:
                    state["fail_start"] -= 1
                    raise self.m.ManagerError("systemctl start failed")
                state["active"] = True
                stopped = self.proc / "stopped"
                if stopped.exists():
                    stopped.rename(self.proc / "43210")
                    exe = self.proc / "43210/exe"
                    exe.unlink()
                    exe.symlink_to(self.m.current_release(self.root) / "cloudreve")
                elif not (self.proc / "43210").exists():
                    pid = self.proc / "43210"
                    pid.mkdir()
                    (pid / "exe").symlink_to(self.m.current_release(self.root) / "cloudreve")
                    (pid / "cwd").symlink_to(self.root, target_is_directory=True)
                    put(pid / "cgroup", "0::/system.slice/cloudreve-custom.service\n")
                    put(pid / "cmdline", b"cloudreve\x00")
            elif args[0] == "enable":
                state["enabled"] = True
            elif args[0] == "disable":
                state["enabled"] = False
            elif args[0] != "daemon-reload":
                raise AssertionError("unexpected systemctl operation")
            return ""

        self.addCleanup(mock.patch.stopall)
        mock.patch.object(self.m, "systemctl", side_effect=systemctl).start()
        mock.patch.object(self.m, "wait_healthy").start()
        if active:
            put(service.unit_path, service.unit_text())
        return service, state


class ArchiveTests(Fixtures):
    def test_hardlinked_uploads_export_as_regular_files_and_restore(self):
        original = self.root / "uploads/1/example.txt"
        alias = self.root / "uploads/1/linked.txt"
        os.link(original, alias)
        archive = self.snapshot()
        with tarfile.open(archive) as tf:
            for name in ("uploads/1/example.txt", "uploads/1/linked.txt"):
                self.assertTrue(tf.getmember(name).isfile(), name)
                self.assertEqual(tf.extractfile(name).read(), b"original upload\x00\xff")
        result = self.m.restore(self.base / "hardlink-restore", archive, digest(archive), verify_only=True)
        self.assertTrue(result["verified"])

    @unittest.skipUnless(os.name == "nt", "Windows extraction-specific restriction")
    def test_windows_extraction_refuses_windows_reserved_aliases(self):
        archive = self.base / "windows-name.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            item = tarfile.TarInfo("uploads/CON")
            tf.addfile(item)
        with self.assertRaisesRegex(self.m.ManagerError, "Nonportable"):
            self.m.extract_checked(archive, digest(archive), self.base / "extract-windows")

    def test_wal_only_database_changes_are_detected_by_snapshot_watch(self):
        with contextlib.closing(sqlite3.connect(self.root / "cloudreve.db")) as db:
            db.execute("PRAGMA journal_mode=WAL")
            before = self.m.sqlite_fingerprint(self.root)
            original_db = digest(self.root / "cloudreve.db")
            db.execute("INSERT INTO users VALUES (55)")
            db.commit()
            self.assertEqual(digest(self.root / "cloudreve.db"), original_db)
            self.assertNotEqual(self.m.sqlite_fingerprint(self.root), before)

    def test_wal_transactions_are_included_in_sqlite_snapshot(self):
        with contextlib.closing(sqlite3.connect(self.root / "cloudreve.db")) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("INSERT INTO users VALUES (42)")
            db.commit()
            archive = self.snapshot()
            extracted = self.base / "wal-check"
            self.m.extract_checked(archive, digest(archive), extracted)
            with contextlib.closing(sqlite3.connect(extracted / "cloudreve.db")) as restored:
                self.assertEqual(restored.execute("SELECT id FROM users").fetchall(), [(42,)])

    def test_empty_static_override_cannot_be_added_to_verified_release(self):
        release = self.stage()
        (release / "statics").mkdir()
        with self.assertRaises(self.m.ManagerError):
            self.m.verify_release(release)

    def test_parent_manifest_full_go_output_and_binary_hash(self):
        archive = self.release("3.8.3-custom.dev-1234abcd", go_version="go version go1.20.14 linux/amd64",
                               backend_version="3.8.3")
        folder = archive.with_suffix("").with_suffix("")
        manifest = json.loads((folder / "build-info.json").read_text())
        manifest["binary_sha256"] = digest(folder / "cloudreve")
        put(folder / "build-info.json", json.dumps(manifest))
        with tarfile.open(archive, "w:gz") as tf:
            for child in folder.iterdir():
                tf.add(child, arcname=child.name)
        result = self.m.stage_release(self.root, archive, digest(archive))
        self.assertEqual(result.name, "3.8.3-custom.dev-1234abcd")

    def test_binary_hash_and_backend_version_mismatch_refused(self):
        for changes in ({"binary_sha256": "0" * 64}, {"backend_version": "4.0.0"}):
            archive = self.release(**changes)
            with self.assertRaises(self.m.ManagerError):
                self.m.stage_release(self.root, archive, digest(archive))

    def test_checksum_failure_creates_no_release(self):
        archive = self.release()
        with self.assertRaisesRegex(self.m.ManagerError, "SHA256"):
            self.m.stage_release(self.root, archive, "0" * 64)
        self.assertFalse((self.root / "releases").exists())

    def test_provenance_recorded_without_touching_data(self):
        original = (self.root / "conf.ini").read_bytes()
        release = self.stage()
        self.assertEqual(release.name, "3.8.3-custom.1")
        self.assertEqual((release / "cloudreve").read_bytes()[:4], b"\x7fELF")
        self.assertEqual((self.root / "conf.ini").read_bytes(), original)
        self.assertTrue((release / ".release.json").is_file())
        self.assertFalse((self.root / "current").exists())

    def test_wrong_version_architecture_provenance_or_schema_is_refused(self):
        for changes in ({"version": "4.0.0"}, {"version": "3.8.30"},
                        {"platform": "linux/386"}, {"backend_commit": "main"},
                        {"frontend_commit": ""}, {"go_version": "unknown"},
                        {"required_db_version": "3.8.2"}, {"asset_version": "4.0.0"}):
            with self.subTest(changes=changes):
                archive = self.release(**changes)
                with self.assertRaises(self.m.ManagerError):
                    self.m.stage_release(self.root, archive, digest(archive))

    def test_binary_elf_must_match_manifest(self):
        archive = self.release()
        folder = archive.with_suffix("").with_suffix("")
        put(folder / "cloudreve", b"#!/bin/sh\nexit 0\n")
        with tarfile.open(archive, "w:gz") as tf:
            for child in folder.iterdir():
                tf.add(child, arcname=child.name)
        with self.assertRaisesRegex(self.m.ManagerError, "ELF"):
            self.m.stage_release(self.root, archive, digest(archive))

    def test_same_version_cannot_be_replaced_by_another_archive(self):
        release = self.stage()
        before = (release / ".release.json").read_bytes()
        archive = self.release(backend_commit="c" * 40)
        with self.assertRaisesRegex(self.m.ManagerError, "already exists"):
            self.m.stage_release(self.root, archive, digest(archive))
        self.assertEqual((release / ".release.json").read_bytes(), before)

    def test_traversal_absolute_links_special_and_duplicates_are_refused(self):
        cases = [("../escape", tarfile.REGTYPE, ""), ("/absolute", tarfile.REGTYPE, ""),
                 ("C:/escape", tarfile.REGTYPE, ""), ("a\\..\\escape", tarfile.REGTYPE, ""),
                 ("link", tarfile.SYMTYPE, "../../escape"), ("hard", tarfile.LNKTYPE, "elsewhere"),
                 ("pipe", tarfile.FIFOTYPE, ""), ("duplicate", tarfile.REGTYPE, "")]
        for name, kind, target in cases:
            with self.subTest(name=name):
                archive = self.base / "bad.tar.gz"
                with tarfile.open(archive, "w:gz") as tf:
                    member = tarfile.TarInfo(name)
                    member.type, member.linkname = kind, target
                    tf.addfile(member)
                    if name == "duplicate":
                        tf.addfile(member)
                with self.assertRaises(self.m.ManagerError):
                    self.m.extract_checked(archive, digest(archive), self.base / "extract")
                self.assertFalse((self.base / "escape").exists())

    def test_private_backup_roundtrip_validation_preserves_source(self):
        before = (self.root / "conf.ini").read_bytes()
        archive = self.snapshot()
        target = self.base / "restored \u65b0\u673a"
        result = self.m.restore(target, archive, digest(archive), verify_only=True)
        self.assertEqual(result["version"], "3.8.3-custom.1")
        self.assertFalse(target.exists())
        self.assertEqual((self.root / "conf.ini").read_bytes(), before)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
        with tarfile.open(archive) as tf:
            self.assertEqual(tf.extractfile("uploads/1/example.txt").read(), b"original upload\x00\xff")
            self.assertEqual(tf.extractfile("conf.ini").read(), before)
            self.assertEqual(tf.extractfile("cloudreve").read(), b"legacy executable preserved")

    def test_restore_never_overwrites_existing_root_or_source(self):
        archive = self.snapshot()
        before = digest(archive)
        with self.assertRaisesRegex(self.m.ManagerError, "empty"):
            self.m.restore(self.root, archive, before)
        self.assertEqual(digest(archive), before)
        self.assertEqual((self.root / "uploads/1/example.txt").read_bytes(), b"original upload\x00\xff")

    def test_restore_rejects_checksum_before_creating_destination(self):
        archive = self.snapshot()
        target = self.base / "absent"
        with self.assertRaisesRegex(self.m.ManagerError, "SHA256"):
            self.m.restore(target, archive, "1" * 64)
        self.assertFalse(target.exists())

    def test_restore_checks_member_hash_even_if_outer_hash_updated(self):
        archive = self.snapshot()
        changed = self.base / "changed.tar.gz"
        with tarfile.open(archive) as src, tarfile.open(changed, "w:gz") as dst:
            for member in src:
                content = src.extractfile(member).read() if member.isfile() else None
                if member.name == "uploads/1/example.txt":
                    content = b"tampered"
                    member.size = len(content)
                dst.addfile(member, io.BytesIO(content) if content is not None else None)
        with self.assertRaisesRegex(self.m.ManagerError, "manifest|hash"):
            self.m.restore(self.base / "absent", changed, digest(changed), verify_only=True)

    def test_backup_includes_empty_directories_and_refuses_existing_output(self):
        (self.root / "uploads/empty").mkdir()
        release = self.stage()
        output = self.base / "backup.tar.gz"
        self.m.write_backup(self.root, release, output)
        with tarfile.open(output) as tf:
            self.assertTrue(tf.getmember("uploads/empty").isdir())
        original = digest(output)
        with self.assertRaises(self.m.ManagerError):
            self.m.write_backup(self.root, release, output)
        self.assertEqual(digest(output), original)


class ValidationTests(Fixtures):
    def test_working_directory_uses_unquoted_path_and_literal_specifiers(self):
        renamed = self.base / "data %n $HOME \u4e91\u76d8"
        self.root.rename(renamed)
        self.root = renamed
        service, state = self.service()
        directives = dict(line.split("=", 1) for line in service.unit_text().splitlines() if "=" in line)
        expected = str(self.base.resolve() / "data %%n $HOME \u4e91\u76d8")
        self.assertEqual(directives["WorkingDirectory"], expected)
        self.assertTrue(directives["ExecStart"].startswith(':"'))

    def test_working_directory_and_exec_argument_have_distinct_escaping(self):
        value = '/srv/cloud "copy" %n $HOME \u4e91\u76d8'
        self.assertEqual(self.m.unit_working_directory(value), '/srv/cloud "copy" %%n $HOME \u4e91\u76d8')
        self.assertEqual(self.m.unit_quote(value), '"/srv/cloud \\"copy\\" %%n $HOME \u4e91\u76d8"')

    def test_linux_names_are_valid_but_windows_extraction_can_reject_them(self):
        for name in ("uploads/CON", "uploads/NUL.txt", "uploads/file.", "uploads/file ", "uploads/file:tag"):
            with self.subTest(name=name):
                self.assertEqual(self.m.member_name(name), name)
                with self.assertRaisesRegex(self.m.ManagerError, "Nonportable"):
                    self.m.member_name(name, windows=True)

    def test_checkpoint_refuses_empty_database_without_initializing_it(self):
        put(self.root / "cloudreve.db", b"")
        with self.assertRaises(self.m.ManagerError):
            self.m.checkpoint_database(self.root)
        self.assertEqual((self.root / "cloudreve.db").read_bytes(), b"")

    def test_operational_retention_keeps_three_and_unresolved_and_data_exports(self):
        service, state = self.service()
        backups = []
        for index in range(5):
            backup = self.m.backup_operations(self.root, service)
            backups.append(backup)
            if index != 0:
                self.m.resolve_backup(self.root, backup)
        self.assertTrue((backups[0] / "conf.ini").exists())
        self.assertFalse((backups[1] / "conf.ini").exists())
        self.assertTrue(all((path / "conf.ini").exists() for path in backups[2:]))
        put(self.root / "backups/data-export.tar.gz", b"never prune data")
        extra = self.m.backup_operations(self.root, service)
        self.m.resolve_backup(self.root, extra)
        self.assertEqual((self.root / "backups/data-export.tar.gz").read_bytes(), b"never prune data")

    def test_missing_stored_upload_refuses_complete_validation(self):
        (self.root / "uploads/1/example.txt").unlink()
        with self.assertRaisesRegex(self.m.ManagerError, "missing"):
            self.m.validate_data(self.root, full=True)

    def test_systemctl_not_found_show_is_valid_absent_state(self):
        response = subprocess.CompletedProcess([], 1, "LoadState=not-found\nActiveState=inactive\n", "")
        with mock.patch.object(self.m.subprocess, "run", return_value=response):
            self.assertIn("not-found", self.m.systemctl("show", "unused.service"))

    def test_probe_requires_actual_success_json_and_exact_version(self):
        payload = {"code": 0, "data": "3.8.3"}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = {"host": "127.0.0.1", "port": server.server_port}
            self.m.wait_healthy(config, timeout=0)
            for bad in ({"code": 0, "data": "4.0.0"}, {"code": True, "data": "3.8.3"},
                        {"code": 1, "data": "3.8.3"}):
                payload = bad
                with self.assertRaises(self.m.ManagerError):
                    self.m.wait_healthy(config, timeout=0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_missing_or_newer_db_marker_refuses_activation(self):
        for sql in ("DELETE FROM settings WHERE name LIKE 'db_version_%'",
                    "INSERT INTO settings VALUES ('db_version_4.0.0','installed',NULL)"):
            self.change_db(sql)
            with self.assertRaisesRegex(self.m.ManagerError, "database version"):
                self.m.validate_data(self.root)
            self.change_db("INSERT INTO settings VALUES ('db_version_3.8.1','installed',NULL)")

    def test_soft_deleted_marker_does_not_skip_migration(self):
        self.change_db("UPDATE settings SET deleted_at='2020-01-01' WHERE name LIKE 'db_version_%'")
        with self.assertRaisesRegex(self.m.ManagerError, "database version"):
            self.m.validate_data(self.root)

    def test_corrupt_sqlite_is_refused(self):
        put(self.root / "cloudreve.db", b"not a database")
        with self.assertRaisesRegex(self.m.ManagerError, "SQLite"):
            self.m.validate_data(self.root)

    def test_external_db_cache_and_unix_or_tls_listener_refused(self):
        original = (self.root / "conf.ini").read_text()
        for extra in ("[Database]\nType=mysql\n", "[Database]\nDBFile=/outside.db\n",
                      "[Redis]\nServer=127.0.0.1:6379\n", "[UnixSocket]\nListen=/run/app.sock\n",
                      "[SSL]\nCertPath=/cert.pem\n", "[OptionOverwrite]\ntemp_path=/outside\n"):
            with self.subTest(extra=extra):
                put(self.root / "conf.ini", original + extra)
                with self.assertRaises(self.m.ManagerError):
                    self.m.validate_data(self.root)

    def test_storage_rules_and_historical_source_names_checked(self):
        cases = ["UPDATE policies SET type='s3'", "UPDATE policies SET dir_name_rule='/outside/{uid}'",
                 "UPDATE policies SET dir_name_rule='uploads/../../outside'",
                 "UPDATE files SET source_name='/outside/file'", "UPDATE files SET source_name='uploads/../outside'",
                 "UPDATE files SET policy_id=99", "UPDATE nodes SET aria2_enabled=1"]
        for sql in cases:
            with self.subTest(sql=sql):
                self.change_db(sql)
                with self.assertRaises(self.m.ManagerError):
                    self.m.validate_data(self.root)
                (self.root / "cloudreve.db").unlink()
                database(self.root)

    def test_config_and_secrets_remain_unchanged_and_unprinted(self):
        before = (self.root / "conf.ini").read_bytes()
        data = self.m.validate_data(self.root)
        self.assertEqual(data["port"], 5212)
        self.assertEqual((self.root / "conf.ini").read_bytes(), before)
        self.assertNotIn("test-only-session-value", repr(data))
        self.assertNotIn("test-only-hash-salt", repr(data))

    def test_tcp_connections_count_configured_port_only(self):
        put(self.proc / "net/tcp", "header\n"
            "0: 0100007F:145C 0100007F:ABCD 01 00000000:00000000 00:0 0 0 0 123\n"
            "1: 0100007F:145C 00000000:0000 0A 00000000:00000000 00:0 0 0 0 124\n"
            "2: 0100007F:145D 0100007F:ABCD 01 00000000:00000000 00:0 0 0 0 125\n")
        info = self.m.connections(self.proc, 5212)
        self.assertEqual(info["active"], 1)
        self.assertEqual(info["listeners"], {"124"})

    def test_unmanaged_port_refused_even_with_override(self):
        service, state = self.service()
        put(self.proc / "net/tcp", "header\n"
            "0: 0100007F:145C 00000000:0000 0A 00000000:00000000 00:0 0 0 0 124\n")
        for override in (False, True):
            with self.assertRaisesRegex(self.m.ManagerError, "unmanaged"):
                service.guard(5212, allow_active=override)
        self.assertFalse(state["active"])

    def test_foreign_service_unit_is_not_overwritten(self):
        service, state = self.service()
        put(service.unit_path, "[Service]\nExecStart=/bin/true\n")
        original = service.unit_path.read_bytes()
        with self.assertRaisesRegex(self.m.ManagerError, "unit"):
            service.inspect()
        self.assertEqual(service.unit_path.read_bytes(), original)


@unittest.skipUnless(os.name == "posix", "requires Linux symlinks and /proc; run on Linux CI")
class LifecycleTests(Fixtures):
    def test_linux_reserved_windows_names_survive_export_restore(self):
        for name in ("CON", "file.", "file ", "file:tag"):
            put(self.root / "uploads/1" / name, "valid Linux filename")
        archive = self.snapshot()
        target = self.base / "linux-names-restored"
        self.m.restore(target, archive, digest(archive))
        for name in ("CON", "file.", "file ", "file:tag"):
            self.assertEqual((target / "uploads/1" / name).read_text(), "valid Linux filename")

    def test_alias_recovery_symlink_allocation_failure_keeps_original_binding(self):
        release = self.stage()
        self.m.bind_data(self.root, release)
        self.alias_wal(release)
        alias = release / "cloudreve.db"
        with mock.patch.object(Path, "symlink_to", side_effect=OSError(errno.ENOSPC, "simulated full disk")):
            with self.assertRaises(OSError):
                self.m.checkpoint_database(self.root, release)
        self.assertTrue(alias.is_symlink())
        self.assertEqual(alias.resolve(), self.root / "cloudreve.db")
        self.m.verify_release(release)

    def test_alias_cleanup_does_not_allocate_symlink_after_alias_mutation(self):
        release = self.stage()
        self.m.bind_data(self.root, release)
        self.alias_wal(release)
        alias = release / "cloudreve.db"
        create_link = Path.symlink_to

        def allocate(path, *args, **kwargs):
            if not alias.is_symlink():
                raise OSError(errno.ENOSPC, "simulated full disk after alias mutation")
            return create_link(path, *args, **kwargs)

        with mock.patch.object(Path, "symlink_to", new=allocate):
            self.m.checkpoint_database(self.root, release)
        self.assertTrue(alias.is_symlink())
        self.m.verify_release(release)

    def test_rollback_can_leave_corrupt_current_binary_for_verified_target(self):
        damaged, service, state = self.prepare_managed()
        good = self.stage("3.8.3-custom.2")
        before = (self.root / "conf.ini").read_bytes()
        put(damaged / "cloudreve", b"corrupt current executable")
        self.m.rollback(self.root, good.name, service)
        self.assertEqual(self.m.current_release(self.root), good)
        self.assertTrue(state["active"])
        self.assertEqual((self.root / "conf.ini").read_bytes(), before)

    def test_rollback_still_refuses_escaped_current_data_binding(self):
        current, service, state = self.prepare_managed()
        good = self.stage("3.8.3-custom.2")
        (current / "uploads").unlink()
        (current / "uploads").symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(self.m.ManagerError):
            self.m.rollback(self.root, good.name, service)
        self.assertTrue(state["active"])
        self.assertEqual(os.readlink(self.root / "current"), "releases/" + current.name)

    def test_install_service_checkpoints_alias_wal_before_start(self):
        release = self.stage()
        self.m.bind_data(self.root, release)
        self.m.switch_current(self.root, release.name)
        self.alias_wal(release)
        service, state = self.service()
        self.m.install_service(self.root, service)
        with contextlib.closing(sqlite3.connect(self.root / "cloudreve.db")) as db:
            self.assertEqual(db.execute("SELECT id FROM users").fetchall(), [(77,)])
        self.assertTrue(state["active"])

    def test_export_recovers_hot_release_alias_journal(self):
        release = self.stage()
        self.m.bind_data(self.root, release)
        self.m.switch_current(self.root, release.name)
        alias = release / "cloudreve.db"
        alias.unlink()
        os.link(self.root / "cloudreve.db", alias)
        script = ("import os,sqlite3,sys; db=sqlite3.connect(sys.argv[1]); "
                  "db.execute('PRAGMA journal_mode=DELETE'); db.execute('PRAGMA cache_size=1'); "
                  "db.execute('BEGIN IMMEDIATE'); "
                  "db.executemany('INSERT INTO users VALUES (?)', ((i,) for i in range(3000))); os._exit(0)")
        subprocess.run([sys.executable, "-c", script, str(alias)], check=True)
        alias.unlink()
        alias.symlink_to("../../cloudreve.db")
        self.assertGreater((release / "cloudreve.db-journal").stat().st_size, 0)
        service, state = self.service()
        output = self.root / "backups/hot-journal.tar.gz"
        self.m.export(self.root, output, service)
        with contextlib.closing(sqlite3.connect(self.root / "cloudreve.db")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM users").fetchone(), (0,))
        self.assertTrue(alias.is_symlink())

    def test_install_service_enables_owned_unit_and_starts_current_release(self):
        release = self.stage()
        self.m.bind_data(self.root, release)
        self.m.switch_current(self.root, release.name)
        service, state = self.service()
        result = self.m.install_service(self.root, service)
        self.assertTrue(result["active"])
        self.assertTrue(state["enabled"])
        self.assertEqual(service.unit_path.read_text(), service.unit_text())
        self.assertTrue((self.root / ".cloudreve-manager/service.json").is_file())

    def test_unit_is_sandboxed_to_the_data_root(self):
        service, state = self.service()
        directives = dict(line.split("=", 1) for line in service.unit_text().splitlines() if "=" in line)
        self.assertEqual(directives["ProtectSystem"], "strict")
        self.assertEqual(directives["ProtectHome"], "read-only")
        self.assertEqual(directives["ReadWritePaths"], self.m.unit_quote(self.root))
        self.assertEqual(directives["NoNewPrivileges"], "yes")
        self.assertEqual(directives["CapabilityBoundingSet"], "")
        self.assertNotIn("ProtectSystem", service.unit_text(hardened=False))

    def test_install_service_upgrades_legacy_unit_and_restarts(self):
        release = self.stage()
        self.m.bind_data(self.root, release)
        self.m.switch_current(self.root, release.name)
        service, state = self.service()
        put(service.unit_path, service.unit_text(hardened=False))
        self.assertTrue(service.inspect()["legacy"])
        state["active"] = state["enabled"] = True
        pid = self.proc / "43210"
        pid.mkdir()
        (pid / "exe").symlink_to(release / "cloudreve")
        (pid / "cwd").symlink_to(self.root, target_is_directory=True)
        put(pid / "cgroup", "0::/system.slice/cloudreve-custom.service\n")
        put(pid / "cmdline", b"cloudreve\x00")
        calls = []
        stop = service.stop
        mock.patch.object(service, "stop", side_effect=lambda: (calls.append("stop"), stop())).start()
        result = self.m.install_service(self.root, service)
        self.assertTrue(result["active"])
        self.assertEqual(calls, ["stop"])
        self.assertEqual(service.unit_path.read_text(), service.unit_text())
        self.assertFalse(service.inspect()["legacy"])

    def test_install_service_upgrade_failure_restores_legacy_unit(self):
        release = self.stage()
        self.m.bind_data(self.root, release)
        self.m.switch_current(self.root, release.name)
        service, state = self.service()
        legacy = service.unit_text(hardened=False)
        put(service.unit_path, legacy)
        state["fail_start"] = 1
        with self.assertRaisesRegex(self.m.ManagerError, "installation failed"):
            self.m.install_service(self.root, service)
        self.assertEqual(service.unit_path.read_text(), legacy)

    def test_install_service_failure_removes_new_unit_preserving_data(self):
        release = self.stage()
        self.m.bind_data(self.root, release)
        self.m.switch_current(self.root, release.name)
        service, state = self.service()
        state["fail_start"] = 1
        before = (self.root / "conf.ini").read_bytes()
        with self.assertRaisesRegex(self.m.ManagerError, "installation failed"):
            self.m.install_service(self.root, service)
        self.assertFalse(service.unit_path.exists())
        self.assertFalse(state["enabled"])
        self.assertFalse(state["active"])
        self.assertEqual((self.root / "conf.ini").read_bytes(), before)
        self.assertEqual(self.m.current_release(self.root), release)

    def alias_wal(self, release):
        alias = release / "cloudreve.db"
        alias.unlink()
        os.link(self.root / "cloudreve.db", alias)
        script = ("import os,sqlite3,sys; db=sqlite3.connect(sys.argv[1]); "
                  "db.execute('PRAGMA journal_mode=WAL'); "
                  "db.execute('INSERT INTO users VALUES (77)'); db.commit(); os._exit(0)")
        subprocess.run([sys.executable, "-c", script, str(alias)], check=True)
        alias.unlink()
        alias.symlink_to("../../cloudreve.db")
        self.assertGreater((release / "cloudreve.db-wal").stat().st_size, 0)

    def test_export_checkpoints_release_alias_wal_into_source_before_snapshot(self):
        release = self.stage()
        self.m.bind_data(self.root, release)
        self.m.switch_current(self.root, release.name)
        self.alias_wal(release)
        service, state = self.service()
        output = self.root / "backups/alias-wal.tar.gz"
        self.m.export(self.root, output, service)
        self.assertTrue((release / "cloudreve.db").is_symlink())
        with contextlib.closing(sqlite3.connect(self.root / "cloudreve.db")) as db:
            self.assertEqual(db.execute("SELECT id FROM users").fetchall(), [(77,)])
        extracted = self.base / "exported"
        self.m.extract_checked(output, digest(output), extracted)
        with contextlib.closing(sqlite3.connect(extracted / "cloudreve.db")) as db:
            self.assertEqual(db.execute("SELECT id FROM users").fetchall(), [(77,)])

    def test_conflicting_root_and_alias_wal_is_refused_without_deleting_either(self):
        release = self.stage()
        self.m.bind_data(self.root, release)
        self.alias_wal(release)
        alias_hash = digest(release / "cloudreve.db-wal")
        put(self.root / "cloudreve.db-wal", b"other unresolved WAL")
        with self.assertRaisesRegex(self.m.ManagerError, "conflict"):
            self.m.checkpoint_database(self.root, release)
        self.assertEqual(digest(release / "cloudreve.db-wal"), alias_hash)
        self.assertEqual((self.root / "cloudreve.db-wal").read_bytes(), b"other unresolved WAL")

    def prepare_managed(self):
        release = self.stage()
        self.m.bind_data(self.root, release)
        self.m.switch_current(self.root, release.name)
        service, state = self.service(active=True)
        pid = self.proc / "43210"
        pid.mkdir()
        (pid / "exe").symlink_to(release / "cloudreve")
        (pid / "cwd").symlink_to(self.root, target_is_directory=True)
        put(pid / "cgroup", "0::/system.slice/cloudreve-custom.service\n")
        put(pid / "cmdline", b"cloudreve\x00-c\x00" + os.fsencode(self.root / "conf.ini") + b"\x00")
        return release, service, state

    def test_activation_relative_data_links_and_legacy_preservation(self):
        archive = self.release()
        service, state = self.service()
        self.m.install(self.root, archive, digest(archive), service)
        self.assertEqual(os.readlink(self.root / "current"), "releases/3.8.3-custom.1")
        self.assertEqual((self.root / "current/uploads/1/example.txt").read_bytes(), b"original upload\x00\xff")
        self.assertEqual((self.root / "cloudreve").read_bytes(), b"legacy executable preserved")
        self.assertTrue((self.root / "statics/index.html").exists())
        self.assertFalse((self.root / "current/statics").exists())
        self.assertFalse(state["active"])

    def test_active_connections_require_explicit_override(self):
        old, service, state = self.prepare_managed()
        put(self.proc / "net/tcp", "header\n"
            "0: 0100007F:145C 0100007F:ABCD 01 00000000:00000000 00:0 0 0 0 123\n")
        with self.assertRaisesRegex(self.m.ManagerError, "Active TCP"):
            service.guard(5212)
        self.assertTrue(service.guard(5212, allow_active=True)["active"])
        self.assertTrue(state["active"])

    def test_update_start_failure_restores_old_release_and_service(self):
        old, service, state = self.prepare_managed()
        archive = self.release("3.8.3-custom.2")
        state["fail_start"] = 1
        with self.assertRaisesRegex(self.m.ManagerError, "rolled back"):
            self.m.install(self.root, archive, digest(archive), service)
        self.assertEqual(self.m.current_release(self.root), old)
        self.assertTrue(state["active"])

    def test_export_success_restarts_and_emits_private_checksum(self):
        old, service, state = self.prepare_managed()
        output = self.root / "backups/export.tar.gz"
        result = self.m.export(self.root, output, service)
        self.assertTrue(state["active"])
        self.assertEqual(result["sha256"], digest(output))
        sidecar = Path(str(output) + ".sha256")
        self.assertEqual(sidecar.read_text().split()[0], digest(output))
        self.assertEqual(stat.S_IMODE(sidecar.stat().st_mode), 0o600)
        self.m.restore(self.base / "verified", output, digest(output), verify_only=True)

    def test_nested_storage_symlink_refused_before_activation(self):
        (self.root / "uploads/escape").symlink_to(self.base, target_is_directory=True)
        archive = self.release()
        service, state = self.service()
        with self.assertRaisesRegex(self.m.ManagerError, "link"):
            self.m.install(self.root, archive, digest(archive), service)
        self.assertFalse((self.root / "current").exists())

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze not available")
    def test_generated_unit_parses_with_spaces_unicode_and_specifiers(self):
        renamed = self.base / "data %n $HOME \u4e91\u76d8"
        self.root.rename(renamed)
        self.root = renamed
        release = self.stage()
        self.m.bind_data(self.root, release)
        self.m.switch_current(self.root, release.name)
        service = self.m.Service(self.root, "cloudreve-custom-test", self.unit_dir, self.proc)
        put(service.unit_path, service.unit_text())
        result = subprocess.run(["systemd-analyze", "verify", str(service.unit_path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_restore_verified_data_and_relative_links(self):
        archive = self.snapshot()
        target = self.base / "new \u4e91\u76d8"
        self.m.restore(target, archive, digest(archive))
        self.assertEqual((target / "uploads/1/example.txt").read_bytes(), b"original upload\x00\xff")
        self.assertEqual((target / "conf.ini").read_bytes(), (self.root / "conf.ini").read_bytes())
        self.assertEqual(os.readlink(target / "current"), "releases/3.8.3-custom.1")
        self.assertEqual((target / "current/cloudreve.db").resolve(), target / "cloudreve.db")

    def test_symlinked_data_refused(self):
        (self.root / "uploads/outside").symlink_to(self.base, target_is_directory=True)
        with self.assertRaisesRegex(self.m.ManagerError, "link"):
            self.snapshot()

    def test_unmanaged_matching_cwd_is_never_stopped(self):
        service, state = self.service()
        pid = self.proc / "12345"
        pid.mkdir()
        (pid / "exe").symlink_to(self.root / "cloudreve")
        (pid / "cwd").symlink_to(self.root, target_is_directory=True)
        put(pid / "cmdline", b"./cloudreve\x00")
        put(pid / "cgroup", "0::/user.slice/tmux.scope\n")
        with self.assertRaisesRegex(self.m.ManagerError, "unmanaged"):
            service.guard(5212, allow_active=True)
        self.assertFalse(state["active"])

    def test_health_failure_rolls_back_binary_without_restoring_database(self):
        old, service, state = self.prepare_managed()
        archive = self.release("3.8.3-custom.2")
        original_config = (self.root / "conf.ini").read_bytes()

        def health(*args, **kwargs):
            if self.m.current_release(self.root).name == "3.8.3-custom.2":
                self.change_db("INSERT INTO users VALUES (99)")
                raise self.m.ManagerError("health probe failed")

        with mock.patch.object(self.m, "wait_healthy", side_effect=health):
            with self.assertRaisesRegex(self.m.ManagerError, "rolled back"):
                self.m.install(self.root, archive, digest(archive), service)
        self.assertEqual(self.m.current_release(self.root), old)
        self.assertTrue(state["active"])
        self.assertEqual((self.root / "conf.ini").read_bytes(), original_config)
        with contextlib.closing(sqlite3.connect(self.root / "cloudreve.db")) as db:
            self.assertEqual(db.execute("SELECT id FROM users").fetchall(), [(99,)])

    def test_export_failure_restarts_service_and_retains_old_backups(self):
        release, service, state = self.prepare_managed()
        put(self.root / "backups/older-database.tar.gz", b"keep forever")
        (self.root / "uploads/bad-link").symlink_to(self.base, target_is_directory=True)
        output = self.root / "backups/failed.tar.gz"
        with self.assertRaises(self.m.ManagerError):
            self.m.export(self.root, output, service)
        self.assertTrue(state["active"])
        self.assertFalse(output.exists())
        self.assertEqual((self.root / "backups/older-database.tar.gz").read_bytes(), b"keep forever")

    def test_rollback_rechecks_release_hash(self):
        old, service, state = self.prepare_managed()
        target = self.stage("3.8.3-custom.2")
        put(target / "cloudreve", b"tampered executable")
        with self.assertRaises(self.m.ManagerError):
            self.m.rollback(self.root, target.name, service)
        self.assertEqual(self.m.current_release(self.root), old)
        self.assertTrue(state["active"])


if __name__ == "__main__":
    unittest.main()
