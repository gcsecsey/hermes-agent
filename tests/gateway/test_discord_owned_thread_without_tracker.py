"""A thread the bot owns is a bot thread, even when the tracker never saw it being opened.

Threads created with the bot token outside the adapter (a REST script, the ``discord`` tool's
``create_thread``, a cron handoff) never reach a ``ThreadParticipationTracker.mark()`` site: the
bot's own messages are rejected by admission before anything is marked. Replies in such a thread
were then dropped unless they @mentioned the bot, and the first mention's backfill stopped at the
bot's own post, so the thread's opening message (the bot's report) never reached the context.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import plugins.platforms.discord.adapter as discord_platform
from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter

BOT_ID = 999
THREAD_ID = 4242


class FakeThread:
    def __init__(self, history_messages, *, owner_id):
        self.id = THREAD_ID
        self.name = "Számla-egyeztetés"
        self.parent = SimpleNamespace(id=100, name="webshop", guild=SimpleNamespace(name="Shop"))
        self.parent_id = self.parent.id
        self.guild = self.parent.guild
        self.topic = None
        self.owner_id = owner_id
        self._history = list(history_messages)

    def history(self, *, limit, before, after=None, oldest_first=None):
        before_id = int(getattr(before, "id", before))
        after_id = int(getattr(after, "id", after)) if after is not None else None
        messages = [
            m for m in self._history
            if int(m.id) < before_id and (after_id is None or int(m.id) > after_id)
        ]
        messages.sort(key=lambda m: int(m.id), reverse=not (oldest_first or False))

        async def _iter():
            for m in messages[:limit]:
                yield m

        return _iter()


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr(discord_platform.discord, "Thread", FakeThread, raising=False)
    for var in ("DISCORD_THREAD_REQUIRE_MENTION", "DISCORD_FREE_RESPONSE_CHANNELS",
                "DISCORD_ALLOWED_CHANNELS", "DISCORD_IGNORED_CHANNELS", "DISCORD_ALLOW_BOTS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake-token"))
    adapter.config.extra["history_backfill"] = True
    adapter._client = SimpleNamespace(
        user=SimpleNamespace(id=BOT_ID, display_name="Vali", name="Vali", bot=True))
    adapter._text_batch_delay_seconds = 0
    adapter.handle_message = AsyncMock()
    return adapter


def _script_thread(adapter, *, owner_id):
    """A thread opened by a script: the bot posted a report into it, then a human replied."""
    report = SimpleNamespace(
        id=10, author=adapter._client.user, attachments=[],
        content="Daily invoice report: 12 orders without an invoice",
        type=discord_platform.discord.MessageType.default,
    )
    return FakeThread([report], owner_id=owner_id)


def _untagged_reply(channel):
    return SimpleNamespace(
        id=20, content="why were these not invoiced?", mentions=[], attachments=[],
        reference=None, created_at=datetime.now(UTC), channel=channel,
        author=SimpleNamespace(id=42, display_name="Gergely", name="Gergely"),
        type=discord_platform.discord.MessageType.default,
    )


@pytest.mark.asyncio
async def test_untagged_reply_in_bot_owned_thread_is_handled_with_the_bots_opening_post(adapter):
    thread = _script_thread(adapter, owner_id=BOT_ID)
    assert str(THREAD_ID) not in adapter._threads

    await adapter._handle_message(_untagged_reply(thread))

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert "Daily invoice report" in (event.channel_context or "")
    # Tracked from now on, so later turns take the normal bot-thread path and normal backfill.
    assert str(THREAD_ID) in adapter._threads


@pytest.mark.asyncio
async def test_ownership_does_not_open_foreign_threads_or_override_thread_require_mention(
    adapter, monkeypatch,
):
    await adapter._handle_message(_untagged_reply(_script_thread(adapter, owner_id=12345)))
    adapter.handle_message.assert_not_awaited()

    monkeypatch.setenv("DISCORD_THREAD_REQUIRE_MENTION", "true")
    await adapter._handle_message(_untagged_reply(_script_thread(adapter, owner_id=BOT_ID)))
    adapter.handle_message.assert_not_awaited()
