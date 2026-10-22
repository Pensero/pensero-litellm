"""Send the usage of a LiteLLM proxy as OpenTelemetry log events: tokens, cost, model, user and edited lines."""

__version__ = "0.1.0"

from pensero_litellm.handler import OTelLogger  # noqa: E402

handler = OTelLogger()
"""The instance to set in the LiteLLM `config.yaml`: `litellm_settings: {callbacks: pensero_litellm.handler}`."""

__all__ = ["OTelLogger", "handler"]
