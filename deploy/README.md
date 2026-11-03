# Cloudreve 3.8.3 Deployment and Migration

`cloudreve-manager.py` operates an **existing** Cloudreve 3.8.3 data directory.
It never initializes accounts, generates credentials, edits `conf.ini`, runs
database migrations, or downloads/executes a release before checking its hash.
Python 3.9+ with SQLite support, Linux amd64/arm64, systemd 243+, and host `/proc`
access are required. There are no Python packages to install. Run host operations
as root; archive verification and most tests also work on Windows.

## Command Interface

Global arguments precede the command. `--service` defaults to
`cloudreve-custom`; use a different dedicated name for each data root.
Commands return JSON on success and exit 2 on a rejected operation. Error output
does not include configuration contents, database values, or private filenames.

```sh
python3 deploy/cloudreve-manager.py --root "/srv/cloudreve data" --service cloudreve-custom status
python3 deploy/cloudreve-manager.py --root "/srv/cloudreve data" install --archive release.tar.gz --sha256 "$RELEASE_SHA256" --stage-only
python3 deploy/cloudreve-manager.py --root "/srv/cloudreve data" install --archive release.tar.gz --sha256 "$RELEASE_SHA256"
python3 deploy/cloudreve-manager.py --root "/srv/cloudreve data" install-service
python3 deploy/cloudreve-manager.py --root "/srv/cloudreve data" rollback --version 3.8.3-custom.1
python3 deploy/cloudreve-manager.py --root "/srv/cloudreve data" export --output "/srv/cloudreve data/backups/migration.tar.gz"
python3 deploy/cloudreve-manager.py --root "/srv/new cloudreve" restore --archive migration.tar.gz --sha256 "$BACKUP_SHA256" --verify-only
python3 deploy/cloudreve-manager.py --root "/srv/new cloudreve" restore --archive migration.tar.gz --sha256 "$BACKUP_SHA256"
```

`--sha256` takes the **trusted 64-character digest**, not a checksum filename.
Obtain release hashes from the authenticated build/release channel and retain
the export digest independently during transfer. A checksum establishes identity
with that digest; an untrusted archive's own manifest is not a signature.

For `install`, `rollback`, `install-service`, and `export`, an explicit
`--allow-active-connections` override is available for a controlled maintenance
window. It overrides only the conservative TCP connection check. It never
overrides unmanaged processes, file writers, foreign units, unsafe paths, schema
checks, or integrity checks. The check counts established and close-wait TCP
connections on the configured port, including idle HTTP keepalives. Drain traffic
at the proxy first; do not assume every connection is an idle keepalive.

## Release Contract

The gzip tar archive has these entries directly at its top level, with no
enclosing version directory:

```text
cloudreve
build-info.json
deploy/
LICENSE
README.md
```

`build-info.json` requires `version`, `backend_commit`, `frontend_commit`,
`go_version`, and `platform`. Version names are `3.8.3` or unique names beginning
`3.8.3-` / `3.8.3+`, such as `3.8.3-custom.1` or
`3.8.3-custom.dev-1234abcd`. Commits are full 40-character hashes. `go_version`
accepts `go1.20.14` or the full `go version go1.20.14 linux/amd64` output.
`platform` is `linux/amd64` or `linux/arm64` and must match the destination.
The executable's ELF header is also checked. If supplied, `backend_version`
must be `3.8.3`, `asset_version` must be `3.8.3`, `required_db_version` must be
`3.8.1`, and `binary_sha256` must match the executable. The parent packaging
script supplies all of these additional fields.

Archives containing absolute paths, traversal, duplicate normalized names,
symlinks, hard links, special files, sparse members, or special permission bits
are rejected. Extraction uses explicitly validated regular files/directories;
it does not call `tar.extractall`. Symlinks are created only by the manager after
validation. Do not include Python bytecode or local data in a release.
Linux filenames such as `CON`, trailing-dot/space names and colon-containing
names are preserved. Windows extraction rejects its device-name/path aliases
before writing; use Linux to verify archives containing those names.

## Layout and Initial Cutover

```text
data-root/
  conf.ini                    existing, unchanged
  cloudreve.db                existing, no schema changes
  uploads/  avatar/  temp/     existing storage and cache
  cloudreve                   original executable, preserved if present
  statics/                    original static override, preserved if present
  releases/<version>/         immutable verified release plus controlled links
  current -> releases/<version>
  backups/                    operational copies and private data exports
  .cloudreve-manager/          generated unit/provenance copies
  .cloudreve-manager.lock      advisory lock for this manager
```

Upstream `pkg/util/path.go:RelativePath` anchors local files to the actual
executable location, **not the working directory**. Each release therefore has
relative `cloudreve.db`, `uploads`, `avatar`, and `temp` links pointing two levels
up to the data root. The unit still uses the data root as its working directory
and runs `current/cloudreve -c <data-root>/conf.ini`. No release links to the old
`statics/`: new releases use their embedded assets; the original files remain
available for the maintainer's original-binary rollback.

