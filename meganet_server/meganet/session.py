"""MegaNet connection state machine and reliable packet delivery.

The state machine is intentionally kept close to the validated implementation.
Domain helpers live in sibling modules; wire behavior is unchanged.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import pathlib
import re
import threading
from typing import Callable, Optional

from ..common import AsyncByteWriter, utc_now_iso
from ..storage import atomic_write_json
from .protocol import (
    STX, ETX, EOT, ACK, NAK, DLE, CR, LF, ascii_text, display_payload,
    packet_text_bytes, encode_server_frame, ClientFrameParser,
    parse_meganet_id_payload, parse_password_change_payload,
)
from .accounts import (
    MEGANET_ROOT_DIR, DEFAULT_CADASTRO_DB, FIELD_NAMES, AuthResult, CadastroState,
    CadastroDatabase, PasswordChangeStore, _safe_ascii_text, normalize_email_address,
    normalize_mail_domain, email_local_part, meganet_email_for_name,
)
from .content import (
    DEFAULT_MESSAGE, DEFAULT_TECTOY_NEWS, DEFAULT_OUTROS_GROUP, DEFAULT_MAIL_GROUP,
    CONTENT_RECORD_DEFINITIONS, CONNECTION_PANEL_PRESETS, CONTENT_RECORD_BODY_MAX_BYTES,
    MAX_CONTENT_GROUP_SAFE_BYTES, MAX_CONTENT_RECORD_LINES, MAX_CONTENT_RECORD_TOTAL_LINES,
    MAX_MESSAGE_LINE_LEN, CONTENT_BLANK_LINE_MARKER, is_content_blank_line,
    content_line_wire_bytes, strip_content_mapping_lines, merge_content_records, split_line_for_rom,
    normalize_content_record_lines, _safe_latin1_content_text, render_meganet_placeholders,
    render_meganet_placeholder_lines, fit_profile_lines_to_safe_budget, render_message_lines,
    normalize_display_message_lines, prepare_display_lines,
)
from .mail import (
    DEFAULT_WELCOME_EMAIL_SUBJECT, DEFAULT_WELCOME_EMAIL_BODY, DEFAULT_MAIL_STORE_DIR,
    MailStore, _safe_meganet_mail_text,
)

from .arq import MegaNetArqMixin, MegaNetPacketDeliveryError
from .states import MegaNetState

DAILY_SESSION_LOG_LOCK = threading.Lock()

DUPLICATE_USER_MESSAGE = [
    "O USUARIO {usuario} JA EXISTE.",
    "ESCOLHA OUTRO NOME NO MEGA NET.",
]

DUPLICATE_CODIGO_ACESSO_MESSAGE = [
    "CODIGO DE ACESSO",
    "JA CADASTRADO NO",
    "BANCO DE DADOS.",
    "ESCOLHA OUTRO CODIGO.",
]

DEFAULT_PASSWORD_CHANGE_STORE_DIR = MEGANET_ROOT_DIR / "password"


class MegaNetSession(MegaNetArqMixin):
    def __init__(
        self,
        writer: AsyncByteWriter,
        *,
        peer: str,
        message_lines: list[str],
        output_dir: pathlib.Path,
        enable_session_logs: bool = False,
        cadastro_db_path: pathlib.Path,
        online_start: bool,
        auto_ready_seen: bool = False,
        auth_mode: str = "off",
        auth_password_source: str = "bootstrap",
        auth_check_bootstrap_login: bool = False,
        auth_reject_action: str = "nak-close",
        meganet_auth_mode: str = "allow-new",
        meganet_reject_action: str = "message-then-ready",
        meganet_password_case: str = "case-insensitive",
        registration_duplicate_action: str = "reject-message",
        codigo_acesso_duplicate_action: str = "reject-message",
        transaction_close: str = "after-final-message",
        transaction_close_delay_seconds: float = 0.35,
        profile_idle_seconds: float = 0.25,
        post_dle_delay_seconds: float = 0.20,
        post_id_action: str = "none",
        post_id_close_delay_seconds: float = 0.35,
        post_id_message_delay_seconds: float = 0.15,
        outbound_line_max_len: int = 40,
        outbound_packet_delay_seconds: float = 0.04,
        meganet_packet_ack_mode: str = "all",
        meganet_packet_ack_timeout_seconds: float = 3.0,
        meganet_packet_max_retries: int = 5,
        meganet_bootstrap_noise_tolerance: int = 2,
        meganet_bootstrap_noise_window_seconds: float = 2.0,
        tectoy_news_lines: Optional[list[str]] = None,
        content_records: Optional[dict[str, list[str]]] = None,
        connection_panel_lines: Optional[list[str]] = None,
        connection_panel_mail_count: str = "auto",
        content_profile: str = "tectoy-news",
        routing_mode: str = "unified",
        news_route_map: Optional[dict[str, str]] = None,
        mail_store_dir: pathlib.Path = DEFAULT_MAIL_STORE_DIR,
        mail_inbox_path: Optional[pathlib.Path] = None,
        mail_domain: str = "meganet.tectoy.com.br",
        admin_email: str = "admin@meganet.tectoy.com.br",
        welcome_email_enabled: bool = True,
        welcome_email_from: str = "",
        welcome_email_subject: str = DEFAULT_WELCOME_EMAIL_SUBJECT,
        welcome_email_body: object = None,
        password_change_store_dir: pathlib.Path = DEFAULT_PASSWORD_CHANGE_STORE_DIR,
        password_change_apply: bool = False,
        unknown_token_action: str = "none",
        raw_hayes_in_data_action: str = "ignore",
        raw_hayes_close_delay_seconds: float = 0.20,
        access_identity_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.writer = writer
        self.peer = peer
        self.access_identity_callback = access_identity_callback
        self.message_lines = message_lines
        self.output_dir = output_dir
        self.enable_session_logs = bool(enable_session_logs)
        self.cadastro_db = CadastroDatabase(cadastro_db_path)
        self.auth_mode = auth_mode
        self.auth_password_source = auth_password_source
        self.auth_check_bootstrap_login = auth_check_bootstrap_login
        self.auth_reject_action = auth_reject_action
        self.meganet_auth_mode = meganet_auth_mode
        self.meganet_reject_action = meganet_reject_action
        self.meganet_password_case = meganet_password_case
        self.registration_duplicate_action = registration_duplicate_action
        self.codigo_acesso_duplicate_action = codigo_acesso_duplicate_action
        self.transaction_close = transaction_close
        self.transaction_close_delay_seconds = transaction_close_delay_seconds
        self.close_task: Optional[asyncio.Task] = None
        self.closed_by_transaction = False
        self.profile_idle_seconds = profile_idle_seconds
        self.post_dle_delay_seconds = post_dle_delay_seconds
        self.post_id_action = post_id_action
        self.post_id_close_delay_seconds = post_id_close_delay_seconds
        self.post_id_message_delay_seconds = max(0.0, post_id_message_delay_seconds)
        self.outbound_line_max_len = min(MAX_MESSAGE_LINE_LEN, max(8, int(outbound_line_max_len or MAX_MESSAGE_LINE_LEN)))
        self.outbound_packet_delay_seconds = max(0.0, outbound_packet_delay_seconds)

        # server-to-ROM packet delivery control.  The cartridge returns
        # raw ACK (0x06) or NAK (0x15) after each framed server packet. Earlier
        # versions sent an entire transfer before returning to reader.read(), so
        # these controls accumulated and were later discarded as parser noise.
        ack_mode = str(meganet_packet_ack_mode or "all").strip().lower()
        self.meganet_packet_ack_mode = ack_mode if ack_mode in {"off", "transfers", "all"} else "all"
        self.meganet_packet_ack_timeout_seconds = max(0.20, min(30.0, float(meganet_packet_ack_timeout_seconds or 3.0)))
        self.meganet_packet_max_retries = max(1, min(10, int(meganet_packet_max_retries or 5)))
        self.meganet_packet_retry_delay_seconds = min(2.0, max(0.0, self.outbound_packet_delay_seconds))
        self.packet_ack_queue: asyncio.Queue[object] = asyncio.Queue()
        self.outbound_packet_lock = asyncio.Lock()
        self.packet_ack_waiting_seq: Optional[int] = None
        self.packet_wire_buffer = bytearray()
        self.packet_reader_closed = False
        self.packet_reader_close_reason = ""
        self.packet_delivery_failed = False
        self.packet_tx_attempts = 0
        self.packet_tx_confirmed = 0
        self.packet_tx_retransmissions = 0
        self.packet_tx_naks = 0
        self.packet_tx_timeouts = 0
        self.packet_tx_failures = 0

        self.tectoy_news_lines = tectoy_news_lines or DEFAULT_TECTOY_NEWS[:]
        self.content_records = merge_content_records(content_records)
        self.connection_panel_lines = connection_panel_lines or CONNECTION_PANEL_PRESETS["unified"][:]
        self.connection_panel_mail_count = (connection_panel_mail_count or "auto").lower()
        self.content_profile = content_profile
        self.routing_mode = (routing_mode or "unified").lower()
        default_news_route_map = {"1": "tectoy-news", "2": "internet", "3": "revista", "4": "outros"}
        valid_route_profiles = {"tectoy-news", "internet", "revista", "mail", "outros", "troca-senha"}
        self.news_route_map = default_news_route_map.copy()
        if isinstance(news_route_map, dict):
            for raw_group, raw_profile in news_route_map.items():
                group = str(raw_group).strip()
                profile = str(raw_profile or "").strip().lower()
                if group and profile in valid_route_profiles:
                    self.news_route_map[group] = profile
                elif group:
                    logging.warning(
                        "%s GROUP_ROUTE_INVALID group=%s profile=%s action=keep-validated-default",
                        self.peer,
                        group,
                        raw_profile,
                    )
        self.mail_domain = normalize_mail_domain(mail_domain)
        self.admin_email = normalize_email_address(admin_email, self.mail_domain) or f"admin@{self.mail_domain}"
        self.welcome_email_enabled = bool(welcome_email_enabled)
        self.welcome_email_from = normalize_email_address(welcome_email_from or self.admin_email, self.mail_domain) or self.admin_email
        self.welcome_email_subject = str(welcome_email_subject or DEFAULT_WELCOME_EMAIL_SUBJECT)
        if isinstance(welcome_email_body, str):
            self.welcome_email_body = welcome_email_body.splitlines()
        elif isinstance(welcome_email_body, list):
            self.welcome_email_body = [str(x) for x in welcome_email_body]
        else:
            self.welcome_email_body = DEFAULT_WELCOME_EMAIL_BODY[:]
        self.mail_store = MailStore(mail_store_dir, mail_inbox_path, mail_domain=self.mail_domain, admin_email=self.admin_email)
        self.pending_mailbox_clear_email: Optional[str] = None
        self.pending_mailbox_clear_count = 0
        self.password_change_store = PasswordChangeStore(password_change_store_dir)
        self.password_change_apply = bool(password_change_apply)
        self.password_change_probe_active = (str(content_profile or "").lower() == "troca-senha")
        self.password_change_packet_count = 0
        self.outros_group_lines = DEFAULT_OUTROS_GROUP[:]
        self.mail_group_lines = DEFAULT_MAIL_GROUP[:]
        self.unknown_token_action = unknown_token_action
        self.raw_hayes_in_data_action = raw_hayes_in_data_action
        self.raw_hayes_close_delay_seconds = raw_hayes_close_delay_seconds
        self.raw_hayes_in_data_seen = False
        # rolling probe for Hayes escape/hangup bytes that arrive
        # fragmented before the MegaNet packet phase.  The physical cartridge
        # may send +++ and ATH0 one byte at a time after a failed 1200/2400-bps
        # negotiation.  Without this buffer, ATH0 is misread as login/password
        # or appended to the profile field, leaving TCP/SIP sessions open.
        self.pre_auth_hayes_probe = bytearray()
        # diagnostics for MAIL / Correio Eletronico upload path.
        # When the ROM sends #get mail#, the server answers #ready# and then
        # captures the following client-side #begin mail# ... #end mail# flow.
        self.mail_upload_active = False
        self.mail_upload_chunks: list[str] = []
        self.mail_upload_raw_chunks: list[bytes] = []
        self.mail_upload_count = 0

        self.state: MegaNetState = MegaNetState.LOGIN_PROMPT if online_start else MegaNetState.HAYES
        self.auto_ready_seen = bool(auto_ready_seen)
        self.hayes_buf = bytearray()
        self.line_buf = bytearray()
        self.bootstrap_login = ""
        self.bootstrap_password = ""
        self.profile_buf = bytearray()
        self.returning_user_key: Optional[str] = None
        self._meganet_access_recorded = False
        self.auth_result: Optional[AuthResult] = None  # PPP/profile-layer validation result.
        self.meganet_auth_result: Optional[AuthResult] = None  # latest Mega Net validation result.
        self.meganet_id_auth_seen = False
        self.meganet_id_user = ""
        self.meganet_id_password = ""
        self.meganet_id_service = ""
        self.cadastro_db_blocked = False
        self.profile_idle_task: Optional[asyncio.Task] = None
        self.post_dle_task: Optional[asyncio.Task] = None
        self.frame_parser = ClientFrameParser()
        self.cadastro = CadastroState()
        # explicit marker that this session entered the cadastro upload flow.
        # New users are allowed only in this context; normal #id login remains
        # strictly field-only against fields.nome_meganet.
        self.cadastro_session_seen = False
        self.server_seq = 0
        self.connected = online_start
        self.numeric_v0 = False
        self.cadastro_db_saved = False
        self.session_id = _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        # BizHawk TCP opens the socket before the MegaNet ROM is in the
        # data phase. If login: is sent immediately on accept, the virtual modem
        # can discard it while still processing Hayes commands. Keep a retry loop
        # and also treat the first empty CR as the explicit data-phase wake-up.
        self.login_prompt_retry_task: Optional[asyncio.Task] = None
        self.login_prompt_retry_interval_seconds = 0.35
        # Quando um prompt acabou de ser enviado explicitamente, dê tempo ao
        # cartucho físico para reconhecê-lo e começar a responder antes de
        # injetar outro login:. O intervalo entre retries continua curto.
        self.login_prompt_retry_after_prompt_seconds = 0.90
        self.login_prompt_retry_max_count = 40

        # fail-closed protection for corrupted modem sessions.
        # The validated MegaNet bootstrap is textual ASCII until the DLE
        # handshake. High-bit bytes or unexpected controls mean that the modem
        # carrier/data path is corrupted and must not be acknowledged.
        self.bootstrap_guard_enabled = True
        self.bootstrap_terminate_requested = False
        self.bootstrap_watchdog_task: Optional[asyncio.Task] = None
        self.bootstrap_state_timeouts: dict[MegaNetState, float] = {
            MegaNetState.WAIT_LOGIN: 30.0,
            MegaNetState.WAIT_PASSWORD: 20.0,
            MegaNetState.WAIT_PROFILE_FIELD: 10.0,
            MegaNetState.WAIT_DLE: 10.0,
            MegaNetState.WAIT_POST_DLE: 5.0,
        }
        self.bootstrap_max_field_bytes: dict[MegaNetState, int] = {
            MegaNetState.WAIT_LOGIN: 64,
            MegaNetState.WAIT_PASSWORD: 64,
            MegaNetState.WAIT_PROFILE_FIELD: 64,
        }

        # an isolated decoded byte with the high bit set can occur when
        # the V.22 audio crosses a carrier SIP trunk.  Do not treat one such
        # byte as proof that the entire session is corrupt.  Invalid bytes are
        # discarded and counted in a short sliding window; persistent noise
        # still closes the TCP before it can be consumed as credentials.
        self.meganet_bootstrap_noise_tolerance = max(
            0,
            min(32, int(meganet_bootstrap_noise_tolerance or 0)),
        )
        self.meganet_bootstrap_noise_window_seconds = max(
            0.10,
            min(30.0, float(meganet_bootstrap_noise_window_seconds or 2.0)),
        )
        self.bootstrap_noise_count = 0
        self.bootstrap_noise_total = 0
        self.bootstrap_noise_window_started = 0.0
        self.bootstrap_noise_state: MegaNetState = self.state

    async def start(self) -> None:
        logging.info("%s session start state=%s", self.peer, self.state)
        if self.state == MegaNetState.LOGIN_PROMPT:
            self.state = MegaNetState.WAIT_LOGIN
            if self.auto_ready_seen:
                # MGR1 is emitted only after app_meganet has confirmed
                # the modem data phase. Unlike a generic early TCP open, there
                # is no reason to delay the first prompt here. This removes the
                # extra 350 ms that could make the physical ROM enter Retry LOGIN.
                logging.info(
                    "%s MEGANET_AUTO_READY_LOGIN_PROMPT action=send-immediate first_retry_delay=%.2fs",
                    self.peer, self.login_prompt_retry_after_prompt_seconds,
                )
                await self.send_raw(b"login:\r", "login prompt immediately after MGR1/data ready")
                self.arm_login_prompt_repeater(
                    "login prompt retry after MGR1/data ready",
                    initial_delay_seconds=self.login_prompt_retry_after_prompt_seconds,
                )
            else:
                # Generic online-start may still be an early TCP open. Preserve
                # the conservative behavior when no explicit MGR1 exists.
                self.arm_login_prompt_repeater(
                    "login prompt retry after TCP open/data-phase wait",
                    initial_delay_seconds=self.login_prompt_retry_interval_seconds,
                )
            self.arm_bootstrap_watchdog()

    async def feed(self, data: bytes) -> None:
        if not data:
            return
        logging.debug("%s RX %s", self.peer, data.hex(" "))

        # a failed physical-modem connection can return the cartridge to
        # its menu while +++/ATH0 still crosses the established TCP bridge.
        # Probe these bytes before login/profile handlers consume them.  The
        # rolling buffer is required because app_softmodem commonly delivers
        # one decoded character per recv()/feed() call.
        if self.raw_hayes_in_data_action != "ignore":
            pre_auth_hayes = self._feed_pre_auth_hayes_probe(data)
            if pre_auth_hayes:
                self.cancel_login_prompt_repeater()
                if self.profile_idle_task and not self.profile_idle_task.done():
                    self.profile_idle_task.cancel()
                if self.post_dle_task and not self.post_dle_task.done():
                    self.post_dle_task.cancel()
                self.line_buf.clear()
                self.profile_buf.clear()
                logging.info(
                    "%s RAW_HAYES_PRE_AUTH command=%s state=%s action=%s",
                    self.peer,
                    pre_auth_hayes,
                    self.state,
                    self.raw_hayes_in_data_action,
                )
                await self._handle_raw_hayes_in_data(pre_auth_hayes)
                return

        if self.bootstrap_guard_enabled:
            filtered_data = await self.filter_bootstrap_noise(data)
            if filtered_data is None:
                return
            if not filtered_data:
                return
            data = filtered_data

        if self.state == MegaNetState.HAYES:
            await self.feed_hayes(data)
        elif self.state in {MegaNetState.WAIT_LOGIN, MegaNetState.WAIT_PASSWORD}:
            await self.feed_login_lines(data)
        elif self.state == MegaNetState.WAIT_PROFILE_FIELD:
            await self.feed_profile_field(data)
        elif self.state == MegaNetState.WAIT_DLE:
            await self.feed_wait_dle(data)
        elif self.state == MegaNetState.WAIT_POST_DLE:
            await self.feed_post_dle(data)
        elif self.state == MegaNetState.AUTH_REJECTED:
            logging.info("%s RX ignored after auth rejection: %s", self.peer, data.hex(" "))
        else:
            await self.feed_packets(data)

    def arm_bootstrap_watchdog(self) -> None:
        """Arm a state-specific timeout while the textual bootstrap is active."""
        if not self.bootstrap_guard_enabled:
            return
        timeout = float(self.bootstrap_state_timeouts.get(self.state, 0.0) or 0.0)
        if timeout <= 0:
            self.cancel_bootstrap_watchdog()
            return
        if self.bootstrap_watchdog_task and not self.bootstrap_watchdog_task.done():
            self.bootstrap_watchdog_task.cancel()
        watched_state = self.state
        self.bootstrap_watchdog_task = asyncio.create_task(
            self._bootstrap_watchdog(watched_state, timeout)
        )

    def cancel_bootstrap_watchdog(self) -> None:
        if self.bootstrap_watchdog_task and not self.bootstrap_watchdog_task.done():
            self.bootstrap_watchdog_task.cancel()
        self.bootstrap_watchdog_task = None

    async def _bootstrap_watchdog(self, watched_state: MegaNetState, timeout: float) -> None:
        try:
            await asyncio.sleep(timeout)
            if self.bootstrap_terminate_requested:
                return
            if self.state != watched_state:
                return
            if self.writer.is_closing():
                return
            await self.abort_corrupt_bootstrap(
                reason=f"bootstrap state timeout after {timeout:.1f}s",
                data=b"",
                offset=-1,
                bad_byte=None,
            )
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logging.debug("%s BOOTSTRAP_WATCHDOG stopped error=%s", self.peer, exc)

    @staticmethod
    def _bootstrap_byte_allowed(state: MegaNetState, value: int) -> bool:
        """Return whether one byte is valid in the current bootstrap state."""
        if state not in {
            MegaNetState.HAYES,
            MegaNetState.LOGIN_PROMPT,
            MegaNetState.WAIT_LOGIN,
            MegaNetState.WAIT_PASSWORD,
            MegaNetState.WAIT_PROFILE_FIELD,
            MegaNetState.WAIT_DLE,
            MegaNetState.WAIT_POST_DLE,
        }:
            return True

        if state in {
            MegaNetState.HAYES,
            MegaNetState.LOGIN_PROMPT,
            MegaNetState.WAIT_LOGIN,
            MegaNetState.WAIT_PASSWORD,
            MegaNetState.WAIT_PROFILE_FIELD,
        }:
            return value in (CR, LF, 0x09) or 0x20 <= value <= 0x7E

        if state == MegaNetState.WAIT_DLE:
            return value in {DLE, CR, LF, ACK, NAK, 0x00}

        return value in {DLE, ACK, NAK, 0x00, CR, LF}

    def _find_bootstrap_corruption(
        self,
        data: bytes,
    ) -> Optional[tuple[int, int, str]]:
        """Compatibility helper returning the first invalid bootstrap byte."""
        for offset, value in enumerate(data):
            if not self._bootstrap_byte_allowed(self.state, value):
                if self.state in {
                    MegaNetState.HAYES,
                    MegaNetState.LOGIN_PROMPT,
                    MegaNetState.WAIT_LOGIN,
                    MegaNetState.WAIT_PASSWORD,
                    MegaNetState.WAIT_PROFILE_FIELD,
                }:
                    reason = "non-ASCII byte during textual bootstrap"
                else:
                    reason = f"unexpected control/data byte in {self.state}"
                return offset, value, reason
        return None

    def reset_bootstrap_noise(self, *, state: Optional[MegaNetState] = None) -> None:
        self.bootstrap_noise_count = 0
        self.bootstrap_noise_window_started = 0.0
        self.bootstrap_noise_state = state or self.state

    async def filter_bootstrap_noise(self, data: bytes) -> Optional[bytes]:
        """Discard isolated invalid bytes and close on persistent corruption.

        Returns:
          - filtered bytes when the session may continue;
          - b"" when the whole chunk was harmless isolated noise;
          - None after the TCP close was requested.
        """
        state = self.state
        if state not in {
            MegaNetState.HAYES,
            MegaNetState.LOGIN_PROMPT,
            MegaNetState.WAIT_LOGIN,
            MegaNetState.WAIT_PASSWORD,
            MegaNetState.WAIT_PROFILE_FIELD,
            MegaNetState.WAIT_DLE,
            MegaNetState.WAIT_POST_DLE,
        }:
            return data

        invalid: list[tuple[int, int]] = []
        clean = bytearray()

        for offset, value in enumerate(data):
            if self._bootstrap_byte_allowed(state, value):
                clean.append(value)
            else:
                invalid.append((offset, value))

        if not invalid:
            # A state change represents protocol progress and starts a new
            # independent noise budget.  Expired windows are reset lazily.
            if self.bootstrap_noise_state != state:
                self.reset_bootstrap_noise(state=state)
            elif self.bootstrap_noise_window_started:
                now = asyncio.get_running_loop().time()
                if now - self.bootstrap_noise_window_started > self.meganet_bootstrap_noise_window_seconds:
                    self.reset_bootstrap_noise(state=state)
            return data

        loop = asyncio.get_running_loop()
        now = loop.time()

        if (
            self.bootstrap_noise_state != state
            or not self.bootstrap_noise_window_started
            or now - self.bootstrap_noise_window_started
            > self.meganet_bootstrap_noise_window_seconds
        ):
            self.bootstrap_noise_state = state
            self.bootstrap_noise_count = 0
            self.bootstrap_noise_window_started = now

        for offset, value in invalid:
            self.bootstrap_noise_count += 1
            self.bootstrap_noise_total += 1

            if self.bootstrap_noise_count <= self.meganet_bootstrap_noise_tolerance:
                logging.warning(
                    "%s MEGANET_BOOTSTRAP_NOISE state=%s offset=%d byte=%02X "
                    "count=%d/%d window=%.3fs data=%s action=discard-continue",
                    self.peer,
                    state,
                    offset,
                    value,
                    self.bootstrap_noise_count,
                    self.meganet_bootstrap_noise_tolerance,
                    self.meganet_bootstrap_noise_window_seconds,
                    data[:128].hex(" ") or "-",
                )
                continue

            await self.abort_corrupt_bootstrap(
                reason=(
                    "bootstrap noise threshold exceeded "
                    f"({self.bootstrap_noise_count}>"
                    f"{self.meganet_bootstrap_noise_tolerance} in "
                    f"{self.meganet_bootstrap_noise_window_seconds:.3f}s)"
                ),
                data=data,
                offset=offset,
                bad_byte=value,
            )
            return None

        if clean:
            logging.info(
                "%s MEGANET_BOOTSTRAP_NOISE_FILTERED state=%s "
                "input_len=%d output_len=%d discarded=%d total_discarded=%d",
                self.peer,
                state,
                len(data),
                len(clean),
                len(invalid),
                self.bootstrap_noise_total,
            )

        return bytes(clean)


    async def abort_corrupt_bootstrap(
        self,
        *,
        reason: str,
        data: bytes,
        offset: int,
        bad_byte: Optional[int],
    ) -> None:
        """Stop a corrupted bootstrap without sending further protocol data."""
        if self.bootstrap_terminate_requested:
            return
        self.bootstrap_terminate_requested = True
        self.closed_by_transaction = True
        self.cancel_login_prompt_repeater()
        self.cancel_bootstrap_watchdog()

        if self.profile_idle_task and not self.profile_idle_task.done():
            self.profile_idle_task.cancel()
        if self.post_dle_task and not self.post_dle_task.done():
            self.post_dle_task.cancel()
        if self.close_task and not self.close_task.done():
            self.close_task.cancel()

        self.line_buf.clear()
        self.profile_buf.clear()
        self.pre_auth_hayes_probe.clear()
        self.state = MegaNetState.AUTH_REJECTED

        byte_text = f"{bad_byte:02X}" if bad_byte is not None else "-"
        logging.warning(
            "%s MEGANET_BOOTSTRAP_CORRUPTION reason=%s offset=%d byte=%s "
            "data=%s action=force-close-no-response",
            self.peer,
            reason,
            offset,
            byte_text,
            data[:128].hex(" ") or "-",
        )
        try:
            self.writer.close()
        except Exception as exc:
            logging.debug("%s MEGANET_BOOTSTRAP_CORRUPTION close error=%s", self.peer, exc)

    async def feed_hayes(self, data: bytes) -> None:
        for b in data:
            self.hayes_buf.append(b)
            if b == CR:
                cmd = ascii_text(bytes(self.hayes_buf)).strip()
                self.hayes_buf.clear()
                if not cmd:
                    # with BizHawk's local modem emulation, the TCP side
                    # may receive only the final CR after ATD, not the AT command
                    # itself. Interpret this empty CR as the start of the online
                    # MegaNet data phase and present the textual login prompt.
                    logging.info("%s EMPTY_HAYES_CR_AS_MEGANET_DATA_PHASE action=login-prompt", self.peer)
                    self.connected = True
                    self.state = MegaNetState.WAIT_LOGIN
                    await self.send_raw(b"login:\r", "login prompt after empty Hayes CR/data phase")
                    self.arm_login_prompt_repeater(
                        "login prompt retry after empty Hayes CR/data phase",
                        initial_delay_seconds=self.login_prompt_retry_after_prompt_seconds,
                    )
                    self.arm_bootstrap_watchdog()
                    continue
                await self.handle_at_command(cmd)

    async def handle_at_command(self, cmd: str) -> None:
        if not cmd:
            return
        upper = cmd.upper()
        logging.info("%s AT command: %s", self.peer, cmd)

        if "V0" in upper:
            self.numeric_v0 = True

        if upper == "AT&F":
            # The ROM uses AT&F as a no-wait reset in one path. Suppress by default.
            return

        if upper.startswith("+++"):
            return

        if upper.startswith("ATH"):
            await self.send_raw(b"0\r" if self.numeric_v0 else b"0\r\n", "ATH OK")
            return

        if self.is_dial_command(upper):
            self.connected = True
            await self.send_raw(b"1\r" if self.numeric_v0 else b"CONNECT\r\n", "CONNECT")
            await self.send_raw(b"login:\r", "login prompt after CONNECT")
            self.state = MegaNetState.WAIT_LOGIN
            self.arm_login_prompt_repeater(
                "login prompt retry after CONNECT",
                initial_delay_seconds=self.login_prompt_retry_after_prompt_seconds,
            )
            self.arm_bootstrap_watchdog()
            return

        await self.send_raw(b"0\r" if self.numeric_v0 else b"0\r\n", "generic OK")

    @staticmethod
    def is_dial_command(upper: str) -> bool:
        # Observed: ATX3DP,123456789. Also accept ATD/ATDT/ATDP variants.
        return bool(re.match(r"^AT.*D[TPP,]", upper)) or upper.startswith("ATD")

    def arm_login_prompt_repeater(
        self,
        reason: str,
        *,
        initial_delay_seconds: Optional[float] = None,
    ) -> None:
        if self.login_prompt_retry_task and not self.login_prompt_retry_task.done():
            return
        self.login_prompt_retry_task = asyncio.create_task(
            self.login_prompt_repeater(reason, initial_delay_seconds=initial_delay_seconds)
        )

    def cancel_login_prompt_repeater(self) -> None:
        if self.login_prompt_retry_task and not self.login_prompt_retry_task.done():
            self.login_prompt_retry_task.cancel()
        self.login_prompt_retry_task = None

    async def login_prompt_repeater(
        self,
        reason: str,
        *,
        initial_delay_seconds: Optional[float] = None,
    ) -> None:
        try:
            first_delay = (
                self.login_prompt_retry_interval_seconds
                if initial_delay_seconds is None
                else max(0.05, float(initial_delay_seconds))
            )
            for attempt in range(1, self.login_prompt_retry_max_count + 1):
                await asyncio.sleep(
                    first_delay if attempt == 1 else self.login_prompt_retry_interval_seconds
                )
                if self.state != MegaNetState.WAIT_LOGIN:
                    return
                if self.writer.is_closing():
                    return
                if self.line_buf:
                    # Do not inject a prompt while the cartridge is already
                    # typing the username. Wait for the CR handler to finish.
                    continue
                logging.info(
                    "%s LOGIN_PROMPT_RETRY attempt=%d reason=%s",
                    self.peer, attempt, reason,
                )
                await self.send_raw(b"login:\r", f"{reason} attempt {attempt}")
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logging.debug("%s login prompt retry stopped: %s", self.peer, exc)

    async def feed_login_lines(self, data: bytes) -> None:
        for b in data:
            self.line_buf.append(b)
            max_len = int(self.bootstrap_max_field_bytes.get(self.state, 64))
            if len(self.line_buf) > max_len + 2:
                await self.abort_corrupt_bootstrap(
                    reason=f"{self.state} field exceeds {max_len} bytes",
                    data=bytes(self.line_buf[:128]),
                    offset=len(self.line_buf) - 1,
                    bad_byte=b,
                )
                return
            if b == CR:
                line = bytes(self.line_buf).rstrip(b"\r\n")
                self.line_buf.clear()
                text = ascii_text(line)
                normalized = text.strip().upper()
                log_text = "<redacted>" if self.state == MegaNetState.WAIT_PASSWORD else text
                logging.info("%s CLIENT_LINE %s", self.peer, log_text)

                # +++ is the modem escape guard sequence, not a MegaNet
                # username/password.  Ignore it while waiting for the following
                # ATH/ATH0, which the rolling pre-auth probe will close cleanly.
                if normalized == "+++" and self.raw_hayes_in_data_action != "ignore":
                    logging.info(
                        "%s RAW_HAYES_ESCAPE_PRE_AUTH state=%s action=ignore-wait-ath0",
                        self.peer,
                        self.state,
                    )
                    continue

                # Defensive fallback for calls where ATH/ATH0 arrives as one
                # complete line and the rolling probe was disabled/reset.
                if normalized in {"ATH", "ATH0"} and self.raw_hayes_in_data_action != "ignore":
                    self.cancel_login_prompt_repeater()
                    self.pre_auth_hayes_probe.clear()
                    await self._handle_raw_hayes_in_data("ATH0")
                    return

                if self.state == MegaNetState.WAIT_LOGIN:
                    if not text.strip():
                        logging.info("%s EMPTY_LOGIN_CR_IGNORED action=repeat-login-prompt", self.peer)
                        await self.send_raw(b"login:\r", "login prompt repeated after empty CR")
                        self.arm_login_prompt_repeater(
                            "login prompt retry after empty CR",
                            initial_delay_seconds=self.login_prompt_retry_after_prompt_seconds,
                        )
                        continue
                    self.cancel_login_prompt_repeater()
                    self.pre_auth_hayes_probe.clear()
                    self.bootstrap_login = text
                    await self.send_raw(b"password:\r", "password prompt")
                    self.state = MegaNetState.WAIT_PASSWORD
                    self.arm_bootstrap_watchdog()
                elif self.state == MegaNetState.WAIT_PASSWORD:
                    if not text.strip():
                        logging.info("%s EMPTY_PASSWORD_CR_IGNORED action=repeat-password-prompt", self.peer)
                        await self.send_raw(b"password:\r", "password prompt repeated after empty CR")
                        continue
                    self.pre_auth_hayes_probe.clear()
                    self.bootstrap_password = text
                    self.state = MegaNetState.WAIT_PROFILE_FIELD
                    self.arm_bootstrap_watchdog()

    async def feed_profile_field(self, data: bytes) -> None:
        # The ROM sends the profile/name field without CR and then waits for ACK.
        for b in data:
            if b == CR:
                # tolerate CR if a future ROM path sends one
                continue
            self.profile_buf.append(b)
            max_len = int(self.bootstrap_max_field_bytes.get(MegaNetState.WAIT_PROFILE_FIELD, 64))
            if len(self.profile_buf) > max_len:
                await self.abort_corrupt_bootstrap(
                    reason=f"WAIT_PROFILE_FIELD exceeds {max_len} bytes",
                    data=bytes(self.profile_buf[:128]),
                    offset=len(self.profile_buf) - 1,
                    bad_byte=b,
                )
                return
        if self.profile_idle_task:
            self.profile_idle_task.cancel()
        self.profile_idle_task = asyncio.create_task(self.profile_idle_ack())

    async def profile_idle_ack(self) -> None:
        try:
            await asyncio.sleep(self.profile_idle_seconds)
            if self.state != MegaNetState.WAIT_PROFILE_FIELD or not self.profile_buf:
                return
            profile = bytes(self.profile_buf)
            profile_text = ascii_text(profile)
            logging.info("%s CLIENT_FIELD_NO_CR profile=%s", self.peer, profile_text)
            self.auth_result = self.cadastro_db.validate_user(
                profile_name=profile_text,
                bootstrap_login=self.bootstrap_login,
                bootstrap_password=self.bootstrap_password,
                auth_mode=self.auth_mode,
                auth_password_source=self.auth_password_source,
                auth_check_bootstrap_login=self.auth_check_bootstrap_login,
            )
            if self.auth_result.known_user:
                self.returning_user_key = self.auth_result.user_key
                entry = self.auth_result.entry or {}
                logging.info(
                    "%s RETURNING_USER key=%s total_sessions=%s last_session=%s auth=%s",
                    self.peer,
                    self.auth_result.user_key,
                    entry.get("total_sessions"),
                    entry.get("last_session_id"),
                    self.auth_result.reason,
                )
            else:
                logging.info("%s NEW_OR_UNKNOWN_USER profile=%s auth=%s", self.peer, profile_text, self.auth_result.reason)

            if not self.auth_result.allowed:
                await self.reject_auth(profile_text, self.auth_result.reason)
                return

            self.pre_auth_hayes_probe.clear()
            await self.send_raw(bytes([ACK]), "ACK after profile field/auth ok")
            self.state = MegaNetState.WAIT_DLE
            self.arm_bootstrap_watchdog()
        except asyncio.CancelledError:
            return

    async def reject_auth(self, profile_text: str, reason: str) -> None:
        logging.warning(
            "%s AUTH_REJECT profile=%s mode=%s password_source=%s reason=%s action=%s",
            self.peer, profile_text, self.auth_mode, self.auth_password_source, reason, self.auth_reject_action,
        )
        self.state = MegaNetState.AUTH_REJECTED
        if self.auth_reject_action == "nak-close":
            await self.send_raw(bytes([NAK]), "NAK auth rejected")
        elif self.auth_reject_action == "no-carrier-close":
            await self.send_raw(b"\r\nNO CARRIER\r\n", "NO CARRIER auth rejected")
        elif self.auth_reject_action == "silent-close":
            pass
        else:
            await self.send_raw(bytes([NAK]), "NAK auth rejected unknown action fallback")
        try:
            self.writer.close()
        except Exception:
            pass

    async def feed_wait_dle(self, data: bytes) -> None:
        # Important timing detail:
        # The ROM is still inside FUN_00006DEA immediately after sending DLE.
        # If we send packetized #bemvindo# in the same burst as the DLE ACK,
        # the ROM can consume packet bytes while still in the binary handshake
        # and respond with repeated DLE/NAK.  Send only ACK now, then defer the
        # welcome packets until the ROM has had time to return to FUN_000088EA.
        for b in data:
            if b == DLE:
                logging.info("%s CLIENT_CTRL DLE 0x10", self.peer)
                await self.send_raw(bytes([ACK]), "ACK after DLE")
                self.state = MegaNetState.WAIT_POST_DLE
                self.arm_bootstrap_watchdog()
                if self.post_dle_task:
                    self.post_dle_task.cancel()
                self.post_dle_task = asyncio.create_task(self.send_welcome_after_dle_delay())
                return

    async def feed_post_dle(self, data: bytes) -> None:
        # Ignore duplicate DLE bytes while the delayed welcome is pending.
        # Do NOT ACK duplicates, because an extra ACK immediately before the
        # packet layer corrupts the next token comparison.
        for b in data:
            if b == DLE:
                logging.info("%s CLIENT_CTRL duplicate DLE ignored while waiting to send welcome", self.peer)
            elif b in (ACK, NAK, 0x00):
                logging.info("%s CLIENT_CTRL %02X ignored while waiting to send welcome", self.peer, b)
            else:
                logging.info("%s unexpected byte while waiting post-DLE: %02X", self.peer, b)

    async def send_welcome_after_dle_delay(self) -> None:
        try:
            await asyncio.sleep(self.post_dle_delay_seconds)
            if self.state != MegaNetState.WAIT_POST_DLE:
                return

            # DLE/ACK completes the bootstrap.  The reliable welcome
            # packets may legitimately spend several seconds in ACK/NAK retry.
            # Leaving the WAIT_POST_DLE watchdog armed here can close an already
            # valid session while send_packet() is still waiting for an ACK.
            # From this point onward delivery failure belongs to the packet ARQ,
            # not to the textual/bootstrap corruption guard.
            self.cancel_bootstrap_watchdog()
            self.bootstrap_guard_enabled = False
            self.reset_bootstrap_noise(state=MegaNetState.PACKET)
            logging.info(
                "%s MEGANET_BOOTSTRAP_COMPLETE state=%s "
                "action=cancel-watchdog-enter-reliable-packet-transition",
                self.peer, self.state,
            )

            await self.send_packet(b"#bemvindo#", "welcome token after DLE delay")
            await self.send_packet(b"#ready#\r", "ready control after welcome")
            self.state = MegaNetState.PACKET
        except asyncio.CancelledError:
            return
        except MegaNetPacketDeliveryError as exc:
            logging.error("%s WELCOME_PACKET_DELIVERY_FAILED error=%s", self.peer, exc)
        except (ConnectionError, OSError) as exc:
            logging.info("%s welcome transmission stopped: %s", self.peer, exc)


    def _feed_pre_auth_hayes_probe(self, data: bytes) -> Optional[str]:
        """Detect fragmented ATH/ATH0 during the pre-packet MegaNet bootstrap.

        The app_softmodem bridge may deliver one decoded byte at a time.  A
        search restricted to the current chunk therefore misses ATH0\\r and
        allows the sequence to be consumed as login/password/profile data.

        Only ATH/ATH0 closes the connection.  The preceding +++ escape sequence
        is retained as context but is not itself enough to terminate a session.
        """
        probe_states = {
            MegaNetState.LOGIN_PROMPT,
            MegaNetState.WAIT_LOGIN,
            MegaNetState.WAIT_PASSWORD,
            MegaNetState.WAIT_PROFILE_FIELD,
            MegaNetState.WAIT_DLE,
            MegaNetState.WAIT_POST_DLE,
        }

        if self.state not in probe_states:
            self.pre_auth_hayes_probe.clear()
            return None

        if not data:
            return None

        self.pre_auth_hayes_probe.extend(data.upper())

        # Keep only a small rolling suffix.  This is long enough for repeated
        # +++\\rATH0\\r sequences and prevents unbounded growth under line noise.
        if len(self.pre_auth_hayes_probe) > 128:
            del self.pre_auth_hayes_probe[:-128]

        buffered = bytes(self.pre_auth_hayes_probe)
        if (
            b"ATH0\r" in buffered
            or b"ATH\r" in buffered
            or b"ATH0\n" in buffered
            or b"ATH\n" in buffered
        ):
            self.pre_auth_hayes_probe.clear()
            return "ATH0"

        return None

    def _find_raw_hayes_in_data(self, data: bytes) -> Optional[str]:
        """Detect modem escape/hangup commands that leaked through the TCP bridge.

        With the current bridge, after the ROM decides to hang up it may send
        ATH0\r and +++\r through the TCP socket. A real modem would process
        these locally. For mapping, the server can treat them as a hangup
        signal so the Python log stays clean and the ROM receives a minimal OK.
        """
        upper = data.upper()
        if b"ATH0\r" in upper or b"ATH\r" in upper:
            return "ATH0"
        if b"+++\r" in upper or b"+++" in upper:
            return "+++"
        return None

    async def _handle_raw_hayes_in_data(self, command: str) -> None:
        if self.raw_hayes_in_data_seen:
            return
        self.raw_hayes_in_data_seen = True
        self.cancel_bootstrap_watchdog()
        logging.info(
            "%s RAW_HAYES_IN_DATA command=%s action=%s close_delay=%.3f",
            self.peer,
            command,
            self.raw_hayes_in_data_action,
            self.raw_hayes_close_delay_seconds,
        )
        if self.raw_hayes_in_data_action == "ok-close":
            # ROM token probe: always respond with numeric Hayes OK (V0) and force-close
            # independently of --transaction-close. In this path used
            # close_transaction(), so --transaction-close off prevented the close.
            await self.send_raw(b"0\r", "raw Hayes hangup leaked through TCP: modem numeric OK forced")
            if self.raw_hayes_close_delay_seconds > 0:
                await asyncio.sleep(self.raw_hayes_close_delay_seconds)
            self.closed_by_transaction = True
            if self.close_task:
                self.close_task.cancel()
            logging.info("%s RAW_HAYES_FORCE_CLOSE scheduled delay=0.000s reason=raw Hayes hangup leaked through TCP: %s", self.peer, command)
            self.close_task = asyncio.create_task(self._force_close_later(0.0, f"raw Hayes hangup leaked through TCP: {command}"))
        elif self.raw_hayes_in_data_action == "close":
            if self.raw_hayes_close_delay_seconds > 0:
                await asyncio.sleep(self.raw_hayes_close_delay_seconds)
            self.closed_by_transaction = True
            if self.close_task:
                self.close_task.cancel()
            logging.info("%s RAW_HAYES_FORCE_CLOSE scheduled delay=0.000s reason=raw Hayes hangup leaked through TCP: %s", self.peer, command)
            self.close_task = asyncio.create_task(self._force_close_later(0.0, f"raw Hayes hangup leaked through TCP: {command}"))
        # ignore leaves the bytes for normal logging/parser behavior.

    def record_password_change_event(self, *, kind: str, payload: bytes = b"", token: bytes = b"", user: str = "", note: str = "", extra: Optional[dict] = None) -> None:
        if not self.password_change_probe_active:
            return
        self.password_change_packet_count += 1
        entry = {
            "timestamp": _dt.datetime.now().isoformat(timespec="seconds"),
            "session_id": self.session_id,
            "peer": self.peer,
            "kind": kind,
            "user": user or self.meganet_id_user or self.returning_user_key or "",
            "token": ascii_text(token or payload).strip(),
            "payload_text": ascii_text(payload).replace("\r", " | "),
            "payload_hex": payload.hex(" "),
            "note": note,
        }
        if extra:
            entry.update(extra)
        path = self.password_change_store.append(entry)
        logging.info(
            "%s PASSWORD_CHANGE_EVENT kind=%s user=%s note=%s saved=%s payload=%s",
            self.peer, kind, entry.get("user"), note, path, display_payload(payload) if payload else "-",
        )

    async def handle_password_change_token(self, nome_meganet: str, old_password: str, new_password: str, payload: bytes) -> None:
        """Handle real ROM token: #senha <user> <old> <new>#.

        The probe showed that TROCA DE SENHA does not use #id for the
        password change.  It sends a dedicated #senha packet, then expects a
        visible message/ready sequence and finishes with #bye#.
        """
        found = self.cadastro_db.find_user_by_meganet_name(nome_meganet)
        known_user = bool(found)
        stored_senha = None
        old_ok = False
        updated = None
        reason = "unknown_user"
        if found:
            entry = found[1]
            fields = entry.get("fields", {}) if isinstance(entry, dict) else {}
            stored_senha = fields.get("senha") if isinstance(fields, dict) else None
            old_ok = self.cadastro_db._same_meganet_secret(old_password, stored_senha, self.meganet_password_case)
            reason = "old_password_ok" if old_ok else "old_password_mismatch"

        logging.info(
            "%s PASSWORD_CHANGE_TOKEN nome_meganet=%s known=%s old_ok=%s apply=%s old=%s new=%s reason=%s",
            self.peer, nome_meganet, known_user, old_ok, self.password_change_apply, old_password, new_password, reason,
        )
        self.record_password_change_event(
            kind="senha_token",
            payload=payload,
            token=payload.rstrip(b"\r"),
            user=nome_meganet,
            note="parsed #senha <user> <old_password> <new_password>",
            extra={
                "known_user": known_user,
                "old_password_ok": old_ok,
                "old_password_seen": old_password,
                "new_password_seen": new_password,
                "stored_password_before": stored_senha,
                "apply_enabled": self.password_change_apply,
                "reason": reason,
            },
        )

        if not known_user:
            await self.send_message_block(["USUARIO NAO ENCONTRADO", "SENHA NAO ALTERADA"], reason="password change rejected unknown user", final=False)
            return

        if not old_ok:
            await self.send_message_block(["SENHA ATUAL INVALIDA", "SENHA NAO ALTERADA"], reason="password change rejected old password mismatch", final=False)
            return

        if not self.password_change_apply:
            await self.send_message_block(["SENHA VALIDADA", "MODO TESTE", "NAO ALTERADA"], reason="password change validated but not applied", final=False)
            return

        updated = self.cadastro_db.update_meganet_password(
            nome_meganet, new_password, session_id=self.session_id, peer=self.peer, source="password-change-senha-token"
        )
        logging.warning(
            "%s PASSWORD_CHANGE_APPLIED_BY_SENHA_TOKEN nome_meganet=%s old_senha=%s new_senha=%s updated=%s",
            self.peer, nome_meganet, old_password, new_password, updated,
        )
        self.record_password_change_event(
            kind="password_applied",
            user=nome_meganet,
            note="cadastro DB updated from #senha token because --password-change-apply was enabled",
            extra={"updated": str(updated), "old_password": old_password, "new_password": new_password},
        )
        await self.send_message_block(["SENHA ALTERADA", "COM SUCESSO"], reason="password change applied", final=False)

    async def feed_packets(self, data: bytes) -> None:
        leaked_hayes = self._find_raw_hayes_in_data(data)
        if leaked_hayes and self.raw_hayes_in_data_action != "ignore":
            await self._handle_raw_hayes_in_data(leaked_hayes)
            return
        frames = self.frame_parser.feed(data)
        for frame in frames:
            self.cadastro.add_packet(frame)
            payload = frame.payload
            token = payload.rstrip(b"\r")
            if self.password_change_probe_active:
                self.record_password_change_event(kind="client_packet", payload=payload, token=token, note="raw packet captured during troca-senha probe")
            logging.info(
                "%s CLIENT_PACKET seq=%d len=%d valid=%s payload=%s",
                self.peer, frame.seq, len(payload), frame.valid, display_payload(payload),
            )
            if not frame.valid:
                await self.send_raw(bytes([NAK]), "NAK bad checksum")
                continue

            await self.send_raw(bytes([ACK]), "ACK client packet")

            password_change = parse_password_change_payload(token)
            if password_change:
                nome_meganet, old_password, new_password = password_change
                await self.handle_password_change_token(nome_meganet, old_password, new_password, payload)
                continue

            meganet_id = parse_meganet_id_payload(token)
            if meganet_id:
                nome_meganet, senha, service = meganet_id
                await self.handle_meganet_id_auth(nome_meganet, senha, service)
                continue

            token_lower = token.lower()
            if token_lower.startswith(b"#send news ") or token_lower.startswith(b"#send internet"):
                await self.send_news_minimal_response(token, reason="ROM requested group download")
            elif token_lower == b"#send mail#" or token_lower == b"#get mail#":
                await self.send_mail_minimal_response(token, reason="ROM requested mail transfer")
            elif token_lower == b"#begin mail#":
                self.mail_upload_active = True
                self.mail_upload_chunks = []
                self.mail_upload_raw_chunks = []
                logging.info("%s OUTGOING_MAIL_BEGIN seq=%d action=capture-client-mail-upload", self.peer, frame.seq)
            elif token_lower in (b"#sep#", b"#item#") and self.mail_upload_active:
                logging.info("%s OUTGOING_MAIL_SEPARATOR token=%s chunks=%d", self.peer, ascii_text(token), len(self.mail_upload_chunks))
            elif token_lower == b"#end mail#":
                if self.mail_upload_active:
                    self.mail_upload_count += 1
                    await self.persist_outgoing_mail_upload()
                    self.mail_upload_active = False
                    self.mail_upload_chunks = []
                    self.mail_upload_raw_chunks = []
                else:
                    logging.info("%s OUTGOING_MAIL_END without active upload", self.peer)
                await self.send_packet(b"#ready#\r", "ready after client #end mail# upload")
            elif token == b"#get cadastro#":
                self.cadastro_session_seen = True
                logging.info("%s CADASTRO_SESSION_BEGIN token=#get cadastro#", self.peer)
                await self.send_packet(b"#ready#\r", "ready after get cadastro")
            elif token == b"#begin cadastro#":
                self.cadastro_session_seen = True
                logging.info("%s CADASTRO_SESSION_BEGIN token=#begin cadastro#", self.peer)
                self.cadastro.in_cadastro = True
                self.cadastro.waiting_item_value = False
            elif token == b"#item#":
                self.cadastro.waiting_item_value = True
            elif token == b"#end cadastro#":
                self.cadastro.in_cadastro = False
                await self.handle_end_cadastro()
            elif token == b"#bye#":
                logging.info("%s #bye# received; saving cadastro and closing application session", self.peer)
                await self.save_cadastro_db("bye")
                try:
                    await self.save_outputs()
                except (OSError, UnicodeError, TypeError, ValueError) as exc:
                    # Session logging is optional and must never keep the ROM in
                    # an already-finished transaction when local persistence is
                    # unavailable. Cadastro persistence above remains strict.
                    logging.warning(
                        "%s SESSION_OUTPUT_SAVE_FAILED_AFTER_BYE error=%s action=close-anyway",
                        self.peer,
                        exc,
                    )
                await self.close_transaction("client #bye#", immediate=True)
            else:
                if self.mail_upload_active:
                    # keep both the display preview and the exact payload bytes.
                    # Delivery/parsing must use the bytes, otherwise To:/Subject:/body
                    # are lost and the message falls back to the admin mailbox.
                    captured = ascii_text(payload).replace("\r", " | ")
                    self.mail_upload_chunks.append(captured)
                    self.mail_upload_raw_chunks.append(bytes(payload))
                    logging.info(
                        "%s OUTGOING_MAIL_DATA seq=%d len=%d payload=%s hex=%s",
                        self.peer,
                        frame.seq,
                        len(payload),
                        display_payload(payload),
                        payload.hex(" "),
                    )
                    continue

                added = self.cadastro.maybe_add_field(payload)
                if added:
                    idx, value = added
                    field_name = FIELD_NAMES[idx - 1] if idx <= len(FIELD_NAMES) else f"campo_{idx:02d}"
                    logging.info("%s CADASTRO_FIELD %02d %s=%s", self.peer, idx, field_name, ascii_text(value))
                else:
                    logging.info(
                        "%s UNKNOWN_CLIENT_TOKEN seq=%d len=%d payload=%s hex=%s action=%s",
                        self.peer,
                        frame.seq,
                        len(payload),
                        display_payload(payload),
                        payload.hex(" "),
                        self.unknown_token_action,
                    )
                    if self.unknown_token_action == "ready":
                        await self.send_packet(b"#ready#\r", "ready after unknown client token for mapping")
                    elif self.unknown_token_action == "message-ready":
                        await self.send_message_block(
                            ["TOKEN RECEBIDO", ascii_text(payload).strip()],
                            reason="unknown client token mapper echo",
                            final=False,
                        )

    def is_cadastro_registration_context(self) -> bool:
        """Return True only when the ROM is inside the cadastro/new-user upload flow.

        Normal service login sends #id after the bootstrap/welcome and must be
        validated strictly against fields.nome_meganet.  New user creation is
        allowed only after the ROM has explicitly entered the cadastro protocol
        (#get cadastro#, #begin cadastro# or captured cadastro fields).
        """
        return bool(
            self.cadastro_session_seen
            or self.cadastro.in_cadastro
            or self.cadastro.fields
        )

    async def handle_meganet_id_auth(self, nome_meganet: str, senha: str, service: str) -> None:
        """Validate the actual #id packet sent by the ROM after welcome/ready."""
        self.meganet_id_auth_seen = True
        self.meganet_id_user = nome_meganet
        self.meganet_id_password = senha
        self.meganet_id_service = service
        registration_context = self.is_cadastro_registration_context()

        if self.password_change_probe_active:
            found_for_probe = self.cadastro_db.find_user_by_meganet_name(nome_meganet)
            stored_senha = None
            same_as_stored = False
            known_user = False
            if found_for_probe:
                known_user = True
                stored_fields = found_for_probe[1].get("fields", {}) if isinstance(found_for_probe[1], dict) else {}
                stored_senha = stored_fields.get("senha") if isinstance(stored_fields, dict) else None
                same_as_stored = self.cadastro_db._same_meganet_secret(senha, stored_senha, self.meganet_password_case)
            candidate_new_password = bool(known_user and stored_senha and not same_as_stored)
            note = "#id password matches cadastro DB" if same_as_stored else ("#id password differs from cadastro DB; possible new password or failed auth" if known_user else "#id user not found in cadastro DB")
            logging.info(
                "%s PASSWORD_CHANGE_ID_PROBE nome_meganet=%s known=%s same_as_stored=%s candidate_new_password=%s apply=%s",
                self.peer, nome_meganet, known_user, same_as_stored, candidate_new_password, self.password_change_apply,
            )
            self.record_password_change_event(
                kind="id_probe",
                payload=(f"#id {nome_meganet} {senha} {service}#\r").encode("ascii", "replace"),
                token=(f"#id {nome_meganet} {senha} {service}#").encode("ascii", "replace"),
                user=nome_meganet,
                note=note,
                extra={
                    "known_user": known_user,
                    "candidate_new_password": candidate_new_password,
                    "same_as_stored": same_as_stored,
                    "password_seen_in_id": senha,
                    "stored_password_before": stored_senha,
                },
            )
            if candidate_new_password and self.password_change_apply:
                updated = self.cadastro_db.update_meganet_password(
                    nome_meganet, senha, session_id=self.session_id, peer=self.peer, source="password-change-id-probe"
                )
                logging.warning(
                    "%s PASSWORD_CHANGE_APPLIED_BY_ID nome_meganet=%s new_senha=%s updated=%s",
                    self.peer, nome_meganet, senha, updated,
                )
                self.record_password_change_event(
                    kind="password_applied",
                    user=nome_meganet,
                    note="cadastro DB updated from #id candidate because --password-change-apply was enabled",
                    extra={"updated": str(updated), "new_password": senha},
                )

        # In the cadastro flow, the ROM uses #id <nome> <senha> SSO# as the
        # first server-side check for the chosen Mega Net name.  The manual says
        # that an already existing name must produce a visible "usuario XXX ja
        # existe" message, so duplicate-name detection takes precedence over
        # password validation while registration duplicate rejection is enabled.
        if registration_context and self.registration_duplicate_action == "reject-message":
            found = self.cadastro_db.find_user_by_meganet_name(nome_meganet)
            if found:
                self.meganet_auth_result = AuthResult(False, True, found[0], "registration_duplicate_nome_meganet", found[1])
                logging.info(
                    "%s MEGANET_ID_AUTH nome_meganet=%s service=%s known=True allowed=False mode=%s password_case=%s reason=registration_duplicate_nome_meganet",
                    self.peer,
                    nome_meganet,
                    service or "-",
                    self.meganet_auth_mode,
                    self.meganet_password_case,
                )
                self.cadastro_db_blocked = True
                await self.reject_duplicate_registration(nome_meganet, found[0])
                return

        # production login is multi-user and strict: it scans all users[*].fields.nome_meganet. New users are allowed when
        # and only when the ROM is inside the cadastro/new-user protocol.  This
        # separates two cases that both use #id <nome_meganet> <senha> SSO#:
        #   - normal service login: require the requested name to match some users[*].fields.nome_meganet;
        #   - cadastro flow: allow a new fields.nome_meganet, subject to duplicate checks.
        effective_meganet_auth_mode = self.meganet_auth_mode
        if self.meganet_auth_mode == "require-known":
            known_lookup = self.cadastro_db.find_user_by_meganet_name(nome_meganet)
            if not known_lookup and registration_context:
                effective_meganet_auth_mode = "allow-new"
                logging.info(
                    "%s MEGANET_ID_AUTH_CADASTRO_NEW nome_meganet=%s service=%s registration_context=True mode=require-known effective_mode=allow-new reason=new_nome_meganet_allowed_only_in_cadastro",
                    self.peer,
                    nome_meganet,
                    service or "-",
                )
            elif not known_lookup:
                self.meganet_auth_result = AuthResult(False, False, None, "unknown_nome_meganet_strict", None)
                logging.info(
                    "%s MEGANET_ID_AUTH nome_meganet=%s service=%s known=False allowed=False mode=%s password_case=%s registration_context=False reason=unknown_nome_meganet_strict",
                    self.peer,
                    nome_meganet,
                    service or "-",
                    self.meganet_auth_mode,
                    self.meganet_password_case,
                )
                self.cadastro_db_blocked = True
                await self.reject_meganet_auth({"nome_meganet": nome_meganet}, self.meganet_auth_result.reason)
                return

        self.meganet_auth_result = self.cadastro_db.validate_meganet_id_credentials(
            nome_meganet=nome_meganet,
            senha=senha,
            auth_mode=effective_meganet_auth_mode,
            password_case=self.meganet_password_case,
        )
        logging.info(
            "%s MEGANET_ID_AUTH nome_meganet=%s service=%s known=%s allowed=%s mode=%s password_case=%s reason=%s",
            self.peer,
            nome_meganet,
            service or "-",
            self.meganet_auth_result.known_user,
            self.meganet_auth_result.allowed,
            self.meganet_auth_mode,
            self.meganet_password_case,
            self.meganet_auth_result.reason + (";effective=" + effective_meganet_auth_mode if effective_meganet_auth_mode != self.meganet_auth_mode else ""),
        )
        if not self.meganet_auth_result.allowed:
            self.cadastro_db_blocked = True
            await self.reject_meganet_auth({"nome_meganet": nome_meganet}, self.meganet_auth_result.reason)
            return

        logging.info(
            "%s MEGANET_AUTH_ACCEPT nome_meganet=%s known=%s reason=%s",
            self.peer,
            nome_meganet,
            self.meganet_auth_result.known_user,
            self.meganet_auth_result.reason,
        )
        if not self._meganet_access_recorded:
            try:
                recorded = self.cadastro_db.record_user_access(
                    user_key=self.meganet_auth_result.user_key if self.meganet_auth_result else None,
                    nome_meganet=nome_meganet,
                    session_id=self.session_id,
                    peer=self.peer,
                    source="meganet-id-auth",
                )
                self._meganet_access_recorded = bool(recorded)
                if recorded:
                    logging.info("%s MEGANET_ACCESS_RECORDED user_key=%s cadastro_db=%s", self.peer, recorded[0], recorded[1])
            except Exception as exc:
                logging.warning("%s MEGANET_ACCESS_RECORD_FAILED nome_meganet=%s error=%s", self.peer, nome_meganet, exc)
        if self.access_identity_callback:
            try:
                self.access_identity_callback(nome_meganet)
            except Exception as exc:
                logging.debug("%s DASHBOARD_IDENTITY_UPDATE_FAILED service=meganet error=%s", self.peer, exc)
        await self.handle_post_id_auth_ok(nome_meganet, service)

    async def handle_post_id_auth_ok(self, nome_meganet: str, service: str) -> None:
        """Optional mapper behavior after successful #id authentication.

        For unknown service mapping, the ROM may wait for a packet-layer #ready#
        after #id before it sends the selected group command. Earlier builds only ACKed
        the #id client frame; the current implementation can also emit the configured Tectoy News message immediately
        after #id. This tests the hypothesis that the selected group is
        implicit in the dialed activity and no extra group token is sent.
        """
        logging.info(
            "%s POST_ID_AUTH_OK nome_meganet=%s service=%s action=%s",
            self.peer,
            nome_meganet,
            service or "-",
            self.post_id_action,
        )
        if self.post_id_action in ("ready", "ready-close"):
            await self.send_packet(b"#ready#\r", "post #id auth ok ready for next activity/token")
            if self.post_id_action == "ready-close":
                await self.force_close_client_session("post #id auth ok: close SSO transaction after ready")
        elif self.post_id_action in ("message-ready", "message-ready-close"):
            await self.send_message_block(
                [f"LOGIN OK: {nome_meganet}", "SELECIONE O SERVICO"],
                reason="post #id auth ok mapper message",
                final=False,
            )
            if self.post_id_action == "message-ready-close":
                await self.force_close_client_session("post #id auth ok: close SSO transaction after mapper message")
        elif self.post_id_action in ("tectoy-news", "tectoy-news-close"):
            if self.post_id_message_delay_seconds > 0:
                logging.info(
                    "%s POST_ID_MESSAGE_DELAY seconds=%.3f reason=Tectoy News before message block",
                    self.peer,
                    self.post_id_message_delay_seconds,
                )
                await asyncio.sleep(self.post_id_message_delay_seconds)
            news_lines = render_message_lines(self.tectoy_news_lines, nome_meganet)
            await self.send_message_block(
                news_lines,
                reason="post #id auth ok Tectoy News handler",
                final=(self.post_id_action == "tectoy-news-close"),
            )
        elif self.post_id_action in ("sync-batch", "sync-batch-close"):
            if self.post_id_message_delay_seconds > 0:
                logging.info(
                    "%s POST_ID_MESSAGE_DELAY seconds=%.3f reason=batch Outros/Mail/Tectoy before first block",
                    self.peer,
                    self.post_id_message_delay_seconds,
                )
                await asyncio.sleep(self.post_id_message_delay_seconds)
            logging.info(
                "%s SYNC_BATCH_START order=outros,mail,tectoy_news close=%s",
                self.peer,
                self.post_id_action == "sync-batch-close",
            )
            await self.send_message_block(
                render_message_lines(self.outros_group_lines, nome_meganet),
                reason="post #id auth ok batch step 1/3 GRUPO OUTROS",
                final=False,
            )
            if self.outbound_packet_delay_seconds > 0:
                await asyncio.sleep(self.outbound_packet_delay_seconds)
            await self.send_message_block(
                render_message_lines(self.mail_group_lines, nome_meganet),
                reason="post #id auth ok batch step 2/3 CORREIO ELETRONICO",
                final=False,
            )
            if self.outbound_packet_delay_seconds > 0:
                await asyncio.sleep(self.outbound_packet_delay_seconds)
            await self.send_message_block(
                render_message_lines(self.tectoy_news_lines, nome_meganet),
                reason="post #id auth ok batch step 3/3 TECTOY NEWS",
                final=(self.post_id_action == "sync-batch-close"),
            )
        elif self.post_id_action in ("id-exact-end-wait", "id-exact-end-ready-wait"):
            if self.post_id_message_delay_seconds > 0:
                logging.info(
                    "%s POST_ID_MESSAGE_DELAY seconds=%.3f reason=exact ROM id response before waiting for next token",
                    self.peer,
                    self.post_id_message_delay_seconds,
                )
                await asyncio.sleep(self.post_id_message_delay_seconds)
            await self.send_rom_exact_empty_message_end(
                reason="post #id auth ok exact ROM table response",
                ready=(self.post_id_action == "id-exact-end-ready-wait"),
            )
        elif self.post_id_action in ("id-body-hash-end-ready2-wait", "id-body-hash-end-ready1-wait", "id-body-hash-end-welcome-ready-wait", "id-body-hash-end-bemvindo-ready-wait"):
            if self.post_id_message_delay_seconds > 0:
                logging.info(
                    "%s POST_ID_MESSAGE_DELAY seconds=%.3f reason=body parser separator/status probe",
                    self.peer,
                    self.post_id_message_delay_seconds,
                )
                await asyncio.sleep(self.post_id_message_delay_seconds)
            panel_lines = self._build_connection_panel_lines()
            logging.info(
                "%s CONNECTION_PANEL_MESSAGE lines=%d content=%s",
                self.peer,
                len(panel_lines),
                " | ".join(panel_lines),
            )
            await self.send_rom_message_body_hash_end(
                panel_lines,
                reason="post #id configurable connection panel message",
            )
            if self.outbound_packet_delay_seconds > 0:
                await asyncio.sleep(self.outbound_packet_delay_seconds)
            if self.post_id_action == "id-body-hash-end-ready1-wait":
                await self.send_packet(b"#ready#\r", "post #id status probe: single #ready#")
            elif self.post_id_action == "id-body-hash-end-ready2-wait":
                logging.info(
                    "%s REVISTA_COMPLETE_ENABLE action=send-abril-then-ready descriptor=$016FF2 records=11",
                    self.peer,
                )
                await self.send_packet(
                    b"#abril#\r",
                    "post #id native Revista selector: #abril# sets $FF000E=1",
                )
                if self.outbound_packet_delay_seconds > 0:
                    await asyncio.sleep(self.outbound_packet_delay_seconds)
                await self.send_packet(
                    b"#ready#\r",
                    "post #id final #ready# after #abril# Revista selector",
                )
            elif self.post_id_action == "id-body-hash-end-welcome-ready-wait":
                await self.send_packet(b"#welcome#\r", "post #id status probe: #welcome# optional status")
                if self.outbound_packet_delay_seconds > 0:
                    await asyncio.sleep(self.outbound_packet_delay_seconds)
                await self.send_packet(b"#ready#\r", "post #id status probe: final #ready#")
            elif self.post_id_action == "id-body-hash-end-bemvindo-ready-wait":
                await self.send_packet(b"#bemvindo#\r", "post #id status probe: #bemvindo# optional status")
                if self.outbound_packet_delay_seconds > 0:
                    await asyncio.sleep(self.outbound_packet_delay_seconds)
                await self.send_packet(b"#ready#\r", "post #id status probe: final #ready#")
        elif self.post_id_action in ("id-exact-body-eot-wait", "id-exact-body-ack-eot-wait", "id-exact-body-ready-eot-wait"):
            if self.post_id_message_delay_seconds > 0:
                logging.info(
                    "%s POST_ID_MESSAGE_DELAY seconds=%.3f reason=exact ROM id response with one body line before message end",
                    self.peer,
                    self.post_id_message_delay_seconds,
                )
                await asyncio.sleep(self.post_id_message_delay_seconds)
            await self.send_rom_exact_message_with_body(
                ["OK"],
                reason="post #id auth ok exact ROM message with one body line",
                ready=(self.post_id_action == "id-exact-body-ready-eot-wait"),
            )
            if self.outbound_packet_delay_seconds > 0:
                await asyncio.sleep(self.outbound_packet_delay_seconds)
            if self.post_id_action == "id-exact-body-ack-eot-wait":
                await self.send_raw(bytes([ACK]), "post #id exact body message: raw ACK before EOT")
                if self.outbound_packet_delay_seconds > 0:
                    await asyncio.sleep(self.outbound_packet_delay_seconds)
            await self.send_raw(bytes([EOT]), "post #id exact body message: raw EOT after message end")
        elif self.post_id_action in ("id-exact-body-text-eot-wait", "id-exact-body-text-ack-eot-wait"):
            if self.post_id_message_delay_seconds > 0:
                logging.info(
                    "%s POST_ID_MESSAGE_DELAY seconds=%.3f reason=exact ROM id response with textual body before message end",
                    self.peer,
                    self.post_id_message_delay_seconds,
                )
                await asyncio.sleep(self.post_id_message_delay_seconds)
            await self.send_rom_exact_message_with_body(
                ["USUARIO IDENTIFICADO", "ACESSO LIBERADO"],
                reason="post #id auth ok exact ROM message with textual body",
                ready=False,
            )
            if self.outbound_packet_delay_seconds > 0:
                await asyncio.sleep(self.outbound_packet_delay_seconds)
            if self.post_id_action == "id-exact-body-text-ack-eot-wait":
                await self.send_raw(bytes([ACK]), "post #id exact text body message: raw ACK before EOT")
                if self.outbound_packet_delay_seconds > 0:
                    await asyncio.sleep(self.outbound_packet_delay_seconds)
            await self.send_raw(bytes([EOT]), "post #id exact text body message: raw EOT after message end")
        elif self.post_id_action in ("id-exact-end-raw-ack-eot-wait", "id-exact-end-raw-eot-wait", "id-exact-end-raw-ack-wait"):
            if self.post_id_message_delay_seconds > 0:
                logging.info(
                    "%s POST_ID_MESSAGE_DELAY seconds=%.3f reason=exact ROM id response with raw control after message end",
                    self.peer,
                    self.post_id_message_delay_seconds,
                )
                await asyncio.sleep(self.post_id_message_delay_seconds)
            await self.send_rom_exact_empty_message_end(
                reason="post #id auth ok exact ROM table response before raw control",
                ready=False,
            )
            if self.outbound_packet_delay_seconds > 0:
                await asyncio.sleep(self.outbound_packet_delay_seconds)
            if self.post_id_action in ("id-exact-end-raw-ack-eot-wait", "id-exact-end-raw-ack-wait"):
                await self.send_raw(bytes([ACK]), "post #id exact message end: raw ACK expected by FUN_00007fee")
                if self.outbound_packet_delay_seconds > 0:
                    await asyncio.sleep(self.outbound_packet_delay_seconds)
            if self.post_id_action in ("id-exact-end-raw-ack-eot-wait", "id-exact-end-raw-eot-wait"):
                await self.send_raw(bytes([EOT]), "post #id exact message end: raw EOT expected by FUN_00007fee")
        elif self.post_id_action in ("id-combined-empty-wait", "id-combined-empty-ready-wait", "id-combined-hash-empty-wait", "id-combined-empty-eot-wait", "id-combined-empty-news-wait", "id-combined-empty-begin-news-wait", "id-combined-empty-raw-ack-eot-wait", "id-combined-empty-raw-eot-wait"):
            if self.post_id_message_delay_seconds > 0:
                logging.info(
                    "%s POST_ID_MESSAGE_DELAY seconds=%.3f reason=combined ROM id response probe",
                    self.peer,
                    self.post_id_message_delay_seconds,
                )
                await asyncio.sleep(self.post_id_message_delay_seconds)
            use_hash_end = (self.post_id_action == "id-combined-hash-empty-wait")
            await self.send_rom_combined_empty_message(
                reason="post #id auth ok combined ROM table response",
                hash_end=use_hash_end,
            )
            if self.post_id_action == "id-combined-empty-ready-wait":
                if self.outbound_packet_delay_seconds > 0:
                    await asyncio.sleep(self.outbound_packet_delay_seconds)
                await self.send_packet(b"#ready#\r", "post #id auth ok combined response: optional #ready#")
            elif self.post_id_action == "id-combined-empty-eot-wait":
                if self.outbound_packet_delay_seconds > 0:
                    await asyncio.sleep(self.outbound_packet_delay_seconds)
                await self.send_raw(b"\x04", "post #id auth ok combined response: raw EOT")
            elif self.post_id_action in ("id-combined-empty-raw-ack-eot-wait", "id-combined-empty-raw-eot-wait"):
                if self.outbound_packet_delay_seconds > 0:
                    await asyncio.sleep(self.outbound_packet_delay_seconds)
                if self.post_id_action == "id-combined-empty-raw-ack-eot-wait":
                    await self.send_raw(bytes([ACK]), "post #id combined message end: raw ACK expected by FUN_00007fee")
                    if self.outbound_packet_delay_seconds > 0:
                        await asyncio.sleep(self.outbound_packet_delay_seconds)
                await self.send_raw(bytes([EOT]), "post #id combined message end: raw EOT expected by FUN_00007fee")
            elif self.post_id_action in ("id-combined-empty-news-wait", "id-combined-empty-begin-news-wait"):
                if self.outbound_packet_delay_seconds > 0:
                    await asyncio.sleep(self.outbound_packet_delay_seconds)
                await self.send_server_driven_news_probe(
                    begin=(self.post_id_action == "id-combined-empty-begin-news-wait"),
                    reason="post #id auth ok combined response then #send news probe",
                )
        elif self.post_id_action in ("id-exact-nocr-wait", "id-exact-nocr-ready-wait"):
            if self.post_id_message_delay_seconds > 0:
                logging.info(
                    "%s POST_ID_MESSAGE_DELAY seconds=%.3f reason=exact no-CR ROM id response probe",
                    self.peer,
                    self.post_id_message_delay_seconds,
                )
                await asyncio.sleep(self.post_id_message_delay_seconds)
            await self.send_rom_exact_empty_message_nocr(
                reason="post #id auth ok exact no-CR ROM table response",
                ready=(self.post_id_action == "id-exact-nocr-ready-wait"),
            )
        elif self.post_id_action in ("id-combined-nocr-wait", "id-combined-nocr-ready-wait"):
            if self.post_id_message_delay_seconds > 0:
                logging.info(
                    "%s POST_ID_MESSAGE_DELAY seconds=%.3f reason=combined no-CR ROM id response probe",
                    self.peer,
                    self.post_id_message_delay_seconds,
                )
                await asyncio.sleep(self.post_id_message_delay_seconds)
            await self.send_rom_combined_empty_message_nocr(
                reason="post #id auth ok combined no-CR ROM table response",
            )
            if self.post_id_action == "id-combined-nocr-ready-wait":
                if self.outbound_packet_delay_seconds > 0:
                    await asyncio.sleep(self.outbound_packet_delay_seconds)
                await self.send_packet(b"#ready#\r", "post #id auth ok combined no-CR response: optional #ready#")
        elif self.post_id_action in ("id-direct-send-news-wait", "id-direct-send-begin-news-wait", "id-exact-send-news-wait", "id-exact-send-begin-news-wait"):
            if self.post_id_message_delay_seconds > 0:
                logging.info(
                    "%s POST_ID_MESSAGE_DELAY seconds=%.3f reason=server-driven news token probe",
                    self.peer,
                    self.post_id_message_delay_seconds,
                )
                await asyncio.sleep(self.post_id_message_delay_seconds)
            if self.post_id_action.startswith("id-exact"):
                await self.send_rom_exact_empty_message_end(
                    reason="post #id auth ok exact ROM table response before server-driven news",
                    ready=False,
                )
                if self.outbound_packet_delay_seconds > 0:
                    await asyncio.sleep(self.outbound_packet_delay_seconds)
            await self.send_server_driven_news_probe(
                begin=("begin" in self.post_id_action),
                reason="post #id auth ok server-driven #send news probe",
            )
        elif self.post_id_action == "close":
            await self.force_close_client_session("post #id auth ok: close SSO transaction without packet")

    async def handle_end_cadastro(self) -> None:
        """Validate the cadastro result and send registration-specific messages."""
        record = self.cadastro.as_record()
        nome_meganet = str(record.get("nome_meganet") or self.meganet_id_user or "").strip()

        if self.meganet_id_auth_seen:
            if not self.meganet_auth_result or not self.meganet_auth_result.allowed:
                self.cadastro_db_blocked = True
                logging.info(
                    "%s MEGANET_AUTH cadastro_end blocked because #id auth failed nome_meganet=%s",
                    self.peer,
                    self.meganet_id_user,
                )
                return
            logging.info(
                "%s MEGANET_AUTH cadastro_end skipped; already authenticated by #id nome_meganet=%s known=%s allowed=%s reason=%s",
                self.peer,
                self.meganet_id_user,
                self.meganet_auth_result.known_user,
                self.meganet_auth_result.allowed,
                self.meganet_auth_result.reason,
            )
        else:
            # #end cadastro is explicitly a registration flow.  Even when
            # normal login runs with --meganet-auth-mode require-known, the
            # completed cadastro must be validated with allow-new so a new
            # fields.nome_meganet can be created.  Existing names are still
            # handled below by --registration-duplicate-action.
            cadastro_effective_auth_mode = "allow-new" if (self.cadastro_session_seen or self.cadastro.fields) else self.meganet_auth_mode
            self.meganet_auth_result = self.cadastro_db.validate_meganet_credentials(
                record,
                auth_mode=cadastro_effective_auth_mode,
                password_case=self.meganet_password_case,
            )
            logging.info(
                "%s MEGANET_AUTH_CADASTRO nome_meganet=%s known=%s allowed=%s mode=%s effective_mode=%s reason=%s",
                self.peer,
                record.get("nome_meganet", ""),
                self.meganet_auth_result.known_user,
                self.meganet_auth_result.allowed,
                self.meganet_auth_mode,
                cadastro_effective_auth_mode,
                self.meganet_auth_result.reason,
            )
            if not self.meganet_auth_result.allowed:
                self.cadastro_db_blocked = True
                await self.reject_meganet_auth(record, self.meganet_auth_result.reason)
                return

        if self.registration_duplicate_action == "reject-message":
            found = self.cadastro_db.find_user_by_meganet_name(nome_meganet) if nome_meganet else None
            if found:
                self.cadastro_db_blocked = True
                await self.reject_duplicate_registration(nome_meganet, found[0])
                return

        # codigo_acesso duplicate control is cadastro-only.  It does not
        # participate in login.  Default behavior is strict rejection so the
        # same physical/access code cannot be used to create multiple MegaNet
        # accounts.  Use --codigo-acesso-duplicate-action allow or
        # --allow-duplicate-codigo-acesso to relax this for testing.
        if self.codigo_acesso_duplicate_action == "reject-message":
            codigo_acesso = record.get("codigo_acesso", "")
            if codigo_acesso:
                # If an update flow is explicitly allowed for an already-known
                # nome_meganet, do not count the same record against itself.
                exclude_key = None
                if self.registration_duplicate_action == "allow-update" and nome_meganet:
                    existing_by_nome = self.cadastro_db.find_user_by_meganet_name(nome_meganet)
                    exclude_key = existing_by_nome[0] if existing_by_nome else None
                found_codigo = self.cadastro_db.find_user_by_codigo_acesso(codigo_acesso, exclude_user_key=exclude_key)
                if found_codigo:
                    self.cadastro_db_blocked = True
                    await self.reject_duplicate_codigo_acesso(codigo_acesso, found_codigo[0])
                    return

        await self.save_cadastro_db("end cadastro after meganet registration accepted")
        await self.send_registration_success_message(nome_meganet)

    async def reject_duplicate_registration(self, nome_meganet: str, existing_key: str) -> None:
        logging.warning(
            "%s MEGANET_REGISTRATION_DUPLICATE nome_meganet=%s existing_key=%s action=%s",
            self.peer,
            nome_meganet,
            existing_key,
            self.registration_duplicate_action,
        )
        duplicate_lines = render_message_lines(DUPLICATE_USER_MESSAGE, nome_meganet)
        await self.send_message_block(duplicate_lines, reason="Mega Net cadastro duplicate", final=True)

    async def reject_duplicate_codigo_acesso(self, codigo_acesso: str, existing_key: str) -> None:
        logging.warning(
            "%s MEGANET_REGISTRATION_DUPLICATE_CODIGO_ACESSO codigo_acesso=%s existing_key=%s action=%s",
            self.peer,
            codigo_acesso,
            existing_key,
            self.codigo_acesso_duplicate_action,
        )

        # /duplicate codigo_acesso is a hard cadastro rejection, equivalent
        # to the working invalid-user/invalid-password rejection path.  The ROM
        # only treats a server-side rejection as failure when the received body
        # contains the literal token ERRO and is followed by a valid status.
        # A plain message block without ERRO may be displayed as informational
        # or may let the flow continue.  Therefore we send:
        #   #message begin#
        #   ERRO
        #   CODIGO DE ACESSO ...
        #   #message end#
        #   #ready#
        # and then force-close the transaction so no cadastro is saved and no
        # queued service download continues.
        failure_lines = [
            "ERRO",
            "CODIGO DE ACESSO",
            "JA CADASTRADO",
            "NO BANCO DE DADOS",
        ]
        logging.info(
            "%s MEGANET_REGISTRATION_DUPLICATE_CODIGO_ACESSO_ERRO_STATUS codigo_acesso=%s existing_key=%s action=body-hash-end-ready-force-close",
            self.peer,
            codigo_acesso,
            existing_key,
        )
        await self.send_rom_message_body_hash_end(
            failure_lines,
            reason="Mega Net cadastro duplicate codigo_acesso ERRO body",
            wait_ack=False,
        )
        if self.outbound_packet_delay_seconds > 0:
            await asyncio.sleep(self.outbound_packet_delay_seconds)
        await self.send_packet(
            b"#ready#\r",
            "Mega Net cadastro duplicate codigo_acesso: #ready# for cadastro error status",
            wait_ack=False,
        )
        self.state = MegaNetState.AUTH_REJECTED
        self.cadastro_db_blocked = True
        logging.info(
            "%s MEGANET_CODIGO_ACESSO_REJECT_LOCK state=%s action=erro-body-ready-ignore-subsequent-packets-until-close",
            self.peer,
            self.state,
        )
        await self.force_close_client_session("Mega Net cadastro duplicate codigo_acesso: ERRO body delivered", delay=1.200)

    async def send_registration_success_message(self, nome_meganet: str) -> None:
        usuario = str(nome_meganet or self.meganet_id_user or "USUARIO").strip() or "USUARIO"
        email = self.cadastro_db.email_for_nome_meganet(usuario, self.mail_domain)
        if not email:
            email = meganet_email_for_name(usuario, self.mail_domain)
        record = self.cadastro.as_record()
        if self.welcome_email_enabled:
            try:
                subject = render_meganet_placeholders(self.welcome_email_subject, record, usuario=usuario, email=email, mail_domain=self.mail_domain)
                body_lines = render_meganet_placeholder_lines(self.welcome_email_body, record, usuario=usuario, email=email, mail_domain=self.mail_domain, max_lines=MAX_CONTENT_RECORD_LINES)
                welcome_path = self.mail_store.append_welcome_message(
                    email,
                    usuario,
                    subject=subject,
                    body_lines=body_lines,
                    sender=self.welcome_email_from or self.admin_email,
                )
                logging.info("%s MAIL_WELCOME_QUEUED nome_meganet=%s email=%s mailbox=%s subject=%s", self.peer, usuario, email, welcome_path, subject)
            except Exception as exc:
                logging.error("%s MAIL_WELCOME_FAILED nome_meganet=%s email=%s error=%s", self.peer, usuario, email, exc)
        else:
            logging.info("%s MAIL_WELCOME_DISABLED nome_meganet=%s email=%s", self.peer, usuario, email)
        success_lines = render_message_lines(self.message_lines, usuario)
        success_lines.extend([
            "SEU E-MAIL MEGA NET",
            email,
            "USE-O PARA TROCAR",
            "MENSAGENS",
        ])
        logging.info("%s MEGANET_REGISTRATION_ACCEPTED nome_meganet=%s email=%s", self.peer, usuario, email)
        await self.send_message_block(success_lines, reason="Mega Net cadastro accepted", final=True)

    async def reject_meganet_auth(self, record: dict, reason: str) -> None:
        logging.warning(
            "%s MEGANET_AUTH_REJECT nome_meganet=%s mode=%s reason=%s action=%s",
            self.peer,
            record.get("nome_meganet", ""),
            self.meganet_auth_mode,
            reason,
            self.meganet_reject_action,
        )
        if self.meganet_reject_action == "nak-close":
            await self.send_raw(bytes([NAK]), "NAK Mega Net auth rejected")
            try:
                self.writer.close()
            except Exception:
                pass
            return
        if self.meganet_reject_action == "silent-close":
            try:
                self.writer.close()
            except Exception:
                pass
            return

        # the Ghidra report shows that the #id routine does not have a
        # separate remote "invalid password" token.  It parses a normal
        # #message begin# body, requires the final status token #ready#
        # (index 4), and only then searches the received message body for the
        # literal ROM string "ERRO" at 0x161e2.  If "ERRO" is found, the
        # routine returns D0=0x45 instead of D0=0x30.
        #
        # Therefore, a remote auth rejection must look like a completed #id
        # transaction whose body contains ERRO.  Sending no #ready# leaves the
        # ROM stuck at "Identificando usuario"; sending #ready# without ERRO
        # makes the ROM continue to the queued downloads.
        reason_text = str(reason or "").lower()
        if "unknown" in reason_text or "missing_nome" in reason_text:
            failure_kind = "unknown-user"
            failure_lines = [
                "ERRO",
                "USUARIO INVALIDO",
                "TENTE NOVAMENTE",
            ]
        elif "missing_senha" in reason_text or "password" in reason_text or "senha" in reason_text:
            failure_kind = "password"
            failure_lines = [
                "ERRO",
                "SENHA INVALIDA",
                "TENTE NOVAMENTE",
            ]
        else:
            failure_kind = "generic"
            failure_lines = [
                "ERRO",
                "DADOS INVALIDOS",
                "TENTE NOVAMENTE",
            ]
        logging.info(
            "%s MEGANET_AUTH_REJECT_ERRO_STATUS kind=%s reason=%s action=body-hash-end-ready2-d0-45",
            self.peer,
            failure_kind,
            reason,
        )
        logging.info(
            "%s MEGANET_AUTH_REJECT_FAST_PATH wait_ack=no close_delay=1.200s "
            "reason=terminal-error-status-is-not-stop-and-wait",
            self.peer,
        )
        await self.send_rom_message_body_hash_end(
            failure_lines,
            reason="Mega Net auth rejected ERRO body for ROM D0=0x45",
            wait_ack=False,
        )
        if self.outbound_packet_delay_seconds > 0:
            await asyncio.sleep(self.outbound_packet_delay_seconds)
        await self.send_packet(
            b"#ready#\r",
            "Mega Net auth rejected: first #ready# for identify status",
            wait_ack=False,
        )
        if self.outbound_packet_delay_seconds > 0:
            await asyncio.sleep(self.outbound_packet_delay_seconds)
        await self.send_packet(
            b"#ready#\r",
            "Mega Net auth rejected: second #ready# required by identify path",
            wait_ack=False,
        )
        self.state = MegaNetState.AUTH_REJECTED
        self.cadastro_db_blocked = True
        logging.info(
            "%s MEGANET_AUTH_REJECT_LOCK state=%s action=erro-body-ready2-ignore-subsequent-packets-until-close",
            self.peer,
            self.state,
        )
        await self.force_close_client_session("Mega Net auth rejected: ERRO body delivered", delay=1.200)

    async def send_message_block(self, lines: Optional[list[str]] = None, reason: str = "message", final: bool = False) -> None:
        selected_lines = prepare_display_lines(lines or self.message_lines, self.outbound_line_max_len)
        logging.info(
            "%s MESSAGE_BLOCK_SAFE reason=%s lines=%d max_line_len=%d packet_delay=%.3f",
            self.peer, reason, len(selected_lines), self.outbound_line_max_len, self.outbound_packet_delay_seconds,
        )
        await self.send_packet(b"#message begin#\r", f"{reason}: message begin")
        for line in selected_lines:
            if self.outbound_packet_delay_seconds > 0:
                await asyncio.sleep(self.outbound_packet_delay_seconds)
            await self.send_packet(packet_text_bytes(line) + b"\r", f"{reason}: message line: {line}")
        if self.outbound_packet_delay_seconds > 0:
            await asyncio.sleep(self.outbound_packet_delay_seconds)
        await self.send_packet(b"#message end#\r", f"{reason}: message end with leading #")
        if self.outbound_packet_delay_seconds > 0:
            await asyncio.sleep(self.outbound_packet_delay_seconds)
        await self.send_packet(b"#ready#\r", f"{reason}: ready after message")
        if final:
            await self.close_transaction(f"{reason}: final message block")

    async def send_rom_message_body_hash_end(
        self,
        lines: list[str],
        reason: str = "ROM message body with hash-prefixed end",
        *,
        wait_ack: Optional[bool] = None,
    ) -> None:
        """Send post-#id body using the delimiter semantics found in Ghidra.

        FUN_00008C5C reads body bytes until it consumes a '#' byte.  The next
        caller then compares the remaining stream with 'message end#'.  This
        means the wire token after the body must be '#message end#', not
        'message end#'.  The parser consumes the leading '#', and the token
        comparator consumes 'message end#'.
        """
        selected_lines = prepare_display_lines(lines, self.outbound_line_max_len)
        logging.info(
            "%s ROM_BODY_HASH_END reason=%s lines=%d packet_delay=%.3f",
            self.peer, reason, len(selected_lines), self.outbound_packet_delay_seconds,
        )
        await self.send_packet(
            b"#message begin#\r",
            f"{reason}: exact #message begin#",
            wait_ack=wait_ack,
        )
        for line in selected_lines:
            if self.outbound_packet_delay_seconds > 0:
                await asyncio.sleep(self.outbound_packet_delay_seconds)
            await self.send_packet(
                packet_text_bytes(line) + b"\r",
                f"{reason}: body line: {line}",
                wait_ack=wait_ack,
            )
        if self.outbound_packet_delay_seconds > 0:
            await asyncio.sleep(self.outbound_packet_delay_seconds)
        await self.send_packet(
            b"#message end#\r",
            f"{reason}: hash-prefixed #message end# separator",
            wait_ack=wait_ack,
        )

    async def send_rom_exact_message_with_body(self, lines: list[str], reason: str = "ROM exact message with body", ready: bool = False) -> None:
        """Send ROM-table post-#id message block with at least one body line.

        Ghidra shows the #id routine waits for '#message begin#', then calls
        an intermediate routine at 0x008C5C before it compares 'message end#'.
        Empty begin/end blocks likely fail in that intermediate routine. This
        helper sends one or more normal body packets between the exact ROM-table
        delimiters, using 'message end#' without a leading '#'.
        """
        selected_lines = prepare_display_lines(lines, self.outbound_line_max_len)
        logging.info(
            "%s ROM_EXACT_MESSAGE_WITH_BODY reason=%s lines=%d ready=%s packet_delay=%.3f",
            self.peer, reason, len(selected_lines), ready, self.outbound_packet_delay_seconds,
        )
        await self.send_packet(b"#message begin#\r", f"{reason}: exact #message begin#")
        for line in selected_lines:
            if self.outbound_packet_delay_seconds > 0:
                await asyncio.sleep(self.outbound_packet_delay_seconds)
            await self.send_packet(packet_text_bytes(line) + b"\r", f"{reason}: body line: {line}")
        if self.outbound_packet_delay_seconds > 0:
            await asyncio.sleep(self.outbound_packet_delay_seconds)
        await self.send_packet(b"message end#\r", f"{reason}: exact message end# without leading #")
        if ready:
            if self.outbound_packet_delay_seconds > 0:
                await asyncio.sleep(self.outbound_packet_delay_seconds)
            await self.send_packet(b"#ready#\r", f"{reason}: optional #ready# after exact body message")

    async def send_rom_exact_empty_message_end(self, reason: str = "ROM exact empty message end", ready: bool = False) -> None:
        """Send the exact token pair found in the ROM string table after #id/#senha.

        The ROM table stores '#message begin#' followed by 'message end#'
        (without a leading '#'). Earlier probes mostly used '#message end#'
        or raw bytes. This sends both as normal framed MegaNet packets.
        """
        logging.info(
            "%s ROM_EXACT_EMPTY_MESSAGE_END reason=%s ready=%s packet_delay=%.3f",
            self.peer, reason, ready, self.outbound_packet_delay_seconds,
        )
        await self.send_packet(b"#message begin#\r", f"{reason}: exact #message begin#")
        if self.outbound_packet_delay_seconds > 0:
            await asyncio.sleep(self.outbound_packet_delay_seconds)
        await self.send_packet(b"message end#\r", f"{reason}: exact message end# without leading #")
        if ready:
            if self.outbound_packet_delay_seconds > 0:
                await asyncio.sleep(self.outbound_packet_delay_seconds)
            await self.send_packet(b"#ready#\r", f"{reason}: optional #ready# after exact empty message")

    async def send_rom_exact_empty_message_nocr(self, reason: str = "ROM exact empty message no CR", ready: bool = False) -> None:
        """Send ROM table tokens as framed packets without trailing CR.

        #bemvindo# works as a packet payload without CR. The post-#id table
        contains '#message begin#' and 'message end#'. This probe tests
        whether the comparator expects the exact token bytes, with no CR in
        the framed payload.
        """
        logging.info(
            "%s ROM_EXACT_EMPTY_MESSAGE_NOCR reason=%s ready=%s packet_delay=%.3f",
            self.peer, reason, ready, self.outbound_packet_delay_seconds,
        )
        await self.send_packet(b"#message begin#", f"{reason}: exact #message begin# no CR")
        if self.outbound_packet_delay_seconds > 0:
            await asyncio.sleep(self.outbound_packet_delay_seconds)
        await self.send_packet(b"message end#", f"{reason}: exact message end# no leading # no CR")
        if ready:
            if self.outbound_packet_delay_seconds > 0:
                await asyncio.sleep(self.outbound_packet_delay_seconds)
            await self.send_packet(b"#ready#\r", f"{reason}: optional #ready# after exact no-CR empty message")

    async def send_rom_combined_empty_message_nocr(self, reason: str = "ROM combined empty message no CR") -> None:
        """Send begin/end concatenated into one framed payload without CR."""
        payload = b"#message begin#message end#"
        logging.info(
            "%s ROM_COMBINED_EMPTY_MESSAGE_NOCR reason=%s len=%d packet_delay=%.3f",
            self.peer, reason, len(payload), self.outbound_packet_delay_seconds,
        )
        await self.send_packet(payload, f"{reason}: combined no-CR rom-table tokens")

    async def send_rom_combined_empty_message(self, reason: str = "ROM combined empty message", hash_end: bool = False) -> None:
        """Send begin/end in a single server packet after #id.

        An earlier probe sent '#message begin#' and 'message end#' as two separate framed
        packets and the ROM ACKed them but stayed in the identification state.
        This probe tests whether the post-#id message parser expects the empty
        block to arrive as one payload frame.
        """
        if hash_end:
            payload = b"#message begin#\r#message end#\r"
            mode = "hash-end"
        else:
            payload = b"#message begin#\rmessage end#\r"
            mode = "rom-table-no-leading-hash-end"
        logging.info(
            "%s ROM_COMBINED_EMPTY_MESSAGE reason=%s mode=%s len=%d packet_delay=%.3f",
            self.peer, reason, mode, len(payload), self.outbound_packet_delay_seconds,
        )
        await self.send_packet(payload, f"{reason}: combined {mode}")

    async def send_server_driven_news_probe(self, begin: bool = False, reason: str = "server-driven news probe") -> None:
        """Probe whether the server must initiate the ROM's next stage using ROM table tokens.

        The ROM string table contains '#send news [1]#' followed by '#begin news [1]#'.
        Earlier tests waited for the client to send '#send news', but it never did.
        This probe sends '#send news 1#' from the server after #id auth; optionally it
        also sends '#begin news 1#'.  The goal is only to see whether the ROM exits
        the identification state and advances to 'Recebendo Noticias'.
        """
        logging.info(
            "%s SERVER_DRIVEN_NEWS_PROBE begin=%s action=send-news%s",
            self.peer, begin, "+begin-news" if begin else "",
        )
        await self.send_packet(b"#send news 1#\r", f"{reason}: #send news 1#")
        if begin:
            if self.outbound_packet_delay_seconds > 0:
                await asyncio.sleep(self.outbound_packet_delay_seconds)
            await self.send_packet(b"#begin news 1#\r", f"{reason}: #begin news 1#")

    def _content_lines_for_key(self, key: str, fallback: list[str]) -> list[str]:
        records = merge_content_records(getattr(self, "content_records", None))
        lines = records.get(key) or fallback
        cleaned = strip_content_mapping_lines([str(x) for x in lines])
        if not cleaned:
            cleaned = strip_content_mapping_lines([str(x) for x in fallback])
        return normalize_content_record_lines(cleaned, max_lines=MAX_CONTENT_RECORD_TOTAL_LINES)

    def _make_content_record_chunks(self, idx: int, path: str, body_lines: list[str], *, max_chunk_bytes: int = CONTENT_RECORD_BODY_MAX_BYTES) -> list[bytes]:
        """Build transport chunks for one logical ROM content item.

        `idx` and `path` are internal routing/UI metadata only.  They are never
        included in the payload shown by the MegaNet client.

        Important protocol detail: `#item#` and `#end#` delimit logical menu
        items, not continuation chunks.  Therefore a long configured message is
        split into several server frames without any status token, and only the
        final chunk of that same logical item receives `#item#` or `#end#` in
        send_news_minimal_response().
        """
        max_line = max(8, min(MAX_MESSAGE_LINE_LEN, int(getattr(self, "outbound_line_max_len", MAX_MESSAGE_LINE_LEN) or MAX_MESSAGE_LINE_LEN)))
        source_lines: list[str] = []
        for raw in body_lines:
            if is_content_blank_line(raw) or str(raw or "") == "":
                # A blank row is encoded as one CR between adjacent content rows.
                source_lines.append("")
                continue

            clean = _safe_latin1_content_text(raw).replace("#", "").strip()
            if not clean:
                continue
            source_lines.extend(split_line_for_rom(clean, max_line))

        if not source_lines:
            source_lines = ["SEM CONTEUDO"]

        chunks: list[bytes] = []
        packed: list[str] = []
        used = 0
        for line in source_lines:
            encoded = line.encode("latin-1", "replace")
            add_len = len(encoded) + 1  # CR terminator
            if packed and used + add_len > max_chunk_bytes:
                chunks.append("\r".join(packed).encode("latin-1", "replace") + b"\r")
                packed = []
                used = 0
            if add_len > max_chunk_bytes:
                # Defensive fallback. This should not occur with 35-char lines,
                # but keeps the packet builder safe if constants are changed.
                for part in split_line_for_rom(line, max(8, max_chunk_bytes - 1)):
                    if packed:
                        chunks.append("\r".join(packed).encode("latin-1", "replace") + b"\r")
                        packed = []
                        used = 0
                    packed.append(part)
                    used = len(part.encode("latin-1", "replace")) + 1
                continue
            packed.append(line)
            used += add_len

        if packed:
            chunks.append("\r".join(packed).encode("latin-1", "replace") + b"\r")

        page_count = max(1, (len(source_lines) + MAX_CONTENT_RECORD_LINES - 1) // MAX_CONTENT_RECORD_LINES)
        logging.info(
            "%s CONTENT_EXTENDED_ITEM item=%02d path=%s lines=%d native_page_lines=%d visual_blocks=%d chunks=%d extended=%s body_bytes=%d",
            self.peer,
            idx,
            path,
            len(source_lines),
            MAX_CONTENT_RECORD_LINES,
            page_count,
            len(chunks),
            "yes" if page_count > 1 else "no",
            sum(content_line_wire_bytes(line) for line in source_lines),
        )
        return chunks

    def _profile_content_records(self, profile: str) -> list[list[bytes]]:
        profile = (profile or "tectoy-news").lower()
        defs = [item for item in CONTENT_RECORD_DEFINITIONS if item.get("profile") == profile]
        if profile == "revista":
            defs.sort(key=lambda item: int(item.get("record_index", 0)))
            actual_indexes = [int(item.get("record_index", -1)) for item in defs]
            expected_indexes = list(range(11))
            if actual_indexes != expected_indexes:
                raise ValueError(
                    f"Revista record order invalid: expected {expected_indexes}, got {actual_indexes}"
                )
            logging.info(
                "%s REVISTA_RECORD_SET mode=complete records=%d keys=%s",
                self.peer,
                len(defs),
                ",".join(str(item.get("key", "")) for item in defs),
            )
        prepared_lines: dict[str, list[str]] = {}
        for item in defs:
            key = str(item["key"])
            fallback = [str(x) for x in item.get("default", [])]
            prepared_lines[key] = self._content_lines_for_key(key, fallback)

        safe_lines, before_budget, after_budget, clamped = fit_profile_lines_to_safe_budget(profile, prepared_lines)
        if clamped:
            logging.warning(
                "%s CONTENT_GROUP_BUDGET_CLAMP profile=%s before=%d after=%d limit=%d action=preserve-native-19-then-extra-in-order",
                self.peer,
                profile,
                int(before_budget.get("total_bytes", 0)),
                int(after_budget.get("total_bytes", 0)),
                MAX_CONTENT_GROUP_SAFE_BYTES,
            )
        else:
            logging.info(
                "%s CONTENT_GROUP_BUDGET profile=%s used=%d limit=%d remaining=%d records=%d",
                self.peer,
                profile,
                int(after_budget.get("total_bytes", 0)),
                MAX_CONTENT_GROUP_SAFE_BYTES,
                MAX_CONTENT_GROUP_SAFE_BYTES - int(after_budget.get("total_bytes", 0)),
                len(defs),
            )

        records: list[list[bytes]] = []
        for idx, item in enumerate(defs):
            key = str(item["key"])
            path = str(item["path"])
            records.append(self._make_content_record_chunks(idx, path, safe_lines.get(key) or ["SEM CONTEUDO"]))
        return records

    def _tectoy_news_menu_records(self, group: str) -> list[list[bytes]]:
        """Return Tec Toy News/Dicas/Sega Club records in the ROM's validated order."""
        return self._profile_content_records("tectoy-news")

    def _internet_group_records(self, group: str) -> list[list[bytes]]:
        """Return Internet group records in the validated cartridge menu order."""
        return self._profile_content_records("internet")

    def _revista_group_records(self, group: str) -> list[list[bytes]]:
        """Return Revista group records."""
        return self._profile_content_records("revista")

    def _outros_group_records(self, group: str) -> list[list[bytes]]:
        """Return Outros/Home Shopping group records in the validated menu order."""
        return self._profile_content_records("outros")

    def _mail_group_records(self, group: str) -> list[list[bytes]]:
        """Return the auxiliary OUTROS record set during MAIL service connections."""
        return self._outros_group_records(group)


    def _current_meganet_email(self) -> str:
        user = self.meganet_id_user or self.returning_user_key or ""
        if user:
            return self.cadastro_db.email_for_nome_meganet(user, self.mail_domain)
        return ""

    def _current_mailbox_message_count(self) -> int:
        """Return pending mailbox messages for the authenticated MegaNet user.

        This count is safe to compute immediately after #id because the user is
        already known, even though the ROM has not yet told us which launcher
        group will be downloaded next.
        """
        mailbox_email = self._current_meganet_email()
        if not mailbox_email:
            return 0
        try:
            return self.mail_store.mailbox_panel_notice_count(mailbox_email)
        except Exception as exc:
            logging.warning("%s MAIL_COUNT_FAILED user=%s email=%s error=%s", self.peer, self.meganet_id_user or self.returning_user_key or "", mailbox_email, exc)
            return 0

    def _build_connection_panel_lines(self) -> list[str]:
        """Render the post-#id connection-panel message.

        Important limitation: in unified TCP mode the ROM only sends the selected
        download tokens (#send news N#, #send mail#) after this post-#id message
        has already been answered with #ready#/#ready#. Therefore the server can
        safely render user/mailbox-specific information here, but it cannot know
        the selected launcher group at this exact moment unless the legacy
        diagnostic --service mode/manual preset is used or the emulator bridge
        later forwards that context.
        """
        usuario = self.meganet_id_user or self.returning_user_key or "USUARIO"
        email = self._current_meganet_email() or self.cadastro_db.email_for_nome_meganet(usuario, self.mail_domain)
        mail_count = self._current_mailbox_message_count() if self.connection_panel_mail_count != "off" else 0
        if mail_count == 0:
            mail_status = "SEM NOVAS MENSAGENS"
        elif mail_count == 1:
            mail_status = "VOCE TEM 01 MENSAGEM"
        else:
            mail_status = f"VOCE TEM {mail_count:02d} MENSAGENS"
        replacements = {
            "{usuario}": str(usuario),
            "{USUARIO}": str(usuario).upper(),
            "{email}": email,
            "{EMAIL}": email.upper(),
            "{mail_count}": f"{mail_count:02d}",
            "{MAIL_COUNT}": f"{mail_count:02d}",
            "{mail_status}": mail_status,
            "{MAIL_STATUS}": mail_status.upper(),
            # The admin page historically documented double-brace placeholders
            # because the HTML is assembled with Python formatted strings.
            # Accept both forms in saved configuration so existing panels render
            # correctly regardless of whether the user typed {mail_count} or
            # copied {{mail_count}} from the help text.
            "{{usuario}}": str(usuario),
            "{{USUARIO}}": str(usuario).upper(),
            "{{email}}": email,
            "{{EMAIL}}": email.upper(),
            "{{mail_count}}": f"{mail_count:02d}",
            "{{MAIL_COUNT}}": f"{mail_count:02d}",
            "{{mail_status}}": mail_status,
            "{{MAIL_STATUS}}": mail_status.upper(),
        }
        rendered: list[str] = []
        for raw in self.connection_panel_lines:
            line = str(raw)
            for key, value in replacements.items():
                line = line.replace(key, value)
            rendered.append(line)
        # Preserve the previous default behavior if the user supplies a custom
        # panel without placeholders and still wants an automatic mail count.
        # We only alter the built-in unified/default neutral panels.
        preset_like_neutral = any("RECEBENDO SERVICOS" in str(x).upper() for x in self.connection_panel_lines)
        has_mail_placeholder = any("{mail_" in str(x).lower() or "{MAIL_" in str(x) for x in self.connection_panel_lines)
        if self.connection_panel_mail_count == "auto" and preset_like_neutral and not has_mail_placeholder:
            rendered = ["CONECTADO A MEGA NET", mail_status, "RECEBENDO SERVICOS"]
        logging.info("%s CONNECTION_PANEL_DYNAMIC usuario=%s email=%s mail_count=%d mail_status=%s", self.peer, usuario, email, mail_count, mail_status)
        if mail_count > 0 and email and self.connection_panel_mail_count != "off":
            try:
                cleared_notice, notice_path = self.mail_store.clear_mailbox_panel_notice(email, reason="connection_panel_shown")
                if cleared_notice:
                    logging.info("%s MAIL_PANEL_NOTICE_CLEARED email=%s count=%d path=%s", self.peer, email, cleared_notice, notice_path)
            except Exception as exc:
                logging.warning("%s MAIL_PANEL_NOTICE_CLEAR_FAILED email=%s error=%s", self.peer, email, exc)
        return normalize_display_message_lines(rendered)

    def _pack_mail_record_lines(self, lines: list[str], *, max_record_bytes: int = 116) -> bytes:
        """Pack a ROM mail record so payload + #end mail# never exceeds 127 bytes.

        The mail receiver stalls at Item 00 when one mail record is larger than
        the observed 127-byte packet payload.  `max_record_bytes` is 116 because
        the largest terminator we append later is `#end mail#\r` (11 bytes).
        Therefore every mail body produced here is safe with both #sep# and
        #end mail#.  This option is intentionally independent from
        --outbound-line-max-len: that option limits visual line width, while this
        function limits the whole framed mail record.

        This keeps Latin-1/high-ASCII bytes intact so MegaNet emoticons composed
        in the ROM can be downloaded again without being converted to '?'.
        """
        safe_lines: list[bytes] = []
        used = 0
        max_line = max(8, min(MAX_MESSAGE_LINE_LEN, int(getattr(self, "outbound_line_max_len", MAX_MESSAGE_LINE_LEN) or MAX_MESSAGE_LINE_LEN)))
        for raw in lines:
            line = _safe_meganet_mail_text(raw)
            if not line:
                continue
            encoded = line.encode("latin-1", "replace")
            if len(encoded) > max_line:
                encoded = encoded[:max_line]
            add_len = len(encoded) + 1  # CR terminator
            if used + add_len <= max_record_bytes:
                safe_lines.append(encoded)
                used += add_len
                continue
            remaining = max_record_bytes - used - 1
            if remaining >= 4:
                safe_lines.append(encoded[:remaining])
                used = max_record_bytes
            break
        if not safe_lines:
            safe_lines = [b"MAIL"]
        return b"\r".join(safe_lines) + b"\r"

    def _mail_sender_label(self, value: object) -> str:
        text = _safe_ascii_text(value or "MEGA NET").strip()
        local = email_local_part(text) if "@" in text else ""
        return (local or text or "MEGA NET")[:20]

    def _mail_receive_records(self) -> list[bytes]:
        """Return incoming e-mail messages compacted for the ROM mail reader.

        Each ROM mail message is sent as a single framed record followed by
        #sep# or #end mail#.  The complete record must stay below 127 bytes;
        otherwise the ROM stops at Item 00. The current implementation compacts/truncates each message
        deterministically instead of raising an exception.
        """
        mailbox_email = self._current_meganet_email()
        self.pending_mailbox_clear_email = None
        self.pending_mailbox_clear_count = 0
        messages = self.mail_store.load_mailbox_messages(mailbox_email, include_global_when_empty=False) if mailbox_email else self.mail_store.load_inbox_messages()
        logging.info("%s MAIL_INBOX_SELECT user=%s mailbox=%s messages=%d", self.peer, self.meganet_id_user or self.returning_user_key or "", mailbox_email or "<legacy-global>", len(messages))
        if not messages:
            return []
        if mailbox_email:
            self.pending_mailbox_clear_email = mailbox_email
            self.pending_mailbox_clear_count = len(messages)
        records: list[bytes] = []
        for idx, msg in enumerate(messages):
            from_email = normalize_email_address(msg.get("from") or msg.get("de") or self.admin_email, self.mail_domain)
            if not from_email:
                from_email = self.admin_email
            subject_text = _safe_ascii_text(msg.get("subject") or msg.get("assunto") or "SEM ASSUNTO")[:27] or "SEM ASSUNTO"
            body_value = msg.get("body") or msg.get("corpo") or []
            if isinstance(body_value, str):
                body_lines = body_value.splitlines() or [body_value]
            elif isinstance(body_value, list):
                body_lines = [str(x) for x in body_value]
            else:
                body_lines = [str(body_value)]
            compact_body = [_safe_meganet_mail_text(line) for line in body_lines if _safe_meganet_mail_text(line)]
            lines = [f"DE: {from_email}", f"ASSUNTO: {subject_text}", *compact_body]
            rec = self._pack_mail_record_lines(lines, max_record_bytes=116)
            original_preview = " | ".join(_safe_meganet_mail_text(x) for x in lines)
            original_est_len = len(("\r".join(lines).encode("latin-1", "replace") + b"\r"))
            if original_est_len > 116:
                logging.info(
                    "%s MAIL_RECORD_COMPACTED idx=%02d original_est_len=%d packed_len=%d preview=%s",
                    self.peer, idx, original_est_len, len(rec), original_preview[:120],
                )
            records.append(rec.replace(b"#", b""))
        return records

    async def persist_outgoing_mail_upload(self) -> None:
        # parse the original bytes captured between #begin mail# and
        # #end mail#.  Earlier builds only kept the human-readable preview,
        # so keep a defensive fallback for diagnostics/replay.
        raw_chunks = list(self.mail_upload_raw_chunks)
        if not raw_chunks and self.mail_upload_chunks:
            raw_chunks = [str(chunk).replace(" | ", "\r").encode("latin-1", "replace") for chunk in self.mail_upload_chunks]
        parsed = MailStore.parse_upload(raw_chunks)
        if not str(parsed.get("to") or "").strip():
            parsed["to"] = self.admin_email
            parsed["delivery_note"] = "destinatario ausente no upload; entregue ao admin"
            logging.warning(
                "%s OUTGOING_MAIL_RECIPIENT_MISSING default_to_admin=%s raw=%s",
                self.peer,
                self.admin_email,
                _safe_ascii_text(parsed.get("raw_text", ""))[:300],
            )
        entry = {
            "timestamp": _dt.datetime.now().isoformat(timespec="seconds"),
            "session_id": self.session_id,
            "peer": self.peer,
            "user": self.meganet_id_user or self.returning_user_key or "",
            "from": self._current_meganet_email() or self.admin_email,
            "service": self.content_profile,
            **parsed,
        }
        path = self.mail_store.append_outgoing(entry, cadastro_db=self.cadastro_db)
        logging.info(
            "%s OUTGOING_MAIL_END upload_no=%d chunks=%d type=%s from=%s to=%s subject=%s delivery=%s delivered_to=%s saved=%s content=%s",
            self.peer,
            self.mail_upload_count,
            len(self.mail_upload_chunks),
            entry.get("type"),
            entry.get("from"),
            entry.get("to"),
            entry.get("subject"),
            entry.get("delivery_status"),
            entry.get("delivered_to"),
            path,
            " | ".join(self.mail_upload_chunks)[:500],
        )

    def _profile_records(self, profile: str, group: str) -> list[list[bytes]]:
        profile = (profile or "tectoy-news").lower()
        if profile == "internet":
            return self._internet_group_records(group)
        if profile == "revista":
            return self._revista_group_records(group)
        if profile == "outros":
            return self._outros_group_records(group)
        if profile == "mail":
            return self._mail_group_records(group)
        if profile == "troca-senha":
            return [[b"TROCA DE SENHA\rMODO DIAGNOSTICO\r"]]
        return self._tectoy_news_menu_records(group)

    def _route_group_profile(self, group: str) -> str:
        """Map the ROM's generic #send news N# request to a real MegaNet group.

        Validated/working routing after the mapping phase:
          1 -> tectoy-news
          2 -> internet
          3 -> revista
          4 -> outros

        In manual mode, the legacy --content-profile is used for every group request.
        """
        if (self.routing_mode or "unified").lower() == "manual":
            return (self.content_profile or "tectoy-news").lower()
        key = str(group).strip()
        return (self.news_route_map.get(key) or self.content_profile or "tectoy-news").lower()

    def _active_group_records(self, group: str) -> tuple[str, list[list[bytes]]]:
        profile = self._route_group_profile(group)
        return profile, self._profile_records(profile, group)

    async def send_news_minimal_response(self, token: bytes, reason: str = "news token response") -> None:
        """Respond to ROM '#send news N#' with mapped Tec Toy News content records.

        Protocol recovered from FUN_000083b0:

            ROM    -> #send news N#
            SERVER -> #begin news N#
            SERVER -> <record 00 bytes>#item#
            SERVER -> <record 01 bytes>#item#
            ...
            SERVER -> <last record bytes>#end#
            SERVER -> #ready#

        The leading '#' of #item#/#end# is consumed by the body parser at 0x08C5C.
        The status reader at 0x0884E then sees item#/end# and maps it through
        the status table at 0x178B4.  The final #ready# maps to status index 4.
        """
        text = ascii_text(token).strip()
        import re
        m = re.search(r"#send news\s+([^#\r\n]+)#", text, flags=re.IGNORECASE)
        if m:
            group = m.group(1).strip()
        elif text.lower().startswith("#send internet"):
            # Legacy alias retained by feed_packets(): it is semantically the
            # Internet launcher and therefore maps to the validated group 2.
            group = "2"
        else:
            group = "1"
        begin = f"#begin news {group}#".encode("ascii", "replace") + b"\r"
        routed_profile, records = self._active_group_records(group)

        logging.info(
            "%s GROUP_ROUTE token=%s group=%s route=%s routing_mode=%s records=%d",
            self.peer, text, group, routed_profile, self.routing_mode, len(records),
        )
        total_chunks = sum(len(item_chunks) for item_chunks in records)
        logging.info(
            "%s GROUP_REQUEST token=%s group=%s profile=%s action=begin-news-mapped-content records=%d chunks=%d",
            self.peer, text, group, routed_profile, len(records), total_chunks,
        )
        await self.send_packet(begin, f"{reason}: #begin news {group}#", reliable=True)
        if self.outbound_packet_delay_seconds > 0:
            await asyncio.sleep(self.outbound_packet_delay_seconds)

        for item_idx, item_chunks in enumerate(records):
            if not item_chunks:
                item_chunks = [b"SEM CONTEUDO\r"]
            item_status = b"#end#\r" if item_idx == len(records) - 1 else b"#item#\r"
            for chunk_idx, chunk in enumerate(item_chunks):
                is_last_chunk = chunk_idx == len(item_chunks) - 1
                status = item_status if is_last_chunk else b""
                payload = chunk + status
                if len(payload) > 127:
                    logging.error(
                        "%s GROUP_RECORD_TOO_LONG item=%02d chunk=%02d len=%d max=127 profile=%s preview=%s",
                        self.peer, item_idx, chunk_idx, len(payload), routed_profile, ascii_text(payload[:80]),
                    )
                    raise ValueError(f"mapped content item {item_idx:02d} chunk {chunk_idx:02d} too long: {len(payload)} bytes")
                status_label = status.decode('ascii').strip() if status else "CONT"
                logging.info(
                    "%s GROUP_RECORD item=%02d chunk=%02d/%02d payload_len=%d status=%s",
                    self.peer, item_idx, chunk_idx + 1, len(item_chunks), len(payload), status_label,
                )
                await self.send_packet(payload, f"{reason}: mapped content item {item_idx:02d} chunk {chunk_idx + 1:02d}/{len(item_chunks):02d} + {status_label}", reliable=True)
                if self.outbound_packet_delay_seconds > 0:
                    await asyncio.sleep(self.outbound_packet_delay_seconds)

        await self.send_packet(b"#ready#\r", f"{reason}: final #ready# after mapped news content", reliable=True)

    async def send_mail_minimal_response(self, token: bytes, reason: str = "mail token response") -> None:
        """Respond to the ROM mail receive/send stages.

        For non-mail services, this keeps the previous empty-mail behavior:
            #begin mail# / #end mail# / #ready#

        For --service mail, this sends multiple short diagnostic messages
        MAIL-00..MAIL-03 so the Correio Eletronico mailbox behavior can be mapped.
        """
        text = ascii_text(token).strip()
        logging.info("%s MAIL_REQUEST token=%s action=begin-mail-end-ready profile=%s", self.peer, text, self.content_profile)
        if text.lower().startswith("#send mail#"):
            await self.send_packet(b"#begin mail#\r", f"{reason}: #begin mail#", reliable=True)
            if self.outbound_packet_delay_seconds > 0:
                await asyncio.sleep(self.outbound_packet_delay_seconds)

            mail_records = self._mail_receive_records()
            if mail_records:
                for idx, record in enumerate(mail_records):
                    status = b"#end mail#\r" if idx == len(mail_records) - 1 else b"#sep#\r"
                    payload = record + status
                    if len(payload) > 127:
                        logging.error(
                            "%s MAIL_RECORD_TOO_LONG idx=%02d len=%d max=127 preview=%s",
                            self.peer, idx, len(payload), ascii_text(payload[:80]),
                        )
                        raise ValueError(f"mail record {idx:02d} too long: {len(payload)} bytes")
                    logging.info(
                        "%s MAIL_RECORD idx=%02d payload_len=%d status=%s",
                        self.peer, idx, len(payload), status.decode('ascii').strip(),
                    )
                    await self.send_packet(payload, f"{reason}: mail record {idx:02d} + {status.decode('ascii').strip()}", reliable=True)
                    if self.outbound_packet_delay_seconds > 0:
                        await asyncio.sleep(self.outbound_packet_delay_seconds)
            else:
                await self.send_packet(b"#end mail#\r", f"{reason}: empty mail body + #end mail# status", reliable=True)
                if self.outbound_packet_delay_seconds > 0:
                    await asyncio.sleep(self.outbound_packet_delay_seconds)

            await self.send_packet(b"#ready#\r", f"{reason}: final #ready# after mail end", reliable=True)
            if mail_records and self.pending_mailbox_clear_email:
                mailbox_email = self.pending_mailbox_clear_email
                try:
                    cleared, path = self.mail_store.clear_mailbox_messages(
                        mailbox_email,
                        archive=True,
                        reason="rom_download_completed",
                    )
                except (OSError, UnicodeError, ValueError) as exc:
                    # The ROM already received every record and the final #ready#.
                    # A persistence failure here must not turn a completed wire
                    # transfer into a transport/session failure.  Keep the mailbox
                    # pending so a later connection can retry the cleanup safely.
                    logging.warning(
                        "%s MAIL_INBOX_CLEAR_FAILED_AFTER_DOWNLOAD mailbox=%s error=%s action=keep-pending",
                        self.peer,
                        mailbox_email,
                        exc,
                    )
                else:
                    logging.info(
                        "%s MAIL_INBOX_CLEARED_AFTER_DOWNLOAD mailbox=%s cleared=%d path=%s",
                        self.peer,
                        mailbox_email,
                        cleared,
                        path,
                    )
                    self.pending_mailbox_clear_email = None
                    self.pending_mailbox_clear_count = 0
        elif text.lower().startswith("#get mail#"):
            # Outgoing mail upload stage. For now, acknowledge readiness and let
            # subsequent client packets be logged. If the ROM sends #begin mail#
            # and message bodies, those packets will identify the upload format.
            logging.info("%s OUTGOING_MAIL_REQUEST token=%s action=ready-capture", self.peer, text)
            await self.send_packet(b"#ready#\r", f"{reason}: ready after get mail", reliable=True)
        else:
            await self.send_packet(b"#ready#\r", f"{reason}: generic mail ready", reliable=True)

    async def force_close_client_session(self, reason: str, delay: Optional[float] = None) -> None:
        """Close this client socket regardless of --transaction-close mode.

        Used by the mapper when an SSO/auth transaction must end so the ROM can
        return to the Mega Net menu and start a new phone call/activity.
        """
        if self.closed_by_transaction:
            return
        self.closed_by_transaction = True
        delay_value = self.post_id_close_delay_seconds if delay is None else max(0.0, delay)
        logging.info("%s TRANSACTION_FORCE_CLOSE scheduled delay=%.3fs reason=%s", self.peer, delay_value, reason)
        if self.close_task:
            self.close_task.cancel()
        self.close_task = asyncio.create_task(self._force_close_later(delay_value, reason))

    async def _force_close_later(self, delay: float, reason: str) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            logging.info("%s TRANSACTION_FORCE_CLOSE closing client session reason=%s", self.peer, reason)
            try:
                self.writer.close()
            except Exception:
                pass
        except asyncio.CancelledError:
            return

    async def close_transaction(self, reason: str, immediate: bool = False) -> None:
        if self.closed_by_transaction:
            return
        if self.transaction_close == "off":
            logging.info("%s TRANSACTION_CLOSE disabled reason=%s", self.peer, reason)
            return
        if self.transaction_close == "after-bye" and not immediate:
            logging.info("%s TRANSACTION_CLOSE waiting for #bye# reason=%s", self.peer, reason)
            return
        self.closed_by_transaction = True
        delay = 0.0 if immediate else max(0.0, self.transaction_close_delay_seconds)
        logging.info("%s TRANSACTION_CLOSE scheduled delay=%.3fs reason=%s", self.peer, delay, reason)
        if self.close_task:
            self.close_task.cancel()
        self.close_task = asyncio.create_task(self._close_transaction_later(delay, reason))

    async def _close_transaction_later(self, delay: float, reason: str) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            logging.info("%s TRANSACTION_CLOSE closing client session reason=%s", self.peer, reason)
            try:
                self.writer.close()
            except Exception:
                pass
        except asyncio.CancelledError:
            return










    async def save_cadastro_db(self, reason: str) -> None:
        if self.cadastro_db_blocked:
            logging.info("%s cadastro DB not saved at %s: Mega Net auth failed", self.peer, reason)
            return
        if self.cadastro_db_saved:
            return
        record = self.cadastro.as_record()
        if not record:
            logging.info("%s cadastro DB not saved at %s: no fields captured", self.peer, reason)
            return
        user_key, path = self.cadastro_db.upsert_registration(
            session_id=self.session_id,
            peer=self.peer,
            profile_name=ascii_text(bytes(self.profile_buf)),
            bootstrap_login=self.bootstrap_login,
            bootstrap_password=self.bootstrap_password,
            fields=record,
            packet_count=len(self.cadastro.packet_log),
            mail_domain=self.mail_domain,
        )
        self.returning_user_key = user_key
        self.cadastro_db_saved = True
        logging.info("%s cadastro DB saved key=%s path=%s reason=%s", self.peer, user_key, path, reason)

    async def save_outputs(self) -> None:
        if not getattr(self, "enable_session_logs", False):
            logging.info("%s session output not saved: session logs disabled", self.peer)
            return
        self.output_dir.mkdir(parents=True, exist_ok=True)
        record = self.cadastro.as_record()
        saved_at = utc_now_iso()
        payload = {
            "session_id": self.session_id,
            "saved_at": saved_at,
            "peer": self.peer,
            "profile_name": ascii_text(bytes(self.profile_buf)),
            "ppp_auth": {
                "allowed": self.auth_result.allowed if self.auth_result else None,
                "known_user": self.auth_result.known_user if self.auth_result else None,
                "user_key": self.auth_result.user_key if self.auth_result else None,
                "reason": self.auth_result.reason if self.auth_result else None,
                "mode": self.auth_mode,
                "password_source": self.auth_password_source,
            },
            "meganet_auth": {
                "allowed": self.meganet_auth_result.allowed if self.meganet_auth_result else None,
                "known_user": self.meganet_auth_result.known_user if self.meganet_auth_result else None,
                "user_key": self.meganet_auth_result.user_key if self.meganet_auth_result else None,
                "reason": self.meganet_auth_result.reason if self.meganet_auth_result else None,
                "mode": self.meganet_auth_mode,
            },
            "fields": record,
            "packets": self.cadastro.packet_log,
        }
        date_key = str(self.session_id or "").split("_", 1)[0] or _dt.datetime.now().strftime("%Y%m%d")
        daily_base = self.output_dir / f"meganet_sessions_{date_key}"
        json_path = daily_base.with_suffix(".json")
        with DAILY_SESSION_LOG_LOCK:
            aggregate = {"schema": "meganet_daily_sessions_v1", "date": date_key, "sessions": []}
            if json_path.exists():
                try:
                    loaded = json.loads(json_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict) and isinstance(loaded.get("sessions"), list):
                        aggregate = loaded
                except Exception as exc:
                    logging.warning("%s daily session log read failed path=%s error=%s", self.peer, json_path, exc)
            sessions = [x for x in aggregate.get("sessions", []) if isinstance(x, dict) and str(x.get("session_id") or "") != self.session_id]
            sessions.append(payload)
            sessions.sort(key=lambda x: str(x.get("session_id") or ""))
            aggregate["schema"] = "meganet_daily_sessions_v1"
            aggregate["date"] = date_key
            aggregate["updated_at"] = saved_at
            aggregate["session_count"] = len(sessions)
            aggregate["sessions"] = sessions
            atomic_write_json(json_path, aggregate, ensure_ascii=False, indent=2)
        logging.info("%s saved daily session log %s.json session_id=%s", self.peer, daily_base, self.session_id)

