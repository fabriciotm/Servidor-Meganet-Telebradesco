"""Typed MegaNet session states.

The enum subclasses ``str`` so legacy comparisons and JSON/log output keep the
same values used by the validated implementation.
"""
from __future__ import annotations

from enum import Enum


class MegaNetState(str, Enum):
    HAYES = "HAYES"
    LOGIN_PROMPT = "LOGIN_PROMPT"
    WAIT_LOGIN = "WAIT_LOGIN"
    WAIT_PASSWORD = "WAIT_PASSWORD"
    WAIT_PROFILE_FIELD = "WAIT_PROFILE_FIELD"
    WAIT_DLE = "WAIT_DLE"
    WAIT_POST_DLE = "WAIT_POST_DLE"
    PACKET = "PACKET"
    AUTH_REJECTED = "AUTH_REJECTED"

    def __str__(self) -> str:
        return self.value
