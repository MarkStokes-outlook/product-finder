from unittest import mock

import requests

from product_finder import extraction
from product_finder.config import OllamaConfig


def _cfg(**overrides):
    return OllamaConfig(enabled=True, base_url="http://ollama.test", **overrides)


def _ollama_response(payload: dict):
    resp = mock.Mock()
    resp.raise_for_status = mock.Mock()
    resp.json = mock.Mock(return_value={"response": __import__("json").dumps(payload)})
    return resp


def test_disabled_skips_without_a_request():
    with mock.patch("product_finder.extraction.requests.post") as post:
        result = extraction.extract_brand_model("Makita LS0816F/2 mitre saw", OllamaConfig(enabled=False))
    assert result is None
    post.assert_not_called()


def test_ollama_unavailable_is_skipped_gracefully(caplog):
    with mock.patch(
        "product_finder.extraction.requests.post",
        side_effect=requests.ConnectionError("connection refused"),
    ):
        with caplog.at_level("WARNING"):
            result = extraction.extract_brand_model("Makita LS0816F/2 mitre saw", _cfg())
    assert result is None
    assert "skipped" in caplog.text.lower()
    assert "unavailable" in caplog.text.lower()


def test_valid_extraction_returns_brand_and_model():
    response = _ollama_response({"brand": "Makita", "model": "LS0816F/2", "confidence": 0.9})
    with mock.patch("product_finder.extraction.requests.post", return_value=response):
        result = extraction.extract_brand_model("Makita LS0816F/2 mitre saw, barely used", _cfg())
    assert result == {"brand": "Makita", "model": "LS0816F/2"}


def test_low_confidence_extraction_is_ignored():
    response = _ollama_response({"brand": "Makita", "model": "LS0816F/2", "confidence": 0.4})
    with mock.patch("product_finder.extraction.requests.post", return_value=response):
        result = extraction.extract_brand_model("mitre saw, unbranded looking", _cfg(minimum_confidence=0.75))
    assert result is None


def test_no_brand_identified_is_ignored():
    response = _ollama_response({"brand": "", "model": "", "confidence": 0})
    with mock.patch("product_finder.extraction.requests.post", return_value=response):
        result = extraction.extract_brand_model("mystery power tool, no branding visible", _cfg())
    assert result is None


def test_malformed_json_response_is_ignored(caplog):
    resp = mock.Mock()
    resp.raise_for_status = mock.Mock()
    resp.json = mock.Mock(return_value={"response": "not valid json"})
    with mock.patch("product_finder.extraction.requests.post", return_value=resp):
        with caplog.at_level("WARNING"):
            result = extraction.extract_brand_model("mitre saw", _cfg())
    assert result is None
    assert "malformed" in caplog.text.lower()


def test_missing_response_field_is_ignored():
    resp = mock.Mock()
    resp.raise_for_status = mock.Mock()
    resp.json = mock.Mock(return_value={"unexpected": "shape"})
    with mock.patch("product_finder.extraction.requests.post", return_value=resp):
        result = extraction.extract_brand_model("mitre saw", _cfg())
    assert result is None


def test_non_numeric_confidence_is_ignored():
    response = _ollama_response({"brand": "Makita", "model": "LS0816F/2", "confidence": "high"})
    with mock.patch("product_finder.extraction.requests.post", return_value=response):
        result = extraction.extract_brand_model("Makita LS0816F/2 mitre saw", _cfg())
    assert result is None


def test_request_uses_configured_model_and_timeout():
    response = _ollama_response({"brand": "Makita", "model": "", "confidence": 0.8})
    with mock.patch("product_finder.extraction.requests.post", return_value=response) as post:
        extraction.extract_brand_model("Makita mitre saw", _cfg(model="qwen2.5:7b", timeout=5))
    _, kwargs = post.call_args
    assert kwargs["json"]["model"] == "qwen2.5:7b"
    assert kwargs["timeout"] == 5
    assert kwargs["json"]["format"] == "json"
