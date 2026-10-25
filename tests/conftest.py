"""Shared test helpers: a fake Claude client and example people."""

from types import SimpleNamespace

import pytest

from jarvis_core.users import Role, User

OLIVER = User("oliver", "Oliver", Role.ADMIN, telegram_ids=frozenset({1}))
CHILD = User("mia", "Mia", Role.CHILD, telegram_ids=frozenset({2}))


def text_block(text):
    return SimpleNamespace(type="text", text=text)


def tool_block(id_, name, input_):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=input_)


def resp(stop_reason, *blocks, usage=None):
    return SimpleNamespace(stop_reason=stop_reason, content=list(blocks), usage=usage)


def usage(inp, out):
    return SimpleNamespace(input_tokens=inp, output_tokens=out,
                           cache_creation_input_tokens=0, cache_read_input_tokens=None)


class FakeClient:
    """Plays back a fixed sequence of Claude responses and records the requests."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.messages = self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def oliver():
    return OLIVER


@pytest.fixture
def child():
    return CHILD
