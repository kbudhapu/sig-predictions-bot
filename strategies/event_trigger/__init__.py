"""All-237 event-trigger edge (SIGNAL/TAIL tier) — SIG abnormal-move detection + GDELT news-volume as
the justified-vs-overdone discriminator. FLAG-GATED (enabled=False by default): nothing trades until a
human turns it on. Direction comes from the move + justified/overdone, never from classifying news."""
from .strategy import EventTriggerStrategy, STRATEGY_ID

__all__ = ["EventTriggerStrategy", "STRATEGY_ID"]
