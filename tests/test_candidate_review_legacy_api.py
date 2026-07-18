"""Contract tests for the historical candidate_review module API."""

from app.ui import candidate_review


LEGACY_PUBLIC_API = {
    "_build_gradio_allowed_paths",
    "get_process_command_line",
    "is_batch_process",
    "get_batch_status",
    "stop_batch",
    "start_batch",
    "_safe_float",
    "_ensure_candidate_thumbnail",
    "_candidate_display_path",
    "_format_api_error",
    "_folder_label",
    "load_candidates",
    "selection_values",
    "select_candidate",
    "submit_review_action",
    "undo_and_refresh",
    "get_profile_status",
    "load_profile_publish_choices",
    "build_profile",
    "build_profile_and_refresh",
    "publish_profile_and_refresh",
    "config_field_name",
    "config_textbox",
    "config_checkbox",
    "load_config",
    "save_config",
    "test_llm_connection",
}


def test_historical_candidate_review_api_remains_importable():
    missing = sorted(name for name in LEGACY_PUBLIC_API if not hasattr(candidate_review, name))
    assert missing == []
