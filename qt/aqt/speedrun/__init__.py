# Re-export the public Qt-driver API so callers can do:
#   from aqt.speedrun import maybe_start
from aqt.speedrun.driver import SpeedrunController, maybe_start, scope_topics

__all__ = ["maybe_start", "scope_topics", "SpeedrunController"]
