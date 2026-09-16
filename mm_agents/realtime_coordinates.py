"""Explicit model coordinate contracts; screenshots and the VM stay 1920x1080.

Gemini: OSWorld-V2 uses integer 0..1000; older action tables say 0..999.
Both use denominator 1000. Keep the older contract for explicit configs/logs.
https://ai.google.dev/gemini-api/docs/computer-use
https://github.com/xlang-ai/OSWorld-V2/blob/627a1d691fbd0fd93b6161ecafa81530d50f6138/mm_agents/gemini_agent.py
MiniMax M3 OSWorld evaluation: relative 0..1000 on 1920x1080 images.
Its upstream converter does not clip the 1000 endpoint to size-1.
https://www.minimax.io/blog/minimax-m3
"""
import copy
from dataclasses import dataclass


COORDINATE_SYSTEMS = {
    "native_pixels": None,
    "normalized_0_999": 999,
    "normalized_0_1000": 1000,
    "normalized_0_1000_unclipped": 1000,
}
SCREEN_SIZE = {"x": 1920, "y": 1080}


@dataclass(frozen=True)
class CoordinateAdapter:
    name: str = "native_pixels"

    def __post_init__(self):
        if not isinstance(self.name, str) or self.name not in COORDINATE_SYSTEMS:
            raise ValueError(
                "api.coordinate_system must be one of " + ", ".join(COORDINATE_SYSTEMS)
            )

    @property
    def maximum(self):
        return COORDINATE_SYSTEMS[self.name]

    @property
    def guidance(self):
        if self.maximum is None:
            return (
                "The VM screen and screenshots use the native 1920x1080 pixel coordinate system. "
                "Always submit x/y action parameters in native screen pixels measured from the "
                "top-left corner. Do not rescale or multiply coordinates."
            )
        endpoints = (
            "(0, 0) is the top-left corner and (1000, 1000) is the bottom-right "
            "corner, regardless of the screenshot resolution. "
            if self.maximum == 1000 else "The origin is at the top-left. "
        )
        return (
            "The screenshot is the original 1920x1080 image. "
            f"Return all x/y action coordinates as normalized integers in [0, {self.maximum}], "
            "relative to the full screenshot in a 1000x1000 coordinate space, "
            "measured from the top-left. "
            + endpoints
            + "The runtime converts these coordinates to "
            "native screen pixels; do not convert them yourself."
        )

    def action_tools(self, tools):
        """Adapt per-agent schemas without changing the shared VM action definitions."""
        adapted = copy.deepcopy(tools)
        if self.maximum is not None:
            for tool in adapted:
                for axis, field in tool["parameters"].get("properties", {}).items():
                    if axis in SCREEN_SIZE:
                        field.update(
                            type="integer", minimum=0, maximum=self.maximum,
                            description=(
                                f"Normalized {axis} coordinate in [0, {self.maximum}], "
                                "relative to the full screenshot on a 1000x1000 scale "
                                "with origin at the top-left."
                            ),
                        )
        return adapted

    def to_native_value(self, axis, value):
        if self.maximum is None:
            return value
        if type(value) is not int or not 0 <= value <= self.maximum:
            raise ValueError(
                f"{axis} must be a normalized integer in [0, {self.maximum}] "
                f"for {self.name}; native screen pixels are not accepted."
            )
        size = SCREEN_SIZE[axis]
        if self.name == "normalized_0_1000_unclipped":
            # OSWorld M3 parser.scaled_xy: int(value * original_size / 1000).
            # Preserve its endpoint: 1000 maps to size, without an extra clamp.
            return int(value * size / 1000)
        # OSWorld-V2 Gemini _pixel bounds the endpoint to the last pixel.
        return min(value * size // 1000, size - 1)

    def to_native_action(self, action):
        """Copy first so provider replies and subsequent assistant history stay raw."""
        converted = copy.deepcopy(action)
        params = converted.get("parameters", {})
        if isinstance(params, dict):
            for axis in SCREEN_SIZE:
                if params.get(axis) is not None:
                    params[axis] = self.to_native_value(axis, params[axis])
        return converted
