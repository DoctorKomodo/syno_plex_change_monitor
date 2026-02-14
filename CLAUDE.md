# Plex Monitor Script – Development Summary

## Purpose

This script monitors media directories on a Synology NAS and triggers **targeted partial scans** in Plex when media files are added, moved, or deleted. It replaces Plex's built-in filesystem monitoring which can be unreliable on NAS devices.

---

## Problems Solved During Development

| Issue | Solution |
|-------|----------|
| Original delimiter parsing failed (`IFS=' DELIMITER '` treats each character as a delimiter) | Changed to tab character `\t'` which won't appear in filenames |
| Full library scans on every file change | Implemented Plex's `?path=` parameter for targeted folder scanning |
| Multiple refreshes when adding many files at once | Added debouncing per scan path with configurable wait period |
| Synology system folders (`@eaDir`, `#snapshot`) triggering scans | Added ignore list with path matching |
| Log file growth over time | Configured logrotate for automatic rotation |
| inotify watch limit too low for large libraries | Set via boot task: `echo 131072 > /proc/sys/fs/inotify/max_user_watches` |
| Plex token exposed in main script | Moved to separate config file (`plex_monitor.conf`) with restricted permissions |

---

## Key Design Decisions

### 1. Targeted vs Full Library Refresh

The script uses Plex's partial scan API:
```
/library/sections/{id}/refresh?path={encoded_path}
```
This scans only the specific show/movie folder rather than walking the entire library.

### 2. Scan Path Level

For TV shows, the scan targets the **show folder** (e.g., `/volume2/tvseries/Shoresy`), not the season folder. This ensures show-level metadata updates while still being very fast.

### 3. Debouncing Strategy

When a file is detected:
- Check when that specific folder was last refreshed
- If ≥30 seconds ago → wait 30s (to catch more incoming files), then refresh
- If <30 seconds ago → skip (previous refresh will catch it)

This means adding a full season triggers one refresh, not one per episode.

### 4. Wait Period Before Refresh

The 30-second delay allows for:
- Large file transfers to complete
- Batch operations to finish
- Filesystem sync on NAS/RAID systems

### 5. Privilege Separation

- inotify limit setting runs as **root** (required for `/proc` access)
- Monitor script runs as **regular user** (principle of least privilege)
- Script waits for limit to be set before starting

---

## Script Structure

```
┌─────────────────────────────────────────────────────────────┐
│ Configuration                                               │
│  - Media extensions, Plex server URL, token, log path       │
│  - Library mapping (directory → Plex library ID)            │
│  - Directories to monitor and ignore                        │
├─────────────────────────────────────────────────────────────┤
│ Functions                                                   │
│  - urlencode(): URL-encode paths for API calls              │
│  - is_ignored_path(): Check against ignore list             │
│  - send_plex_refresh(): Make API call to Plex               │
│  - get_library_id(): Map path to library section            │
│  - get_library_root(): Get base directory for library       │
│  - get_scan_path(): Determine folder to scan                │
├─────────────────────────────────────────────────────────────┤
│ Main Loop                                                   │
│  - inotifywait monitors directories                         │
│  - Filter by extension and ignore list                      │
│  - Debounce by scan path                                    │
│  - Trigger targeted Plex refresh                            │
└─────────────────────────────────────────────────────────────┘
```

---

## Configuration Reference

### Main script (`plex_monitor.sh`)

```bash
# Media file extensions (video + subtitles)
MEDIA_EXTENSIONS="mkv|mp4|avi|ts|m4v|mov|wmv|flv|webm|srt|smi|ssa|ass|sub|idx|sup|vtt"

# Plex connection
PLEXSERVER="https://192-168-0-8.xxx.plex.direct:32400"

# Library mapping (path → Plex library section ID)
LIBRARY_MAP["/volume2/movies/"]="1"
LIBRARY_MAP["/volume2/tvseries/"]="2"

# Ignored directories (Synology system folders)
IGNORE_DIRS=("@eaDir" "#snapshot")

# Timing
WAIT_SEC=30  # Debounce interval
```

### Secret config (`plex_monitor.conf`)

```bash
# Protect with: chmod 600 /volume2/scripts/plex_monitor.conf
PLEX_TOKEN="your_token_here"
```

The config file path can be overridden: `CONFIG_FILE=/path/to/config ./plex_monitor.sh`

---

## Deployment

### Boot Tasks (via DSM Task Scheduler)

| Task | User | Command |
|------|------|---------|
| Set inotify limit | root | `sh -c '(sleep 90 && echo 131072 > /proc/sys/fs/inotify/max_user_watches)&'` |
| Start monitor | regular user | `bash /volume2/scripts/plex_monitor.sh` |

### Log Rotation (via `/etc/logrotate.d/plex_monitor`)

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

---

## File Locations

```
/volume2/scripts/
├── plex_monitor.sh           # Main monitoring script
├── plex_monitor.conf         # Secret configuration (chmod 600)
└── logs/
    └── plex_notify.log       # Runtime log

/etc/logrotate.d/
└── plex_monitor              # Log rotation config
```

---

## Monitored Events

The script watches for these inotify events:
- `CREATE` – New file created
- `MOVED_TO` – File moved into monitored directory
- `DELETE` – File removed
- `MOVE` – File moved (covers `MOVED_FROM`)

---

## Dependencies

- `bash` (v4+ for associative arrays)
- `inotifywait` (from inotify-tools)
- `curl` (for Plex API calls)
- `python3` (for URL encoding)
