#!/bin/bash

### Wait for inotify limit to be configured
REQUIRED_LIMIT=131072
MAX_WAIT=180
WAITED=0

while (( $(cat /proc/sys/fs/inotify/max_user_watches) < REQUIRED_LIMIT )); do
    if (( WAITED >= MAX_WAIT )); then
        echo "[$(date)] ERROR: inotify limit not set after ${MAX_WAIT}s, exiting" >> "$LOGFILE"
        exit 1
    fi
    sleep 10
    ((WAITED += 10))
done

### Configuration
CONFIG_FILE="${CONFIG_FILE:-/volume2/scripts/plex_monitor.conf}"
if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "[$(date)] ERROR: Config file not found: $CONFIG_FILE" >&2
    exit 1
fi
source "$CONFIG_FILE"

MEDIA_EXTENSIONS="mkv|mp4|avi|ts|m4v|mov|wmv|flv|webm|srt|smi|ssa|ass|sub|idx|sup|vtt"
PLEXSERVER="https://192-168-0-8.667d51705797471ea1da1e6234ed9f7e.plex.direct:32400"
LOGFILE="/volume2/scripts/logs/plex_notify.log"
WAIT_SEC=30

# Directory to Library Section Mapping
declare -A LIBRARY_MAP
LIBRARY_MAP["/volume2/movies/"]="1"
LIBRARY_MAP["/volume2/tvseries/"]="2"

# Directories to monitor
MONITOR_DIRS=(
    "/volume2/movies/"
    "/volume2/tvseries/"
)

# Directories to ignore (Synology system folders)
IGNORE_DIRS=(
    "@eaDir"
    "#snapshot"
)

### Functions
urlencode() {
    local string="$1"
    python3 -c "import urllib.parse; print(urllib.parse.quote('''$string''', safe='/'))"
}

is_ignored_path() {
    local file_path="$1"

    for ignore_dir in "${IGNORE_DIRS[@]}"; do
        if [[ "$file_path" == *"/${ignore_dir}/"* ]]; then
            return 0  # True - should be ignored
        fi
    done

    return 1  # False - should not be ignored
}

send_plex_refresh() {
    local library_id="$1"
    local scan_path="$2"

    echo "[$(date)] === SENDING PLEX REFRESH ===" >> "$LOGFILE"
    echo "[$(date)] Library ID: $library_id" >> "$LOGFILE"
    echo "[$(date)] Scan path: $scan_path" >> "$LOGFILE"

    local encoded_path
    encoded_path=$(urlencode "$scan_path")

    local url="${PLEXSERVER}/library/sections/${library_id}/refresh?path=${encoded_path}&X-Plex-Token=${PLEX_TOKEN}"

    echo "[$(date)] Request URL: ${PLEXSERVER}/library/sections/${library_id}/refresh?path=${encoded_path}" >> "$LOGFILE"

    local curl_response
    curl_response=$(curl -s -w "HTTP_STATUS:%{http_code}" -X GET "$url" 2>&1)

    local http_status=$(echo "$curl_response" | grep -o "HTTP_STATUS:[0-9]*" | cut -d: -f2)

    echo "[$(date)] HTTP Status: $http_status" >> "$LOGFILE"

    if [[ "$http_status" == "200" ]]; then
        echo "[$(date)] ✅ Plex partial refresh successful!" >> "$LOGFILE"
    else
        echo "[$(date)] ❌ Plex refresh failed!" >> "$LOGFILE"
    fi
    echo "[$(date)] === END PLEX REFRESH ===" >> "$LOGFILE"
}

get_library_id() {
    local file_path="$1"

    for dir in "${!LIBRARY_MAP[@]}"; do
        if [[ "$file_path" == "$dir"* ]]; then
            echo "${LIBRARY_MAP[$dir]}"
            return
        fi
    done

    echo ""
}

get_library_root() {
    local file_path="$1"

    for dir in "${!LIBRARY_MAP[@]}"; do
        if [[ "$file_path" == "$dir"* ]]; then
            echo "$dir"
            return
        fi
    done

    echo ""
}

get_scan_path() {
    local file_path="$1"
    local library_root="$2"

    # Get path relative to library root
    local relative_path="${file_path#$library_root}"

    # Extract the top-level folder (show name or movie folder)
    local top_folder=$(echo "$relative_path" | cut -d'/' -f1)

    # Return the full path to that folder
    echo "${library_root}${top_folder}"
}

### Main Monitoring Logic
INCLUDE_PATTERN="\.(${MEDIA_EXTENSIONS})$"

echo "[$(date)] ========================================" >> "$LOGFILE"
echo "[$(date)] Starting Plex monitoring (targeted refresh)" >> "$LOGFILE"
echo "[$(date)] Monitoring: ${MONITOR_DIRS[*]}" >> "$LOGFILE"
echo "[$(date)] Ignoring: ${IGNORE_DIRS[*]}" >> "$LOGFILE"
echo "[$(date)] Debounce interval: ${WAIT_SEC}s" >> "$LOGFILE"
echo "[$(date)] ========================================" >> "$LOGFILE"

# Track last refresh per scan path for debouncing
declare -A LAST_REFRESH_TIME

inotifywait -m -r -e create -e moved_to -e delete -e move \
    --format $'%e\t%w%f' "${MONITOR_DIRS[@]}" | \
while IFS=$'\t' read -r EVENT FULLPATH; do

    # Check if file matches our media extensions
    if [[ ! "$FULLPATH" =~ $INCLUDE_PATTERN ]]; then
        continue
    fi

    # Check if path is in an ignored directory
    if is_ignored_path "$FULLPATH"; then
        continue
    fi

    echo "[$(date)] Detected: $FULLPATH [$EVENT]" >> "$LOGFILE"

    # Get library info
    LIBRARY_ID=$(get_library_id "$FULLPATH")
    LIBRARY_ROOT=$(get_library_root "$FULLPATH")

    if [[ -z "$LIBRARY_ID" ]]; then
        echo "[$(date)] ❌ No library mapping found for: $FULLPATH" >> "$LOGFILE"
        continue
    fi

    # Determine the folder to scan (show folder or movie folder)
    SCAN_PATH=$(get_scan_path "$FULLPATH" "$LIBRARY_ROOT")

    echo "[$(date)] Library ID: $LIBRARY_ID" >> "$LOGFILE"
    echo "[$(date)] Scan path: $SCAN_PATH" >> "$LOGFILE"

    NOW=$(date +%s)
    LAST_TIME=${LAST_REFRESH_TIME[$SCAN_PATH]:-0}
    TIME_SINCE=$((NOW - LAST_TIME))

    echo "[$(date)] ${TIME_SINCE}s since last refresh of this path" >> "$LOGFILE"

    # Check if enough time has passed since last refresh for this specific path
    if (( TIME_SINCE >= WAIT_SEC )); then
        echo "[$(date)] ⏳ Waiting ${WAIT_SEC}s for additional changes..." >> "$LOGFILE"
        sleep $WAIT_SEC

        send_plex_refresh "$LIBRARY_ID" "$SCAN_PATH"
        LAST_REFRESH_TIME[$SCAN_PATH]=$(date +%s)
    else
        REMAINING=$((WAIT_SEC - TIME_SINCE))
        echo "[$(date)] ⏭️  Skipping (debounced, ${REMAINING}s remaining)" >> "$LOGFILE"
    fi

done
