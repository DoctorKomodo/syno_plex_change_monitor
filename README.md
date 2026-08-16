# media-scan-monitor

Watch local media directories and fire **targeted scan/refresh events** at your media
servers when files are added, moved, or deleted. A single [inotify](https://man7.org/linux/man-pages/man7/inotify.7.html)
watcher fans out notifications to any number of configured **servers** — Plex, Emby,
Jellyfin, Audiobookshelf, or a generic webhook — replacing the unreliable filesystem
monitoring built into those servers.

It is a **Python application, shipped as a Docker image**, configured entirely from a
**password-protected web UI** (no hand-edited config files for normal use), with state in
**SQLite**.

> **Image:** `ghcr.io/doctorkomodo/media-scan-monitor`
> (published to GHCR; `latest` tracks the `main` branch, and semver tags are published on
> `v*` releases).

## Why?

Media servers detect new files using filesystem notifications (inotify). Those notifications
**do not propagate over network filesystems** like NFS or SMB — so when media is added to a
NAS from another machine over the network, the server never sees the change, and new media
doesn't appear until the next scheduled full scan.

Running the watcher **directly on the machine that holds the files** (where local inotify
events always fire), then calling each server's scan/refresh API for only the folder that
changed, is the reliable fix. This project does that, and fans a single watch out to multiple
servers at once.

## How it works

```
inotify watcher ──> router ──> per-server debounce ──> dispatcher ──> server adapter(s)
   (one watch          │             │                                  (Plex, Emby,
    per directory,      │             │                                   Jellyfin,
    recursive)          │             └─ collapse a burst (e.g. copying    Audiobookshelf,
                        │                a whole season) into one trigger  or webhook)
                        └─ fan out to every (server, folder) whose path
                           prefixes the change, whose extensions match, and
                           whose server is subscribed to the event's type
```

- **Domain model:** `Server (1) ──< Folder (N) ──< FileType (N)`. Each folder declares the
  host path to watch, the backend library/section id, and which file extensions to monitor.
- **Routing:** the watch set is the deduplicated union of all enabled folder paths. On an
  event, every `(server, folder)` whose path is a segment-prefix of the changed file, whose
  extensions match, *and* whose server is subscribed to the event's type (`created`/
  `moved_to`/`deleted`/`moved_from`, default: all four) becomes a subscriber; the event fans
  out to each.
- **Per-server debounce:** `trailing` collapses a burst per `(server, scan target)` into one
  trigger after the folder settles (media-server default); `off` delivers every event (good
  for a generic webhook).
- **Scan targeting:** Plex does native folder-targeted scans (`?path=`). The other adapters
  currently trigger a library refresh; per-folder targeting for them is a planned enhancement.
- **inotify watch limit:** per-directory watches consume the kernel
  `fs.inotify.max_user_watches` budget. A startup gate measures what the config needs versus
  the current limit and blocks (with a clear remediation message in the dashboard) rather than
  silently under-watching.
- **Live reconfiguration:** UI writes commit to SQLite and call the engine to rebuild — no
  restart needed.

### A note on paths

A targeted scan sends the server a path, and **that path must match how the server itself
sees the media**, not how the host sees it. If Plex runs in its own container with the
library mounted at `/data/media/tvseries`, the monitor must also refer to the media as
`/data/media/tvseries`. When running in Docker you bind-mount the source to those same paths
inside the monitor container, so everything lines up.

## Quickstart

### 1. Prepare the config directory

```bash
mkdir -p ./config
chown -R 1000:1000 ./config
```

**The container runs as a non-root user (UID 1000).** The `./config` bind-mount must be owned
by that UID or the app will fail at startup with a permission error when it tries to create
`app.db` and `secret.key`. On Synology this is the most common first-run failure — chown
the directory before starting.

### 2. Get and edit the compose file

The repo ships a ready-to-use `docker-compose.yml` (don't hand-write one — that would drift from
the file the project maintains). Copy it from the repo (clone, or download
`docker-compose.yml` from the `main` branch) and edit two things before starting:

- **Media bind-mount** — replace the placeholder `/path/to/media:/data/media:ro` with your own
  local media directory. ⚠️ **Local storage only — inotify does not work over NFS/SMB.** The
  container-side path (`/data/media/...`) must match what your media server (Plex, etc.) sees.
- **First-run password (optional)** — by default the app generates a strong random admin
  password on first boot; you do not need to create anything. To preset your own instead,
  uncomment the `secrets:` blocks in `docker-compose.yml` and create `./msm_password.txt`
  (`echo 'your-strong-password' > ./msm_password.txt && chmod 600 ./msm_password.txt`).

