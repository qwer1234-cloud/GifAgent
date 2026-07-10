from app.ui.candidate_review import (
    CONFIG_FIELD_HELP,
    CONFIG_FIELD_KEYS,
    CONFIG_TOOLTIP_CSS,
    config_checkbox_kwargs,
    config_field_kwargs,
    config_field_label,
    config_tooltip_icon,
    launch_kwargs,
)


def test_every_config_field_has_non_empty_chinese_tooltip_label():
    assert set(CONFIG_FIELD_HELP) == set(CONFIG_FIELD_KEYS)
    assert len(CONFIG_FIELD_KEYS) == 20

    for key in CONFIG_FIELD_KEYS:
        if key == "preference_memory.enabled":
            kwargs = config_checkbox_kwargs(key)
            label = config_tooltip_icon(key)
            assert kwargs == {"label": "enabled", "container": False}
            assert 'class="config-tooltip-icon"' in label
        else:
            kwargs = config_field_kwargs(key)
            label = config_field_label(key)
            assert kwargs["show_label"] is False
            assert "info" not in kwargs
            assert 'class="config-tooltip-icon"' in label
        assert CONFIG_FIELD_HELP[key] in label
        assert any("\u4e00" <= char <= "\u9fff" for char in CONFIG_FIELD_HELP[key])


def test_launch_injects_tooltip_css():
    kwargs = launch_kwargs()
    assert kwargs["css"] == CONFIG_TOOLTIP_CSS
    assert ".config-tooltip-icon:hover .config-tooltip-text" in kwargs["css"]
