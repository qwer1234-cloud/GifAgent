from app.ui.candidate_review import (
    CONFIG_FIELD_HELP,
    CONFIG_FIELD_KEYS,
    CONFIG_TOOLTIP_CSS,
    CONFIG_TOOLTIP_JS,
    REVIEW_SHORTCUTS_JS,
    REVIEW_LAYOUT_CSS,
    config_checkbox_kwargs,
    config_field_kwargs,
    config_field_label,
    config_tooltip_icon,
    launch_kwargs,
)


def test_every_config_field_has_non_empty_chinese_tooltip_label():
    assert set(CONFIG_FIELD_HELP) == set(CONFIG_FIELD_KEYS)
    assert len(CONFIG_FIELD_KEYS) == 22
    assert "preference_memory.base_score_weight" in CONFIG_FIELD_KEYS
    assert "preference_memory.preference_score_weight" in CONFIG_FIELD_KEYS

    for key in CONFIG_FIELD_KEYS:
        if key == "preference_memory.enabled":
            kwargs = config_checkbox_kwargs(key)
            label = config_tooltip_icon(key)
            assert kwargs == {
                "label": "enabled",
                "container": False,
                "elem_id": "preference-memory-enabled",
            }
            assert 'class="config-tooltip-icon"' in label
        else:
            kwargs = config_field_kwargs(key)
            label = config_field_label(key)
            assert kwargs["show_label"] is False
            assert "info" not in kwargs
            assert 'class="config-tooltip-icon"' in label
        assert f'title="{CONFIG_FIELD_HELP[key]}"' in label
        assert "config-tooltip-text" not in label
        assert any("\u4e00" <= char <= "\u9fff" for char in CONFIG_FIELD_HELP[key])


def test_launch_injects_tooltip_css():
    kwargs = launch_kwargs()
    assert kwargs["css"] == CONFIG_TOOLTIP_CSS + REVIEW_LAYOUT_CSS
    assert ".config-tooltip-icon" in kwargs["css"]
    assert kwargs["js"] == CONFIG_TOOLTIP_JS + REVIEW_SHORTCUTS_JS
    assert "preference-memory-enabled" in kwargs["js"]
    assert kwargs["js"].lstrip().startswith("(() => {")
    assert "setTimeout(attach" in kwargs["js"]
