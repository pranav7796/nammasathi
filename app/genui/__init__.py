"""Generative UI: server-built, validated widgets (docs/genui-spec.md). The model writes none."""

from app.genui.actions import ACTION_IDS, is_known_action
from app.genui.models import Widget
from app.genui.sanitize import clean_text
from app.genui.validate import GenUIError, to_wire, validate_envelope, validate_widget

__all__ = [
    "ACTION_IDS",
    "GenUIError",
    "Widget",
    "clean_text",
    "is_known_action",
    "to_wire",
    "validate_envelope",
    "validate_widget",
]
