from __future__ import annotations


class TetoRendererError(RuntimeError):
    """Base error raised by the lightweight Kasane Teto renderer."""


class TetoConfigurationError(TetoRendererError):
    """The renderer assets or executable are not configured correctly."""


class TetoVoicebankError(TetoRendererError):
    """The configured UTAU voicebank is invalid or incomplete."""


class TetoResourceError(TetoRendererError):
    """The phone cannot safely start a heavy Teto render right now."""


class TetoSynthesisError(TetoRendererError):
    """The resampler or audio assembly failed."""
