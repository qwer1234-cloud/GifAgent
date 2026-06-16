"""Test unified JSON parser."""
import pytest
from app.services.json_guard import parse_json_response, JsonParseResult


def test_plain_json():
    r = parse_json_response('{"a": 1}')
    assert r.ok
    assert r.data == {"a": 1}


def test_fenced_json():
    r = parse_json_response('```json\n{"a": 1}\n```')
    assert r.ok
    assert r.data == {"a": 1}


def test_think_tag():
    r = parse_json_response("<think>reasoning</think>\n{\"a\": 1}")
    assert r.ok
    assert r.data == {"a": 1}


def test_text_before_json():
    r = parse_json_response("Here is your JSON: {\"a\": 1}")
    assert r.ok
    assert r.data == {"a": 1}


def test_invalid_json():
    r = parse_json_response("not json at all")
    assert not r.ok
    assert r.error is not None


def test_empty_string():
    r = parse_json_response("")
    assert not r.ok


def test_none_input():
    r = parse_json_response("")
    assert not r.ok


def test_nested_braces():
    r = parse_json_response('{"a": {"b": 2}, "c": [1,2,3]}')
    assert r.ok
    assert r.data == {"a": {"b": 2}, "c": [1, 2, 3]}


def test_think_with_fence():
    r = parse_json_response("<think>x</think>\n```json\n{\"a\": 1}\n```")
    assert r.ok
    assert r.data == {"a": 1}
