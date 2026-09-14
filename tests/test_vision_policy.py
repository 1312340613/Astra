import json

import pytest

from agent.cli.models import model_profiles
from agent.runtime.llm import LLMConfig
from agent.runtime.vision_policy import parse_vision_preprocess_policy


def test_deepseek_vision_profile_has_only_bundled_tiling_policy():
    profiles = model_profiles()
    policy = profiles["deepseek-flash"].vision_preprocess
    assert policy is not None
    assert (policy.tile_width, policy.tile_height, policy.overlap) == (768, 768, 64)
    assert policy.max_images_per_request == 32
    assert policy.max_inline_body_bytes == 41_943_040
    assert profiles["deepseek-flash"].vision_detail == "original"
    assert profiles["Qwen3.6-35B-A3B"].vision_preprocess is None


@pytest.mark.parametrize(("name", "capabilities", "message"), [
    ("unsupported-vision", ["vision"], "only supported for deepseek-flash"),
    ("deepseek-flash", [], "requires the vision capability"),
])
def test_user_override_rejects_unsupported_vision_preprocess_profile(
    tmp_path, monkeypatch, name, capabilities, message,
):
    override = tmp_path / "models.yaml"
    override.write_text(json.dumps({"models": {
        name: {
            "base_url": "https://example.test/v1",
            "context_limit": 1_000,
            "capabilities": capabilities,
            "vision_preprocess": {"strategy": "tiled"},
        },
    }}), encoding="utf-8")
    monkeypatch.setenv("AGENT_USER_MODELS_FILE", str(override))

    with pytest.raises(ValueError, match=message):
        model_profiles()


@pytest.mark.parametrize("raw, message", [
    ({"strategy": "resize"}, "strategy must be tiled"),
    ({"strategy": "tiled", "tile_width": 0}, "tile_width must be positive"),
    ({"strategy": "tiled", "tile_width": 64, "tile_height": 64, "overlap": 64}, "overlap"),
    ({"strategy": "tiled", "max_images_per_request": 0}, "max_images_per_request"),
    ({"strategy": "tiled", "overflow_strategy": "sample"}, "overflow_strategy must be model_select"),
])
def test_policy_rejects_invalid_values(raw, message):
    with pytest.raises(ValueError, match=message):
        parse_vision_preprocess_policy("broken", raw)


@pytest.mark.parametrize("field", [
    "tile_width",
    "tile_height",
    "overlap",
    "max_images_per_request",
    "max_inline_body_bytes",
])
@pytest.mark.parametrize("value", [True, 1.5])
def test_policy_rejects_non_integer_numeric_values(field, value):
    with pytest.raises(ValueError, match=rf"{field} must be an integer"):
        parse_vision_preprocess_policy("broken", {field: value})


def test_llm_config_carries_policy_across_replace():
    policy = parse_vision_preprocess_policy("vision", {"strategy": "tiled"})
    config = LLMConfig(model="vision", vision_preprocess=policy)
    assert config.vision_preprocess is policy
