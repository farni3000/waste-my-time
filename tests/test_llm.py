import pytest
from pydantic import ValidationError

from app.llm.deepseek import _extract_json
from app.llm.schemas import LLMResponse


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