1. Inspect the existing configuration, storage policies, proxy, and processes.
   Take the agreed private backup and rehearse against isolated copies first.
2. Run `install ... --stage-only` with a locally downloaded release and trusted
   hash. This validates and stages a release without changing `current`, stopping
   processes, or requiring a unit. A stage-only result is not a health check.
3. Drain incoming traffic. The maintainer must verify and gracefully stop the
   old tmux/manual process and any preview process under this data root. The
   manager never kills or adopts an unmanaged process.
4. Run `install ...` again. With no installed unit it creates `current` and the
   data links, but does not start a process. Legacy executable/static files and
   all existing file permissions remain untouched.
5. Run `install-service`, then `status`. Check the real share/download workflow
   and restore proxy traffic only after the health and functional checks pass.

The generated unit uses SIGTERM, a 300-second stop timeout without forced
SIGKILL, restart on failure, journald, and boot enablement. It runs as root to
retain compatibility with an existing root-owned installation; it does not
create users or recursively change ownership. This is deliberately not a
container/user-namespace service or a privilege migration. Since custom.4 the
unit is sandboxed instead: no capabilities, `NoNewPrivileges`, `ProtectSystem=strict`,
`ProtectHome=read-only`, `PrivateTmp`/`PrivateDevices`, and `ReadWritePaths` limited
to the data root, so a file-write bug cannot reach `~/.ssh`, cron, systemd units or
binaries outside the data root. An existing unit must exactly match the manager's
unit for this root (or the pre-custom.4 unsandboxed unit, which `install-service`
upgrades and restarts, restoring it if the restart fails), with no drop-ins,
before it can be used. A foreign or modified unit is refused rather than
overwritten. A manager older than custom.4 refuses the sandboxed unit, so after a
rollback keep using `releases/3.8.3-custom.4/deploy/cloudreve-manager.py`.

## Updates, Failure Recovery, and Retention

For later updates run `install` with a new unique version. Restaging the exact
same archive is idempotent; reusing a version for different bytes is refused.
The manager verifies target release hashes again before activation or rollback.
The current directory and its data bindings are checked separately from its
sealed contents, so a damaged current executable can be replaced by a verified
previous release. This does not bypass target hashes, safe data bindings, or
service/process ownership checks.
It checks the required live `db_version_3.8.1=installed` setting and rejects newer
version markers. It checks `/proc` PID, executable, cwd, cgroup, listening-socket
ownership, and open data writers. Nested preview instances are conservatively
treated as unmanaged and must be stopped separately.

After stopping the managed unit it checkpoints SQLite, validates data again,
backs up operational files, atomically changes the relative `current` link, and
starts the installed unit. Even a previously inactive installed unit is started
by an explicit activation. Health requires HTTP 200 and JSON
`{"code":0,"data":"3.8.3"}` at `/api/v3/site/ping` on the configured listener.
On failure, it stops the failed release, preserves committed SQLite changes,
restores the previous release link, and restarts the previously active unit.
It never replaces the live database with a stale database backup. First service
installation failure leaves data and the selected release in place and removes
the newly created unit; original-binary cutover reversal remains a maintainer
operation. Failed stop/recovery is reported and must be investigated locally.

Operational copies live in `backups/operations-*/`: configuration, the previous
runtime binary, the unit if present, and an operation record. After successful
verification, retain the latest **three resolved copies per source**. Unresolved
operations and copies explicitly referenced by root README/AGENTS/REQUIREMENTS
are retained. An initial activation without a unit remains unresolved because
it has not passed a runtime health check. Release directories are never pruned.
Data export archives and `sqlite-recovery-*` copies are **never automatically
deleted**. Only the manager's own failed partial output/staging files are cleaned.

## Consistent Export and Restore

`export` checks for unmanaged instances/writers, drains/stops the selected
managed service, and rechecks that it is stopped. It then recovers/checkpoints
SQLite before copying data. Root-local WAL is truncated normally. A release-local
WAL or journal is recovered against the same database inode using a temporary
hard-link alias. The restoring symlink is allocated before changing the alias;
cleanup restores it with an atomic rename, without allocating another symlink
on a potentially full disk. Recovery
copies are kept under `backups/sqlite-recovery-*`. Conflicting nonempty logs at
both root and release locations are refused; neither log is discarded. A busy
checkpoint also fails. There is no schema migration or old-database restoration.

A SQLite backup API snapshot is written into the archive, so committed WAL
changes are included. It is consistent with the stopped filesystem. File hashes,
metadata, inventory and SQLite integrity are verified; changed files cause the
attempt to fail. The service restart is in `finally`, including write failures,
Ctrl-C and handled SIGTERM. SIGKILL, power loss and concurrent manual changes
cannot be recovered by Python `finally`; inspect the unit on the next login.
The advisory lock serializes this manager, not arbitrary tools. Keep other
administrative writers and backup jobs away from the data root during migration.

