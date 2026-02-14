# Synology Plex Change Monitor

A lightweight bash script that monitors media directories on a Synology NAS and triggers **targeted partial scans** in Plex when files are added, moved, or deleted. It replaces Plex's built-in filesystem monitoring, which is often unreliable over networks.

## Why?

Plex's built-in "Update my library automatically" option relies on filesystem notifications (inotify) to detect changes. These notifications do not propagate over network filesystems like NFS or SMB — so when media is added to your Synology NAS from another machine over the network, Plex never sees the change. New media won't appear until the next scheduled full library scan.

This script solves that by running `inotifywait` directly on the NAS itself, where local filesystem events are always available, and calling Plex's partial scan API to refresh only the specific show or movie folder that changed — not the entire library.

## How It Works

1. `inotifywait` recursively watches your media directories for file create, move, and delete events.
2. Events are filtered to media file extensions only (video and subtitle formats).
3. Synology system directories (`@eaDir`, `#snapshot`) are ignored.
4. The script determines which Plex library and top-level folder (e.g., the show or movie folder) the change belongs to.
5. A **debounce** mechanism ensures that rapid changes (like copying an entire season) trigger only a single scan after a configurable wait period (default: 30 seconds).
6. A targeted Plex API call refreshes just that folder.

## Requirements

- Synology NAS running DSM
- `bash` 4+ (for associative arrays)
- `inotifywait` — install via the SynoCommunity `inotify-tools` package or compile from source
- `curl`
- `python3` (used for URL encoding)
- A Plex Media Server running on the NAS (or accessible on the network)

## Setup

### 1. Place the files

Copy the script and config to your NAS:

```
/volume2/scripts/
├── plex_monitor.sh        # Main script
├── plex_monitor.conf      # Plex token (keep this secret)
└── logs/                  # Create this directory
    └── plex_notify.log    # Created automatically at runtime
```

### 2. Configure the script

Edit `plex_monitor.sh` and set:

- **`PLEXSERVER`** — Your Plex server URL (the `.plex.direct` address from Plex settings)
- **`MONITOR_DIRS`** — The directories to watch
- **`LIBRARY_MAP`** — Map each monitored directory to its Plex library section ID

To find your library section IDs, visit:
```
https://your-plex-server:32400/library/sections?X-Plex-Token=YOUR_TOKEN
```

### 3. Configure the token

Edit `plex_monitor.conf` and add your Plex token:

```bash
PLEX_TOKEN="your_token_here"
```

Then restrict permissions:

```bash
chmod 600 /volume2/scripts/plex_monitor.conf
```

### 4. Increase the inotify watch limit

Large media libraries require a higher inotify watch limit than the default. Create a **Triggered Task** in DSM Task Scheduler:

| Setting | Value |
|---------|-------|
| Event | Boot-up |
| User | root |
| Command | `sh -c '(sleep 90 && echo 131072 > /proc/sys/fs/inotify/max_user_watches)&'` |

The 90-second delay ensures the system is fully booted before writing to `/proc`.

### 5. Start the monitor on boot

Create a second **Triggered Task** in DSM Task Scheduler:

| Setting | Value |
|---------|-------|
| Event | Boot-up |
| User | *your regular user* (not root) |
| Command | `bash /volume2/scripts/plex_monitor.sh` |

The script will wait for the inotify limit to be set before it begins monitoring.

### 6. Set up log rotation (optional)

Create `/etc/logrotate.d/plex_monitor`:

```
/volume2/scripts/logs/plex_notify.log {
    weekly
    rotate 4
    compress
    missingok
    notifempty
    size 10M
}
```

## Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `PLEXSERVER` | — | Full Plex server URL including port |
| `PLEX_TOKEN` | — | Plex authentication token (in `plex_monitor.conf`) |
| `MEDIA_EXTENSIONS` | `mkv\|mp4\|avi\|...` | Pipe-separated list of file extensions to watch |
| `MONITOR_DIRS` | — | Array of directories to monitor |
| `LIBRARY_MAP` | — | Associative array mapping directories to Plex library section IDs |
| `IGNORE_DIRS` | `@eaDir`, `#snapshot` | Synology system directories to skip |
| `WAIT_SEC` | `30` | Debounce interval in seconds |
| `CONFIG_FILE` | `/volume2/scripts/plex_monitor.conf` | Path to the secrets file (can be overridden via environment variable) |

## Monitored Events

| Event | Trigger |
|-------|---------|
| `CREATE` | New file appears in a watched directory |
| `MOVED_TO` | File moved into a watched directory |
| `DELETE` | File removed from a watched directory |
| `MOVE` | File moved out of a watched directory |

## Supported File Types

Video: `mkv`, `mp4`, `avi`, `ts`, `m4v`, `mov`, `wmv`, `flv`, `webm`

Subtitles: `srt`, `smi`, `ssa`, `ass`, `sub`, `idx`, `sup`, `vtt`

## Troubleshooting

**Script exits immediately with inotify limit error**
The boot task that sets the inotify limit may not have run yet. The script waits up to 3 minutes. Check that the root boot task is enabled in DSM Task Scheduler.

**Changes not detected**
Verify `inotifywait` is installed and in your `PATH`. Test manually:
```bash
inotifywait -m -r /volume2/movies/
```

**Plex refresh returns non-200 status**
Check your `PLEX_TOKEN` and `PLEXSERVER` values. Verify the token works:
```bash
curl -s "https://your-server:32400/library/sections?X-Plex-Token=YOUR_TOKEN"
```

**Logs show wrong library ID**
Confirm your `LIBRARY_MAP` paths end with a trailing slash and match the `MONITOR_DIRS` entries exactly.

## License

MIT
