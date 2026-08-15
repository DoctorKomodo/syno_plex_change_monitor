from mediascanmonitor.db.models import ScanMode
from mediascanmonitor.pipeline.events import FsEvent, FsEventType
from mediascanmonitor.pipeline.router import compute_scan_path, route
from tests.pipeline.factories import make_folder_route, make_runtime_config


def _event(path: str) -> FsEvent:
    return FsEvent(path=path, event_type=FsEventType.created, is_dir=False)


# --- compute_scan_path -------------------------------------------------------


def test_compute_scan_path_file_two_levels_deep() -> None:
    scan_path, top = compute_scan_path("/data/tv", "/data/tv/Shoresy/S01/ep.mkv")
    assert scan_path == "/data/tv/Shoresy"
    assert top == "Shoresy"


def test_compute_scan_path_file_one_level_deep() -> None:
    scan_path, top = compute_scan_path("/data/tv", "/data/tv/Shoresy/ep.mkv")
    assert scan_path == "/data/tv/Shoresy"
    assert top == "Shoresy"


def test_compute_scan_path_file_directly_in_root() -> None:
    # File sits directly in folder_root: top_folder is None, scan_path == folder_root.
    scan_path, top = compute_scan_path("/data/tv", "/data/tv/loose.mkv")
    assert scan_path == "/data/tv"
    assert top is None


def test_compute_scan_path_handles_root_folder_without_double_slash() -> None:
    scan_path, top = compute_scan_path("/", "/Shoresy/ep.mkv")
    assert scan_path == "/Shoresy"
    assert top == "Shoresy"


# --- route: prefix correctness ----------------------------------------------


def test_route_segment_prefix_matches_child_path() -> None:
    config = make_runtime_config([make_folder_route(path="/data/tv")])
    reqs = route(_event("/data/tv/Shoresy/ep.mkv"), config)
    assert len(reqs) == 1
    assert reqs[0].server_id == 1


def test_route_segment_prefix_rejects_sibling_with_shared_prefix() -> None:
    # Route "/data/tv" must NOT match "/data/tvshows/..." (invariant 5).
    config = make_runtime_config([make_folder_route(path="/data/tv")])
    reqs = route(_event("/data/tvshows/ep.mkv"), config)
    assert reqs == []


def test_route_matches_file_exactly_at_root_is_handled() -> None:
    config = make_runtime_config([make_folder_route(path="/data/tv")])
    reqs = route(_event("/data/tv/loose.mkv"), config)
    assert len(reqs) == 1
    assert reqs[0].scan_path == "/data/tv"
    assert reqs[0].top_folder is None


# --- route: ignore dirs ------------------------------------------------------


def test_route_skips_ignored_dirs() -> None:
    config = make_runtime_config([make_folder_route(path="/data/tv")])
    reqs = route(_event("/data/tv/@eaDir/thumb.mkv"), config)
    assert reqs == []


# --- route: fan-out to matching subscribers only ----------------------------


def test_route_fans_out_only_to_extension_matching_subscribers() -> None:
    # Two servers subscribe to the SAME folder path with DIFFERENT extension sets.
    route_mkv = make_folder_route(
        server_id=1, server_name="plex-mkv", extensions=frozenset({"mkv"})
    )
    route_srt = make_folder_route(
        server_id=2, server_name="plex-srt", extensions=frozenset({"srt"})
    )
    config = make_runtime_config([route_mkv, route_srt])

    mkv_reqs = route(_event("/data/tv/Shoresy/ep.mkv"), config)
    assert {r.server_id for r in mkv_reqs} == {1}

    srt_reqs = route(_event("/data/tv/Shoresy/ep.srt"), config)
    assert {r.server_id for r in srt_reqs} == {2}


def test_route_empty_extension_set_subscriber_matches_any_file() -> None:
    route_all = make_folder_route(server_id=3, server_name="webhook", extensions=frozenset())
    config = make_runtime_config([route_all])
    reqs = route(_event("/data/tv/Shoresy/ep.flac"), config)
    assert {r.server_id for r in reqs} == {3}


# --- route: scan_mode / scan_key --------------------------------------------


def test_route_targeted_sets_scan_path_and_scan_key() -> None:
    config = make_runtime_config([make_folder_route(scan_mode=ScanMode.targeted, library_id="2")])
    req = route(_event("/data/tv/Shoresy/ep.mkv"), config)[0]
    assert req.scan_mode is ScanMode.targeted
    assert req.scan_path == "/data/tv/Shoresy"
    assert req.scan_key == "/data/tv/Shoresy"  # invariant 2: scan_key == scan_path
    assert req.library_id == "2"
    assert req.top_folder == "Shoresy"


def test_route_library_mode_sets_null_scan_path_and_lib_scan_key() -> None:
    config = make_runtime_config([make_folder_route(scan_mode=ScanMode.library, library_id="7")])
    req = route(_event("/data/tv/Shoresy/ep.mkv"), config)[0]
    assert req.scan_mode is ScanMode.library
    assert req.scan_path is None  # library-mode servers get scan_path=None
    assert req.top_folder is None
    assert req.scan_key == "lib:7"  # invariant 2: f"lib:{library_id}"
    assert req.library_id == "7"


def test_route_carries_event_context() -> None:
    config = make_runtime_config([make_folder_route()])
    req = route(_event("/data/tv/Shoresy/ep.mkv"), config)[0]
    assert req.event_type is FsEventType.created
    assert req.file_path == "/data/tv/Shoresy/ep.mkv"
    assert req.server_name == "plex-1"


# --- route: event_types subscription -----------------------------------------


def test_route_excludes_server_not_subscribed_to_event_type() -> None:
    route_created_only = make_folder_route(
        server_id=1, server_name="hook", event_types=frozenset({FsEventType.created})
    )
    config = make_runtime_config([route_created_only])

    deleted_event = FsEvent(path="/data/tv/ep.mkv", event_type=FsEventType.deleted, is_dir=False)
    assert route(deleted_event, config) == []


def test_route_includes_server_subscribed_to_event_type() -> None:
    route_created_only = make_folder_route(
        server_id=1, server_name="hook", event_types=frozenset({FsEventType.created})
    )
    config = make_runtime_config([route_created_only])

    reqs = route(_event("/data/tv/ep.mkv"), config)  # _event() defaults to FsEventType.created
    assert {r.server_id for r in reqs} == {1}


def test_route_empty_event_types_matches_nothing() -> None:
    route_none = make_folder_route(server_id=1, server_name="hook", event_types=frozenset())
    config = make_runtime_config([route_none])
    assert route(_event("/data/tv/ep.mkv"), config) == []