To raise the kernel inotify watch limit, the file includes an opt-in, privileged `init-watches`
profile — see [inotify watch limit](#inotify-watch-limit) below. It is **off** unless you run
`docker compose --profile init-watches up`.

### 3. Start the stack

```bash
docker compose up -d
```

### 4. Open the UI and log in

Navigate to `http://<your-host>:12000` and log in.

#### First login (auto-generated password)

If you did not preset a password, the app generated one on first boot and wrote it to
`/config/initial_password.txt` (owner-readable only). Retrieve it:

```bash
docker exec media-scan-monitor cat /config/initial_password.txt
# or, via the bind mount:  cat ./config/initial_password.txt
```

Log in with that password at `http://<host>:12000`. You will be **required to change it**
before you can use the app; once you do, the file is deleted automatically.

### 5. Add a server and a folder

In the web UI:
1. Go to **Servers → Add a server**. Pick the type (Plex, Emby, Jellyfin,
   Audiobookshelf, or Webhook), fill in the base URL and API token, and save.

   For a **Webhook** server you can also choose a **Payload preset**. `Custom` (the default) sends
   your own Jinja2 body template; `Sonarr / Radarr` sends a minimal payload
   (`eventType` + `instanceName` + `file_path`) compatible with apps that ingest Sonarr/Radarr
   webhooks (e.g. subtitle-pruner). The Test button sends that preset's payload with
   `eventType: "Test"` so the receiver can recognise it as a probe.

   Every server also has an **Event types** control: checkboxes for Created / Moved in /
   Deleted / Moved out, deciding which filesystem event types trigger a scan for that server.
   All four are checked by default. A generic webhook that only cares about new files, for
   example, can uncheck Deleted and Moved out.

2. On the server detail page, add a **Folder**: the path inside the container
   (e.g. `/data/media/tv`), the library/section ID that server uses for it,
   and the file extensions to watch (e.g. `mkv, mp4, avi`).

The engine picks up the new config immediately — no restart.

## Configuration

**Everything is in the web UI, behind one password.** There are no hand-edited config files
for normal operation. The container reads only deployment settings from the environment (paths,
host/port, timezone, and the first-run password — see the table below); all *application* data
(servers, folders, file types, debounce policy, inotify gate) is managed through the UI and
stored in `app.db`.

### Environment variable reference

| Variable | Default | Description |
|---|---|---|
| `MSM_HOST` | `0.0.0.0` | Listen address for the web UI. |
| `MSM_PORT` | `8080` | Listen port for the web UI *inside the container*. The shipped compose publishes it on host port `12000` (`12000:8080`); change the host side there, not this. |
| `MSM_DB_PATH` | `/config/app.db` | SQLite database path. |
| `MSM_SECRET_KEY_FILE` | `/config/secret.key` | Path to the Fernet key file used to encrypt server secrets at rest (auto-created if absent). Used only when `MSM_SECRET_KEY` is unset. |
| `MSM_SECRET_KEY` | — | Fernet key provided inline. **Takes precedence over the key file** (`MSM_SECRET_KEY` > file > auto-generate). If set, it must match the key that originally encrypted `app.db` or stored secrets won't decrypt. |
| `MSM_PASSWORD_FILE` | — | Path to a file containing the first-run password. File contents are whitespace-stripped. Takes precedence over `MSM_PASSWORD`. Never overwrites a password already set in the UI. If either `MSM_PASSWORD_FILE` or `MSM_PASSWORD` is set, no password is generated and no forced change occurs. |
| `MSM_PASSWORD` | — | First-run password provided inline. Only used if `MSM_PASSWORD_FILE` is not set. Never overwrites a password already set in the UI. If either `MSM_PASSWORD_FILE` or `MSM_PASSWORD` is set, no password is generated and no forced change occurs. |
| `MSM_INITIAL_PASSWORD_FILE` | `/config/initial_password.txt` | Where the auto-generated first-run password is written (mode 0600). Defaults to `initial_password.txt` in the directory of `MSM_DB_PATH` (i.e. `/config/initial_password.txt` with the default DB path). |
| `TZ` | (system) | Container timezone, used for log timestamps. Set to your local zone (e.g. `Europe/London`, `America/New_York`). |

### Liveness & readiness

`GET http://<host>:12000/health` returns `{"status":"ok"}` when the app is running. This
endpoint is **unauthenticated** and is what Docker's built-in `HEALTHCHECK` uses.

`GET http://<host>:12000/ready` is an **authenticated** readiness probe for orchestration: it
returns `200 {"status":"ready"}` only when the database is reachable **and** the engine is
running, and `503` otherwise.

## Volumes

### `/config` — persistent state

The `/config` directory holds two files the app creates at startup:

- `app.db` — the SQLite database: all servers, folders, file types, and settings.
- `secret.key` — the Fernet encryption key used to encrypt server API tokens at rest.

**Both files must persist across container restarts.** Use a bind-mount (`./config:/config`)
or a named volume. A named volume inherits correct ownership automatically from the image; a
host bind-mount requires `chown -R 1000:1000 ./config` (see [/config ownership](#config-ownership)).

### `/config` ownership

The container runs as a non-root user (`app`, **UID 1000**, GID 1000). If you use a host
bind-mount for `/config`, the directory must be owned by UID 1000 or the app will fail at
startup with a permission error:

```bash
mkdir -p ./config
chown -R 1000:1000 ./config
```

On **Synology** NAS this is particularly important: bind-mounts from DSM default to root
ownership. Create the `config` directory as root, then chown it before starting the container.

### Reading your media (host UID / `MSM_UID` / `MSM_GID`)

The watcher can only watch directories the container user can **read and traverse**. Because the
image runs as UID 1000 but your media is usually owned by a different host user, a default
container often can't see it — the symptom is **Dashboard → "Directories watched: 0"** for an
enabled folder on a correct path.

Override the runtime user in `docker-compose.yml` (already wired via `user: "${MSM_UID:-1000}:${MSM_GID:-1000}"`)
to the owner of your media. Find the owner on the host and start with those values:

```bash
stat -c '%u:%g' /volume2/data/media          # e.g. 1026:100
MSM_UID=1026 MSM_GID=100 docker compose up -d
```

On **Synology Container Manager** (no shell to export variables), either add a `.env` file next
to `docker-compose.yml` with `MSM_UID=1026` / `MSM_GID=100`, or hardcode the line directly:
`user: "1026:100"`.

**`./config` must also be writable by that UID** — chown it to the same values
(`chown -R "$MSM_UID:$MSM_GID" ./config`), or startup fails creating `app.db` / `secret.key`.

If instead the media is **group-readable** but owned by another user, keep UID 1000 (so `/config`
ownership is unchanged) and add the media's group as a supplementary group via the commented
`group_add:` block in the compose file (the Synology `users` group is GID 100).

### `secret.key` — back it up

Server API tokens (Plex tokens, Emby API keys, etc.) are encrypted with Fernet using the key
stored in `/config/secret.key`. If you lose this file or regenerate the key **all stored
secrets become undecryptable** — you will need to re-enter every server token in the UI.

Back up `secret.key` alongside `app.db`, or include `./config` in your regular backup
rotation.

### Media bind-mounts — local storage only

```
⚠ inotify does not work over network filesystems (NFS, SMB/CIFS, AFP).
  Media sources must be bind-mounted from LOCAL storage on the machine running the container.
```

The watcher must see filesystem events directly — events do not propagate over a network
mount. On a Synology NAS this means bind-mounting from a local volume (e.g. `/volume1/media`),
not from a network share, even if the same content is accessible both ways.

The container path you mount the media to must match the path your media servers use for the
same content. See [A note on paths](#a-note-on-paths) above.

## inotify watch limit

The Linux kernel limits the number of inotify directory watches per user. The app derives
`needed` from its configured watch directories and gates on `current ≥ needed` — if the
kernel limit is too low, the watcher will not start and the dashboard surfaces the remediation.

### Check the current limit and what the app needs

Open the web UI → **Dashboard**. The status panel shows:
- **Current kernel limit** (`fs.inotify.max_user_watches`)
- **Needed** (derived from your configured folders)
- **Recommended** (needed + headroom, the value to raise the limit to)

### Option A — host-level sysctl (recommended, default)

Raise the limit directly on the host. This is the standard approach and requires no
privileged container.

**Linux (sysctl.d drop-in, survives reboots):**

```bash
echo 'fs.inotify.max_user_watches = 131072' | sudo tee /etc/sysctl.d/99-msm-inotify.conf
sudo sysctl --system
```

**Synology (root boot task):**

In DSM → Task Scheduler → Create → Triggered task → Boot-up, as root:

```sh
echo 131072 > /proc/sys/fs/inotify/max_user_watches
```

After raising the limit, click **Re-check watch limit** on the Dashboard. The engine will
resume watching automatically.

### Option B — opt-in init sidecar (privileged, host-global)

The compose file ships an opt-in `init-watches` service that sets the limit once and exits.
Enable it with:

```bash
docker compose --profile init-watches up
```

**Warning:** this sidecar runs as **privileged** and writes a **host-wide** kernel value that
affects every process on the host, not just this container. Only use it if you are comfortable
running privileged containers and understand the implications. Option A (host sysctl.d) is
preferred.

## Troubleshooting

### "No events ever seen"

The app is running and healthy but no scan events appear on the Dashboard's Signal panel:

1. **Check that the media bind-mount is local storage.** inotify events do not fire on
   network-mounted filesystems (NFS, SMB). The bind-mount source must be a local path on the
   machine running the container. See [Media bind-mounts — local storage only](#media-bind-mounts--local-storage-only).

2. **Check the inotify watch limit.** Open Dashboard → the watch-limit panel shows whether the
   limit is sufficient. If `current < needed`, the watcher is not running — raise the limit
   (see [inotify watch limit](#inotify-watch-limit)) and click Re-check.

3. **Check `/config` is writable.** If the startup log contains a permission error, the app
   could not create `app.db` or `secret.key`. Fix: `chown -R 1000:1000 ./config` on the host.

4. **Check the folder path and extensions.** Open the Events page — it streams every raw
   filesystem event the watcher sees, before routing. If a change never shows up there at all,
   the watcher isn't seeing the file (bind-mount/permission problem, see above). If it shows up
   but dimmed as `[no match]`, the event reached the app but matched no server's watched folder,
   extension list, or subscribed event type — on the server detail page, verify the folder path
   matches the container's bind-mount path (e.g. `/data/media/tv`, not the host path
   `/volume1/media/tv`), and that the file extensions list includes the type of files being
   added.

### Dashboard shows "Directories watched: 0"

The folder is enabled but the count stays at 0. The watcher counts a directory only if the
container user can read it, so this almost always means a **path or permission** problem:

1. **The path doesn't exist in the container.** The folder path must resolve under a bind-mount.
   Verify inside the container: `docker exec media-scan-monitor ls -ld /data/media/movies`. A
   common mismatch is configuring `/data/media/movies` when the media actually sits directly in
   `/data/media`, or a differently-cased subfolder name.
2. **The container user can't read the media.** If the path exists but is owned by another host
   user, set `MSM_UID`/`MSM_GID` (or `group_add`) to the media's owner — see
   [Reading your media](#reading-your-media-host-uid--msm_uid--msm_gid). Confirm with
   `docker exec media-scan-monitor id` and the `ls -ld` ownership above.
3. **The server or the folder is disabled.** Only enabled servers and enabled folders are
   watched. If the watch-limit panel instead says "No watches configured yet", nothing is
   enabled — tick **Enabled** on the server and **on** for the folder.

### Container exits at startup

Check `docker compose logs media-scan-monitor`. The container exits on startup only if:

- **Permission error on `/config`** — `chown -R 1000:1000 ./config` (see above).

### Can't log in / stored secrets won't decrypt

- **Auto-generated password not working** — retrieve it from `/config/initial_password.txt`
  (see [First login](#first-login-auto-generated-password)). The file is deleted after you
  change the password; if it is missing you have already changed it once.
- **Forgot the password** — on the login page click **Forgot password?** → **Reset password**.
  This regenerates the admin password exactly like first run: it is written to
  `/config/initial_password.txt` (mode 0600) and you are forced to change it on the next login.
  Retrieve it on the host with `docker exec media-scan-monitor cat /config/initial_password.txt`.
  The reset is rate-limited (3 per hour per client) and never reveals the password in the
  browser — recovery still requires host access to read the file.
- **Preset password (`MSM_PASSWORD_FILE`) not accepted** — verify the path exists and is readable
  inside the container. Or remove `MSM_PASSWORD_FILE` and let auto-generation handle first login.
- **`MSM_SECRET_KEY` / `MSM_SECRET_KEY_FILE` mismatch** — if you provide an inline key that
  does not match the key in `/config/secret.key`, decryption fails when a stored secret is used
  (e.g. testing a server). Either keep the key consistent or clear `MSM_SECRET_KEY` and let the
  app use the file.

## Development

See [`CLAUDE.md`](CLAUDE.md) for the project rules, architecture detail, module map,
development commands (`ruff`, `mypy`, `pytest`), lint/style conventions, and the staged
rollout phases. Design specs and implementation plans live under [`docs/`](docs/).

```bash
pip install -e ".[dev]"   # install app + dev tooling

ruff check .              # lint
ruff format .             # format
mypy mediascanmonitor     # type check (strict)
pytest                    # tests
```

CI runs all four on every push/PR and must stay green.

## Legacy bash script

This project began as a Bash script (`plex_monitor.sh` and its `plex_monitor.conf` / Alpine
`Dockerfile`). The Python application has fully replaced it, and the Bash files were removed when
the rewrite became the shipping version. If you need them, they (and the previous Bash-focused
README) live in the git history before that cutover.
