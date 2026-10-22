import pytest
from pydantic import ValidationError

from app.llm import models
from app.llm.deepseek import _extract_json
from app.llm.schemas import LLMResponse


def _catalog() -> list[dict]:
    return [
        {
            "id": "deepseek-flash",
            "name": "DeepSeek-V4.1-Flash",
            "effort": {"supported_levels": ["low", "high", "max"]},
        },
        {"id": "deepseek-v4-pro", "name": "DeepSeek-V4-Pro"},
        {"id": "deepseek-v5-pro", "name": "DeepSeek-V5-Pro"},
    ]


def _settings(**overrides):
    from types import SimpleNamespace

    values = {
        "deepseek_model": "",
        "deepseek_model_tier": "pro",
        "llm_reasoning_effort": "low",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_extract_json_handles_markdown_fence():
    payload = _extract_json('```json\n{"recommendations": []}\n```')
    assert payload == {"recommendations": []}


def test_llm_response_validation():
    parsed = LLMResponse.model_validate(
        {
            "recommendations": [
                {"candidate_id": 1, "rank": 1, "reason": "passt", "match_tags": ["Strategy"]}
            ]
        }
    )
    assert parsed.recommendations[0].candidate_id == 1
    assert parsed.recommendations[0].confidence == 0.5


def test_llm_response_rejects_bad_payload():
    with pytest.raises(ValidationError):
        LLMResponse.model_validate({"recommendations": [{"rank": 1}]})


def test_version_key_orders_v4_before_v41():
    assert models._version_key("DeepSeek-V4-Pro") < models._version_key("DeepSeek-V4.1-Flash")


def test_pick_model_prefers_newest_pro():
    assert models.pick_model(_catalog(), "pro") == "deepseek-v5-pro"


def test_pick_model_flash_stays_in_its_class():
    assert models.pick_model(_catalog(), "flash") == "deepseek-flash"


def test_pick_model_handles_unknown_tier_and_empty_catalog():
    assert models.pick_model(_catalog(), "ultra") is None
    assert models.pick_model([], "pro") is None


def test_resolve_model_uses_pin(monkeypatch):
    monkeypatch.setattr(
        models, "get_settings", lambda: _settings(deepseek_model="deepseek-v4.1-flash-1017")
    )
    monkeypatch.setattr(models, "load_catalog", lambda session=None: [])
    assert models.resolve_model() == "deepseek-v4.1-flash-1017"


def test_resolve_model_falls_back_without_catalog(monkeypatch):
    monkeypatch.setattr(models, "get_settings", lambda: _settings())
    monkeypatch.setattr(models, "load_catalog", lambda session=None: [])
    assert models.resolve_model() == models.TIER_FALLBACK["pro"]


def test_resolve_model_picks_newest_from_catalog(monkeypatch):
    monkeypatch.setattr(models, "get_settings", lambda: _settings())
    monkeypatch.setattr(models, "load_catalog", lambda session=None: _catalog())
    assert models.resolve_model() == "deepseek-v5-pro"


def test_effective_effort_only_for_supporting_models(monkeypatch):
    monkeypatch.setattr(models, "get_settings", lambda: _settings())
    monkeypatch.setattr(models, "load_catalog", lambda session=None: _catalog())
    assert models.effective_effort(None, "deepseek-flash") == "low"
    assert models.effective_effort(None, "deepseek-v4-pro") == ""


def test_effective_effort_respects_empty_setting(monkeypatch):
    monkeypatch.setattr(models, "get_settings", lambda: _settings(llm_reasoning_effort=""))
    monkeypatch.setattr(models, "load_catalog", lambda session=None: _catalog())
    assert models.effective_effort(None, "deepseek-flash") == ""

