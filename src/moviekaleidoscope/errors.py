"""Exceptions carrying the CLI exit code (see SPEC §10)."""


class MkError(Exception):
    """Base class. `exit_code` is the process exit code the CLI should use."""

    exit_code = 1


class ConfigError(MkError):
    """Invalid arguments or configuration."""

    exit_code = 2


class NoValidInputError(MkError):
    """No usable input video."""

    exit_code = 3


class ToolNotFoundError(MkError):
    """ffmpeg/ffprobe missing or unusable."""

    exit_code = 4


class AnalysisError(MkError):
    """Audio analysis (BPM detection) failed."""

    exit_code = 1


class RenderError(MkError):
    """A segment or the final concat failed."""

    exit_code = 1


class Cancelled(MkError):
    """The user (or caller) cancelled the run."""

    exit_code = 130
