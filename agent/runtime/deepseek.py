"""Shared DeepSeek model identity and September 2026 migration rules."""

from urllib.parse import urlparse


DEEPSEEK_FLASH = "deepseek-flash"
# Astra retires the old Flash presets; the API already redirects those names.
# DeepSeek reversed the planned V4 Pro redirect (September 2026 updates), so
# deepseek-v4-pro stays a first-class selectable model instead of migrating.
LEGACY_DEEPSEEK_MODELS = frozenset({
    "deepseek-v4-flash",
    "deepseek-v4-flash-vision-exp",
    "deepseek-v4.1-flash-expires-on-0910",
})
DEEPSEEK_VISION_MODELS = frozenset({
    DEEPSEEK_FLASH,
    "deepseek-v4-flash-vision-exp",
    "deepseek-v4.1-flash-expires-on-0910",
})
DEEPSEEK_IMAGE_TOKENS = 1024


def canonical_deepseek_model(model: str, base_url: str) -> str:
    """Migrate official API IDs without renaming local or third-party models."""
    if (
        model in LEGACY_DEEPSEEK_MODELS
        and (urlparse(base_url).hostname or "").lower() == "api.deepseek.com"
    ):
        return DEEPSEEK_FLASH
    return model


def is_deepseek_model(model: str) -> bool:
    """Recognize the stable API name and legacy V4 gateway model names."""
    normalized = model.lower().rsplit("::", 1)[-1].rsplit("/", 1)[-1]
    return normalized == DEEPSEEK_FLASH or normalized.startswith("deepseek-v4")
