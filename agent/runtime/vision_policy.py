from dataclasses import dataclass


@dataclass(frozen=True)
class VisionPreprocessPolicy:
    strategy: str = "tiled"
    tile_width: int = 768
    tile_height: int = 768
    overlap: int = 64
    max_images_per_request: int = 12
    max_inline_body_bytes: int = 41_943_040
    overflow_strategy: str = "model_select"

    @property
    def stride_x(self) -> int:
        return self.tile_width - self.overlap

    @property
    def stride_y(self) -> int:
        return self.tile_height - self.overlap


def parse_vision_preprocess_policy(model_name: str, raw: object) -> VisionPreprocessPolicy | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"Model '{model_name}' vision_preprocess must be an object")
    policy = VisionPreprocessPolicy(
        strategy=str(raw.get("strategy", "tiled")).strip().lower(),
        tile_width=_integer_field("tile_width", raw.get("tile_width", 768)),
        tile_height=_integer_field("tile_height", raw.get("tile_height", 768)),
        overlap=_integer_field("overlap", raw.get("overlap", 64)),
        max_images_per_request=_integer_field(
            "max_images_per_request", raw.get("max_images_per_request", 12)
        ),
        max_inline_body_bytes=_integer_field(
            "max_inline_body_bytes", raw.get("max_inline_body_bytes", 41_943_040)
        ),
        overflow_strategy=str(raw.get("overflow_strategy", "model_select")).strip().lower(),
    )
    if policy.strategy != "tiled":
        raise ValueError("vision_preprocess strategy must be tiled")
    for name in ("tile_width", "tile_height", "max_images_per_request", "max_inline_body_bytes"):
        if getattr(policy, name) <= 0:
            raise ValueError(f"vision_preprocess {name} must be positive")
    if policy.overlap < 0 or policy.overlap >= min(policy.tile_width, policy.tile_height):
        raise ValueError("vision_preprocess overlap must be non-negative and smaller than each tile dimension")
    if policy.overflow_strategy != "model_select":
        raise ValueError("vision_preprocess overflow_strategy must be model_select")
    return policy


def _integer_field(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"vision_preprocess {name} must be an integer")
    return value
