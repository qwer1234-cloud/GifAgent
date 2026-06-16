"""Test VLM output quality validation."""
import pytest
from app.services.quality import (
    detect_placeholder_text,
    normalize_emotional_core,
    validate_frame_analysis,
    validate_media_annotation,
)


# ── detect_placeholder_text ────────────────────────────────────────────

def test_detects_what_you_see():
    assert detect_placeholder_text("what you see") == ["what you see"]


def test_detects_case_insensitive():
    assert detect_placeholder_text("What You See in this frame") == ["what you see"]


def test_detects_one_reason():
    assert "one reason" in detect_placeholder_text("one reason to like it")


def test_detects_2_3_observations():
    result = detect_placeholder_text("2-3 observations about lighting")
    assert "2-3 observations" in result


def test_clean_value_passes():
    assert detect_placeholder_text("A woman standing in dim light") == []


def test_empty_string():
    assert detect_placeholder_text("") == ["empty"]


# ── normalize_emotional_core ───────────────────────────────────────────

def test_valid_emotion_passes():
    assert normalize_emotional_core("tension") == "tension"


def test_pipe_delimited_takes_first():
    assert normalize_emotional_core("tension|melancholy") == "tension"


def test_comma_delimited():
    assert normalize_emotional_core("joy, sadness") == "joy"


def test_case_insensitive():
    assert normalize_emotional_core("TENSION") == "tension"
    assert normalize_emotional_core("Joy") == "joy"


def test_invalid_falls_to_other():
    assert normalize_emotional_core("mixed emotions") == "other"


def test_none():
    assert normalize_emotional_core(None) == "other"


def test_full_option_list():
    raw = "tension | melancholy | awe | joy | sadness | catharsis | serenity | excitement | dread | nostalgia | admiration | other"
    assert normalize_emotional_core(raw) == "tension"


# ── validate_frame_analysis ────────────────────────────────────────────

def test_clean_frame_passes():
    payload = {
        "caption": "A woman stands by the window in golden hour light",
        "emotional_core": "melancholy",
        "aesthetic_notes": ["warm backlighting creates a halo effect", "shallow depth of field"],
        "why_i_like_it": "The contrast between the warm light and her expression tells a story",
    }
    cleaned, errors = validate_frame_analysis(payload)
    assert len(errors) == 0
    assert cleaned["caption"] == payload["caption"]
    assert cleaned["emotional_core"] == "melancholy"


def test_rejects_placeholder_caption():
    payload = {
        "caption": "what you see in this frame",
        "emotional_core": "tension",
        "aesthetic_notes": ["good lighting", "nice composition"],
        "why_i_like_it": "it looks cinematic and dramatic",
    }
    cleaned, errors = validate_frame_analysis(payload)
    assert len(errors) > 0
    assert any("caption placeholder" in e for e in errors)


def test_rejects_multivalue_emotion():
    payload = {
        "caption": "A man in a dark room looking at the camera",
        "emotional_core": "tension | melancholy | joy",
        "aesthetic_notes": ["low key lighting", "close-up framing"],
        "why_i_like_it": "the intensity in his eyes is captivating",
    }
    cleaned, errors = validate_frame_analysis(payload)
    assert any("multi-value" in e.lower() for e in errors)
    assert cleaned["emotional_core"] == "tension"


def test_rejects_placeholder_notes():
    payload = {
        "caption": "Two people sitting at a dinner table",
        "emotional_core": "tension",
        "aesthetic_notes": ["2-3 observations about lighting", "nice shot"],
        "why_i_like_it": "the unspoken tension between them is palpable",
    }
    cleaned, errors = validate_frame_analysis(payload)
    assert any("placeholder" in e for e in errors)


def test_gif_worthiness_out_of_range():
    payload = {
        "caption": "A car chase scene through narrow streets",
        "emotional_core": "excitement",
        "aesthetic_notes": ["fast camera movement", "tight framing creates urgency"],
        "why_i_like_it": "the kinetic energy makes you feel inside the chase",
        "gif_worthiness": 1.5,
    }
    cleaned, errors = validate_frame_analysis(payload)
    assert any("out of range" in e for e in errors)


# ── validate_media_annotation ──────────────────────────────────────────

def test_clean_media_passes():
    payload = {
        "summary": "The film uses intimate close-ups and warm lighting to convey vulnerability",
        "emotional_core": "intimacy",
        "aesthetic_notes": ["shallow depth of field", "warm color palette"],
        "why_i_like_it": "The visual intimacy draws you into the characters' inner world",
        "tags": ["drama", "intimacy", "close-up"],
    }
    cleaned, errors = validate_media_annotation(payload)
    assert len(errors) == 0
