from app.ui.candidate_review import CONFIG_FIELD_HELP, CONFIG_FIELD_KEYS, config_field_kwargs


def test_every_config_field_has_non_empty_chinese_help_and_question_marker():
    assert set(CONFIG_FIELD_HELP) == set(CONFIG_FIELD_KEYS)
    assert len(CONFIG_FIELD_KEYS) == 20

    for key in CONFIG_FIELD_KEYS:
        kwargs = config_field_kwargs(key)
        assert "?" in kwargs["label"]
        assert kwargs["info"]
        assert any("\u4e00" <= char <= "\u9fff" for char in kwargs["info"])
