from astrbot.api.star import Context, Star


class QQOfficialFullPlugin(Star):
    """Register the QQ Official full platform adapters."""

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        from .qqofficial_adapter import (  # noqa: F401
            QQOfficialFullPlatformAdapter,
            QQOfficialFullWebhookPlatformAdapter,
        )
