from astrbot.api.star import Context, Star


class QQOfficialFullPlugin(Star):
    """Register the QQ Official full platform adapters and agent tools."""

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        from .qqofficial_adapter import (  # noqa: F401
            QQOfficialFullPlatformAdapter,
            QQOfficialFullWebhookPlatformAdapter,
            _register_unique_session_builders,
        )
        from .tools import QQBotPlatformApiTool

        _register_unique_session_builders()
        context.add_llm_tools(QQBotPlatformApiTool())
