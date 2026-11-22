# YaMusicBot backup operations

The independent `backup` container creates encrypted archives in the Storage Box subaccount root. Example: `2026-11-22/12:00.tar.zst.age`. Files are compressed with Zstandard and encrypted with age before SFTP upload. A temporary `.partial` file is read back and checked with SHA-256 before atomic publication. No project or VPS prefix is added.

`backup.timer` runs at every full hour in `Europe/Moscow`, with a catch-up run after downtime. After a verified upload, the service deletes recognized timestamped archives older than 30 days and removes empty date directories. Other files, symlinks and directories are preserved. There are no daily, weekly or monthly tiers.

## Contents

- MongoDB logical dump, including database users and roles, created with the installed `mongodump`.
- Project source, production `settings.json`, `.env` when present, Compose files, Lavalink configuration and tokens, translations and project extensions.
- `last-session.json` and `.env` from the running bot when present.
- Local WireGuard configuration and tunnel health script and units.
- Container image IDs and mounts, application database collection hashes, and a SHA-256 file manifest.

Raw MongoDB files, logs, caches, Git metadata, local test artifacts, deployment logs, `PENDING_PRODUCTION.md` and SSH private keys are excluded. In-memory playback state that has not been persisted cannot be recovered. Remote tunnel servers require their own backup. Container image IDs identify versions; image layers are not stored in each archive.

MongoDB runs as a standalone server. The host coordinator briefly locks database writes while copying the flushed database files; reads and the bot process continue. Writes wait until unlock. The file copy has a 60-second timeout. After unlock, a separate MongoDB container starts on the copy with no network, no published ports and resource limits. The logical dump and collection hashes are created from that isolated copy. The dump has a 15-minute timeout. Python cleanup and systemd `ExecStopPost` release the lock on errors, stop an abandoned archive container and clean temporary plaintext files. A pre-existing database lock causes backup failure without unlocking that lock. The host coordinator uses a nonblocking file lock; the archive container has a fixed name to prevent overlap.

## Installation

Deploy only the approved code and image. The application containers do not require restart. Build with `docker compose -f backup/compose.yml build backup` and set `BACKUP_IMAGE` to the approved image tag in `/etc/yamusicbot-backup/service.env`.

The root-owned directory `/etc/yamusicbot-backup` must have mode `0700`, with mode `0600` files:

- `storage.json`: `{"host":"STORAGE_HOST","username":"SUBACCOUNT"}`. Port is fixed to 23; the account's current directory is the backup root.
- `ssh_key`: the dedicated VPS backup ED25519 key, never included in an image or archive.
- `known_hosts`: independently verified Storage Box host keys. Unknown or changed keys are rejected.
- `recipients.txt`: age public recipient.
- `service.env`: `BACKUP_IMAGE=yamusicbot-backup:APPROVED_TAG` and any required path overrides.

Keep the age private identity at `/etc/yamusicbot-backup-recovery/age-identity.txt` with mode `0600` and its directory mode `0700`. Keep a protected recovery copy outside the VPS. The regular backup container receives only the public recipient and SSH credentials. Only `restore.sh` mounts the recovery identity.

Install the four `.service` and `.timer` files into `/etc/systemd/system`, run `systemctl daemon-reload`, then enable `backup.timer` and `backup-health.timer` with `systemctl enable --now backup.timer backup-health.timer`. The supplied units use `/root/project/YaMusicBot/backup`. Adjust paths when deploying elsewhere. The host requires Docker Compose, systemd and Python 3.12 or newer.

The worker container has no Docker socket or database credentials, a read-only filesystem, dropped Linux capabilities and CPU/memory limits. The root host coordinator uses Docker to create the consistent MongoDB dump and read container-local recovery files. Secrets and source snapshots stay in root-only directories. State defaults to `/var/lib/yamusicbot-backup`; environment overrides are `BACKUP_STATE`, `BACKUP_PROJECT`, `BACKUP_SECRETS`, `BACKUP_RECOVERY`, `BACKUP_DB_CONTAINER` and `BACKUP_APP_CONTAINER`.

## Manual operations

Run commands from the deployed repository. Source the same `service.env` when invoking the scripts directly, or use `systemctl start backup.service` for a manual backup with the service's settings and cleanup.

```sh
systemctl start backup.service
journalctl -u backup.service -n 100 --no-pager
systemctl list-timers backup.timer backup-health.timer
sh backup/healthcheck.sh
cat /var/lib/yamusicbot-backup/status.json
```

`status.json` records last successful completion, archive path, SHA-256, encrypted size, duration and available remote bytes when the server exposes filesystem statistics. Transfer failures are retried three times. A failed run fails `backup.service`. `backup-health.timer` checks every 15 minutes and fails its service if the last successful backup is older than two hours or missing. Monitor these failed units or journal events using the host's existing monitoring. No Telegram or Discord notification is sent.

```sh
set -a
. /etc/yamusicbot-backup/service.env
set +a
sh backup/restore.sh list
sh backup/restore.sh restore '2026-11-22/12:00.tar.zst.age' /state/restores/verification
```

Restore downloads from Hetzner, authenticates age encryption, rejects unsafe tar members, checks the complete file manifest and extracts only into a new directory beneath `/state/restores`. The host destination is `/var/lib/yamusicbot-backup/restores/verification`. An existing destination is rejected. Restore does not start the Discord bot or write to production.

For the current dataset, allow 3 GiB of memory for the isolated test MongoDB and restore process. To test MongoDB recovery, start an isolated `mongo:8` container with `--network=none`, no published ports and a fresh temporary volume, mount the restored directory read-only, and run `mongorestore --archive=/restore/mongo.archive --stopOnError --batchSize=25 --numInsertionWorkersPerCollection=1`. Compare each application database's `dbHash` collection map with `mongo-checks.json`, then stop and remove the test container and volume. Production restoration and replacement of production files require a separate explicit action.

An additional run within the same Moscow hour atomically replaces that hour's archive. Downloaded archives contain credentials and must stay in protected storage. If the VPS is lost, recover the age identity from its external protected copy, rebuild the worker image and provision a new SSH key for the Storage Box account.
