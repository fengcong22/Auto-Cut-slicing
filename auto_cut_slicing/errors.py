class SlicingError(ValueError):
    """An input, dependency or acceptance gate failed."""


class VisualSpeechSyncError(SlicingError):
    """A source-bound speech cue does not match its visual anchor."""