Exports contain `conf.ini`, SQLite, all uploads (including sidecars), avatar if
present, temp/cache, the current release and provenance, original executable and
statics if present, and root operation/docs files if present. They exclude older
releases, previous backup directories, system unit installations, proxy configs,
and TLS private keys. The archive and `.sha256` sidecar are mode `0600`. Archive
contents include secrets and account data; transfer/store them privately.
No configuration values are printed. `SessionSecret` and `HashIDSalt` are
preserved by retaining the exact configuration bytes. SQLite file bytes can
change due to checkpointing/snapshotting while the logical records remain intact.

Restore requires an absent or completely empty destination and an existing
parent directory. It verifies the outer hash, every archive member and content
hash, provenance, required DB setting, storage paths and `PRAGMA integrity_check`
in a private staging directory before publishing anything. It reconstructs
`current` and data links; no archived link is trusted. Source files/archive are
retained. Numeric ownership and ordinary permission bits are retained on Linux;
ACLs, xattrs, hard-link identity and filesystem-specific metadata are not stored.
`--verify-only` performs validation without publishing or installing a service.
Actual restore never installs a unit automatically.

On the destination machine, use the restored manager and a unique unit name:

```sh
python3 "/srv/new cloudreve/current/deploy/cloudreve-manager.py" --root "/srv/new cloudreve" --service cloudreve-custom install-service
python3 "/srv/new cloudreve/current/deploy/cloudreve-manager.py" --root "/srv/new cloudreve" --service cloudreve-custom status
journalctl -u cloudreve-custom.service --since "10 minutes ago"
```

Keep the source machine intact until acceptance. Avoid running two writable
copies behind the same hostname. The included executable requires the same
architecture as the destination; cross-architecture migration needs a separately
verified matching release and a deliberate adaptation, and is refused here.

## Storage and Network Boundaries

Supported data layout: master mode, local SQLite (`UNSET`, `sqlite`, `sqlite3`),
relative `cloudreve.db`, local policies rooted in `uploads/` (default
`uploads/{uid}/{path}`), relative file `source_name`, default `temp` and `avatar`.
Canonical table prefixes are supported. Historical file sources are checked too;
completed live uploads must exist. Missing upload-session placeholders are
allowed, and their existing temp data is included.

External DBs, Redis, remote/object-storage policies, remote/aria2 nodes, absolute
or other custom storage roots, data symlinks, TLS/Unix backend listeners and
unrecognized schema layouts are refused. They require an explicit migration
plan for external state. Optional external thumbnail tools must already be
available on the destination; their binaries are not exported. The JSON backup
manifest is limited to 32 MiB; an oversized export fails rather than producing
an archive that this tool cannot verify. Plan disk space for the archive and
SQLite snapshot on export, and the complete extracted data on restore.

`nginx.conf.example` is an HTTP reverse-proxy template for port 5212, with Range,
large transfers and WebSocket forwarding. Substitute your own hostname and
validate the surrounding nginx configuration before use. It does not install
nginx or modify DNS/firewalls/certificates. For Internet access, configure HTTPS
using your certificate automation and protect the backend port. Prefer binding
the backend to loopback when the proxy is on the same host, through a separately
reviewed configuration change.

Keeping the hostname preserves share URLs; repoint its A/AAAA records after
verification, account for DNS caching, and install/renew the certificate on the
new host before traffic arrives. An obsolete AAAA record can still send clients
to the old host. IP-only links retain the old IP and cannot be preserved by DNS.
Changing hostname/scheme may require explicit site URL, OAuth/callback and
WebAuthn configuration changes; this manager does not rewrite them or reset
credentials. Recreate required firewall/proxy settings and verify actual public
URLs, TLS, upload/download, and authentication flows before retiring the source.

## Tests and Remaining Host Verification

```sh
python3 -m unittest discover -s deploy/tests -v
python3 deploy/cloudreve-manager.py --help
```

Tests use real SQLite databases, tar archives, hashes, paths, backup retention,
HTTP probes, and private temporary roots. Lifecycle tests replace external
service control/health; two focused recovery tests inject ENOSPC at symlink
allocation while keeping archive/SQLite/filesystem operations real. Linux tests
additionally exercise actual
symlinks, release-local WAL, refusal of unmanaged processes, failed-start and
health rollback, export failure restart, restored links and generated unit syntax
when `systemd-analyze` is available. Windows skips Linux lifecycle cases.
The suite never modifies a real unit or production root. A real isolated Linux
service rehearsal and host-specific nginx/TLS checks remain necessary before
production cutover. Regular hardlinked uploads are archived as independent
regular files and verified through restore; symlink sources and link members
in supplied archives remain forbidden.
