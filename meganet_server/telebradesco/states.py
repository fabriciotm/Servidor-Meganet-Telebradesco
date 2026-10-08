"""Typed Telebradesco session mode/stage values.

These enums subclass ``str`` to preserve compatibility with legacy string
comparisons while making state transitions explicit and type-checkable.
"""
from __future__ import annotations

from enum import Enum


class TelebradescoMode(str, Enum):
    COMMAND = "command"
    LOGIN_FRAME = "login_frame"
    LOGIN_CONFIRM = "login_confirm"
    ACK3 = "ack3"

    def __str__(self) -> str:
        return self.value


class TelebradescoStage(str, Enum):
    AWAIT_INITIAL_FRH = "await_initial_frh"
    AWAIT_HELLO_FTH = "await_hello_fth"
    AWAIT_HELLO_FRM = "await_hello_frm"
    AWAIT_LOGIN_FTH = "await_login_fth"
    AWAIT_LOGIN_CONFIRM = "await_login_confirm"
    AWAIT_DATA_FTH = "await_data_fth"
    AWAIT_DATA_FRM = "await_data_frm"
    AWAIT_TERMINAL = "await_terminal"
    FINISHED = "finished"

    def __str__(self) -> str:
        return self.value
