from __future__ import annotations

import asyncio
import logging
import pathlib
from typing import Callable, Optional

from ..common import AsyncByteWriter, utc_now_iso
from .accounts import (
    TELEBRADESCO_ACCOUNTS_SCHEMA, TelebradescoAccountsError,
    authenticate_telebradesco_login, normalize_telebradesco_accounts_content_path,
    parse_telebradesco_login_text, read_telebradesco_accounts,
    record_telebradesco_account_access, telebradesco_account_key,
)
from .content import (
    compile_telebradesco_content, compile_telebradesco_status,
    normalize_telebradesco_block_mode, normalize_telebradesco_error_hold_seconds,
)
from .states import TelebradescoMode, TelebradescoStage
from .protocol import (
    TELEBRADESCO_LOGIN_CONFIRM_MAX_RAW, TELEBRADESCO_LOGIN_FRAME_MAX,
    _tele_class1_wire, _tele_extract_login_confirmation, _tele_login_wire_terminated,
    _tele_reverse_bits8, decode_telebradesco_login_frame,
)

CR = 0x0D
LF = 0x0A

class TelebradescoSession:
    def __init__(
        self,
        writer: AsyncByteWriter,
        *,
        peer: str,
        accounts_file: pathlib.Path,
        accounts_content_file: pathlib.Path,
        block_mode: str = "native-020",
        error_hold_seconds: float = 2.0,
        access_identity_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.writer = writer
        self.peer = peer
        self.access_identity_callback = access_identity_callback
        self.accounts_file = pathlib.Path(accounts_file)
        self.accounts_content_file = normalize_telebradesco_accounts_content_path(pathlib.Path(accounts_content_file))
        self.block_mode = normalize_telebradesco_block_mode(block_mode)
        self.error_hold_seconds = normalize_telebradesco_error_hold_seconds(error_hold_seconds)
        self.accounts_error = ""
        try:
            self.accounts_data = read_telebradesco_accounts(
                self.accounts_file,
                accounts_content_file=self.accounts_content_file,
                create_if_missing=True,
            )
        except TelebradescoAccountsError as exc:
            self.accounts_data = {
                "schema": TELEBRADESCO_ACCOUNTS_SCHEMA,
                "updated_at": utc_now_iso(),
                "accounts": {},
            }
            self.accounts_error = str(exc)
            logging.critical("%s TELE_SESSION_ACCOUNTS_UNAVAILABLE error=%s", self.peer, exc)
        initial = compile_telebradesco_status("001", block_mode=self.block_mode)
        self.auth_wire_code = "001"
        self.frames: list[bytes] = initial["frames"]
        self.command_buf = bytearray()
        self.binary_buf = bytearray()
        self.mode: TelebradescoMode = TelebradescoMode.COMMAND
        self.stage: TelebradescoStage = TelebradescoStage.AWAIT_INITIAL_FRH
        self.ack_context = ""
        self.frame_index = 0
        self.sequence = 2
        self.close_task: Optional[asyncio.Task] = None
        self.login_text = ""
        self.auth_result: Optional[dict] = None
        logging.info(
            "%s TELE_SESSION_INIT accounts=%s account_count=%d accounts_available=%s block_mode=%s error_hold=%.1fs aguardando_login=True",
            self.peer,
            self.accounts_file,
            len(self.accounts_data.get("accounts", {})),
            not bool(self.accounts_error),
            self.block_mode,
            self.error_hold_seconds,
        )

    async def start(self) -> None:
        # O Telebradesco não usa o bootstrap textual login/password do Mega Net.
        # Após o CONNECT fornecido pelo modem local, o cartucho inicia a sessão
        # enviando AT+FRH=3. Portanto, a conexão deve permanecer silenciosa até
        # o primeiro byte vindo do cartucho. Qualquer prompt espontâneo, como
        # login:\r, fica à frente da resposta Class-1 e causa "NÃO ATENDE".
        logging.info("%s TELE_READY aguardando primeiro comando AT sem transmitir dados", self.peer)

    async def send_raw(self, data: bytes, reason: str) -> None:
        logging.debug("%s TELE_TX reason=%s bytes=%s", self.peer, reason, data.hex(" "))
        self.writer.write(data)
        await self.writer.drain()

    async def feed(self, data: bytes) -> None:
        for value in data:
            if self.mode == TelebradescoMode.COMMAND:
                await self.send_raw(bytes([value]), "eco de comando AT")
                if value == CR:
                    command = self.command_buf.decode("latin-1", "replace").strip()
                    self.command_buf.clear()
                    await self.handle_command(command)
                elif value != LF:
                    if len(self.command_buf) < 256:
                        self.command_buf.append(value)
                continue

            self.binary_buf.append(value)
            if self.mode == TelebradescoMode.LOGIN_FRAME:
                # O quadro FTH termina, no fio bit-revertido, com 08 C0
                # (DLE ETX antes da reversão). Não finalize ao encontrar um C0
                # isolado no conteúdo, pois ele pode pertencer ao payload.
                if _tele_login_wire_terminated(self.binary_buf):
                    await self.finish_login_frame()
                elif len(self.binary_buf) >= TELEBRADESCO_LOGIN_FRAME_MAX:
                    logging.warning("%s TELE_LOGIN_FRAME_TOO_LARGE bytes=%d", self.peer, len(self.binary_buf))
                    await self.finish_login_frame()
            elif self.mode == TelebradescoMode.LOGIN_CONFIRM:
                parsed_confirm = _tele_extract_login_confirmation(bytes(self.binary_buf))
                if parsed_confirm is not None:
                    consumed = bytes(parsed_confirm["raw_consumed"])
                    remainder = bytes(parsed_confirm["raw_remainder"])
                    prefix = bytes(parsed_confirm["logical_prefix"])
                    frame = bytes(parsed_confirm["logical_frame"])
                    self.binary_buf.clear()
                    logging.info(
                        "%s TELE_LOGIN_CONFIRM raw=%s prefix=%s frame=%s remainder=%s",
                        self.peer,
                        consumed.hex(" "),
                        prefix.hex(" ") or "-",
                        frame.hex(" "),
                        remainder.hex(" ") or "-",
                    )
                    await self.send_raw(b"0\r", "resultado OK após quadro binário de login completo")
                    self.mode = TelebradescoMode.COMMAND
                    self.stage = TelebradescoStage.AWAIT_HELLO_FRM
                    if remainder:
                        await self.feed(remainder)
                elif len(self.binary_buf) >= TELEBRADESCO_LOGIN_CONFIRM_MAX_RAW:
                    raw = bytes(self.binary_buf)
                    logical = bytes(_tele_reverse_bits8(x) for x in raw)
                    self.binary_buf.clear()
                    logging.warning(
                        "%s TELE_LOGIN_CONFIRM_INVALID raw=%s bitrev=%s action=generic-ok",
                        self.peer,
                        raw.hex(" "),
                        logical.hex(" "),
                    )
                    await self.send_raw(b"0\r", "resultado compatível após confirmação binária não reconhecida")
                    self.mode = TelebradescoMode.COMMAND
                    self.stage = TelebradescoStage.AWAIT_HELLO_FRM
            elif self.mode == TelebradescoMode.ACK3 and len(self.binary_buf) >= 3:
                raw = bytes(self.binary_buf[:3])
                remainder = bytes(self.binary_buf[3:])
                self.binary_buf.clear()
                await self.finish_ack(raw)
                if remainder:
                    await self.feed(remainder)

    async def handle_command(self, command: str) -> None:
        upper = command.upper()
        logging.info("%s TELE_COMMAND stage=%s command=%s", self.peer, self.stage, command)
        if not upper:
            return
        if upper.startswith("ATH"):
            terminal_error = self.auth_wire_code in {"001", "002", "003", "005", "010", "011"}
            if terminal_error and self.error_hold_seconds > 0:
                logging.info(
                    "%s TELE_AUTH_ERROR_HOLD status=%s seconds=%.1f before_ath0_ok=True",
                    self.peer,
                    self.auth_result.get("status") if self.auth_result else "unknown",
                    self.error_hold_seconds,
                )
                await asyncio.sleep(self.error_hold_seconds)
            await self.send_raw(b"0\r", "ATH0 terminal")
            self.stage = TelebradescoStage.FINISHED
            self.close_task = asyncio.create_task(self._close_later(0.30))
            return
        if upper.startswith("AT+FRH=3"):
            await self.send_raw(b"1\r\x10\x03" + b"0\r", "FRH inicial vazio")
            self.stage = TelebradescoStage.AWAIT_LOGIN_FTH
            return
        if upper.startswith("AT+FTH=3"):
            await self.send_raw(b"1\r", "entrada em transmissão FTH")
            self.binary_buf.clear()
            if self.stage == TelebradescoStage.AWAIT_LOGIN_FTH:
                self.mode = TelebradescoMode.LOGIN_FRAME
            else:
                self.mode = TelebradescoMode.ACK3
                self.ack_context = "hello" if self.stage == TelebradescoStage.AWAIT_HELLO_FTH else "data"
            return
        if upper.startswith("AT+FRM=48"):
            if self.stage == TelebradescoStage.AWAIT_HELLO_FRM:
                hello = (b"\x00" * 890) + b"\x01\x0D" + b"3\r"
                await self.send_raw(b"1\r" + hello, "preâmbulo de recepção")
                self.stage = TelebradescoStage.AWAIT_HELLO_FTH
                return
            if self.stage in {TelebradescoStage.AWAIT_DATA_FRM, TelebradescoStage.AWAIT_TERMINAL}:
                if self.frame_index < len(self.frames):
                    wire = _tele_class1_wire(self.frames[self.frame_index], self.sequence)
                    await self.send_raw(b"1\r" + wire, f"quadro {self.frame_index + 1}/{len(self.frames)} seq={self.sequence}")
                    self.stage = TelebradescoStage.AWAIT_DATA_FTH
                else:
                    await self.send_raw(b"3\r", "nenhum quadro adicional")
                    self.stage = TelebradescoStage.AWAIT_TERMINAL
                return
            await self.send_raw(b"3\r", "FRM fora da sequência")
            return
        await self.send_raw(b"0\r", "resultado OK genérico")

    async def finish_login_frame(self) -> None:
        raw = bytes(self.binary_buf)
        self.binary_buf.clear()
        self.frame_index = 0
        self.sequence = 2
        self.ack_context = ""

        try:
            decoded_login = decode_telebradesco_login_frame(raw)
            login_text = str(decoded_login["login_text"])
            logging.info(
                "%s TELE_LOGIN_FRAME_OK bytes=%d format=%s header=%s payload_len=%d",
                self.peer,
                len(raw),
                decoded_login.get("frame_format", "unknown"),
                bytes(decoded_login.get("header", b"")).hex(" "),
                len(decoded_login["payload"]),
            )
        except Exception as exc:
            login_text = ""
            logging.warning("%s TELE_LOGIN_FRAME_INVALID bytes=%d error=%s", self.peer, len(raw), exc)

        self.login_text = login_text
        if self.accounts_error:
            self.auth_result = {
                "status": "accounts_unavailable",
                "status_code": "001",
                "credentials": parse_telebradesco_login_text(login_text),
                "account": None,
            }
        else:
            self.auth_result = authenticate_telebradesco_login(self.accounts_data, login_text)
        status = self.auth_result["status"]
        credentials = self.auth_result.get("credentials") or {}
        account = self.auth_result.get("account")

        if status == "ok" and isinstance(account, dict):
            try:
                access_key = str(credentials.get("key") or telebradesco_account_key(credentials.get("agencia", ""), credentials.get("conta", ""), credentials.get("digito", "")))
                access_result = record_telebradesco_account_access(
                    self.accounts_file,
                    access_key,
                    peer=self.peer,
                    login_text=self.login_text,
                    accounts_content_file=self.accounts_content_file,
                )
                if access_result:
                    account["total_accesses"] = access_result.get("total_accesses", account.get("total_accesses", 0))
                    account["last_access_at"] = access_result.get("last_access_at", account.get("last_access_at", ""))
                    logging.info("%s TELE_ACCESS_RECORDED account=%s total_accesses=%s", self.peer, access_key, access_result.get("total_accesses"))
            except Exception as exc:
                logging.warning("%s TELE_ACCESS_RECORD_FAILED error=%s", self.peer, exc)
            if self.access_identity_callback:
                try:
                    identification = str(account.get("nome") or "").strip()
                    if not identification:
                        identification = telebradesco_account_key(
                            credentials.get("agencia", ""),
                            credentials.get("conta", ""),
                            credentials.get("digito", ""),
                        )
                    self.access_identity_callback(identification)
                except Exception as exc:
                    logging.debug("%s DASHBOARD_IDENTITY_UPDATE_FAILED service=telebradesco error=%s", self.peer, exc)
            requested_data_cartao = credentials.get("data_cartao", "")
            compiled = compile_telebradesco_content(
                account.get("content", {}),
                data_cartao=requested_data_cartao,
            )
            selected_data_cartao = compiled.get("selected_data_cartao") or ""
            if selected_data_cartao:
                logging.info(
                    "%s TELE_CARD_SELECTED data_cartao=%s section=6",
                    self.peer,
                    selected_data_cartao,
                )
            elif requested_data_cartao:
                logging.warning(
                    "%s TELE_CARD_NOT_FOUND data_cartao=%s section=6 action=omit",
                    self.peer,
                    requested_data_cartao,
                )
            else:
                logging.info(
                    "%s TELE_CARD_NOT_REQUESTED data_cartao=0000 section=6 action=omit",
                    self.peer,
                )
            self.frames = compiled["frames"]
            # Successful authentication carries 999 in all status slots.  Keep
            # auth_wire_code in sync so ATH0 does not enter the authentication-
            # error hold path after a completed successful download.
            self.auth_wire_code = str(compiled.get("status_slots", ("999",))[0] or "999")
            logging.info(
                "%s TELE_AUTH_OK agencia=%s conta=%s digito=%s restante_prefixo=%s data_cartao_raw=%s data_cartao=%s selected_data_cartao=%s card_section=%s account_name=%s frames=%d stream=%d",
                self.peer,
                credentials.get("agencia"),
                credentials.get("conta"),
                credentials.get("digito"),
                credentials.get("restante_prefixo", ""),
                credentials.get("data_cartao_raw", ""),
                requested_data_cartao or "<none>",
                compiled.get("selected_data_cartao") or "<none>",
                "sent" if "6" not in compiled.get("omitted_sections", ()) else "omitted",
                account.get("nome", ""),
                len(self.frames),
                compiled["stream_size"],
            )
        else:
            status_code = str(self.auth_result.get("status_code") or "001")
            # Rejected authentication never receives real account content.
            # Native blocking uses a status-only envelope, with code 020 in
            # slots 0 and 1 and sections 2 and 9 absent.
            native_content = None
            compiled_status = compile_telebradesco_status(
                status_code,
                native_content,
                block_mode=self.block_mode,
            )
            self.frames = compiled_status["frames"]
            self.auth_wire_code = compiled_status["wire_status_code"]
            if status == "blocked" and compiled_status["block_mode"] == "native-020":
                logging.warning(
                    "%s TELE_NATIVE_BLOCK_ARMED triggers=conta_corrente_saldo,poupanca_saldo sections=2,9 status_slots=0,1 content_payload_bytes=0 structural_section=5",
                    self.peer,
                )
            if status == "invalid_agency":
                matched = self.auth_result.get("matched_account") or {}
                logging.warning(
                    "%s TELE_NATIVE_INVALID_AGENCY_ARMED requested_agencia=%s registered_agencia=%s conta=%s digito=%s status_slots=0,1,2 code=040 omitted_sections=1,2,3,6,8,9,A structural_section=5",
                    self.peer,
                    credentials.get("agencia", "?"),
                    matched.get("agencia", "?"),
                    credentials.get("conta", "?"),
                    credentials.get("digito", "?"),
                )
            if status == "account_not_found":
                logging.warning(
                    "%s TELE_NATIVE_ACCOUNT_NOT_FOUND_ARMED agencia=%s conta=%s digito=%s status_slots=0,1,2 code=042 omitted_sections=1,2,3,6,8,9,A structural_section=5",
                    self.peer,
                    credentials.get("agencia", "?"),
                    credentials.get("conta", "?"),
                    credentials.get("digito", "?"),
                )
            logging.warning(
                "%s TELE_AUTH_REJECT status=%s logical_code=%s wire_code=%s block_mode=%s slots=%s empty_sections=%s omitted_sections=%s agencia=%s conta=%s digito=%s frames=%d stream=%d",
                self.peer,
                status,
                status_code,
                compiled_status["wire_status_code"],
                compiled_status["block_mode"],
                "/".join(compiled_status["status_slots"]),
                ",".join(compiled_status["empty_sections"]) or "-",
                ",".join(compiled_status["omitted_sections"]) or "-",
                credentials.get("agencia", "?"),
                credentials.get("conta", "?"),
                credentials.get("digito", "?"),
                len(self.frames),
                compiled_status["stream_size"],
            )

        # The transport connects normally. Authentication feedback is carried
        # by the mapped application code inside the downloaded data stream.
        await self.send_raw(b"1\r", "CONNECT após quadro MEGABRA")
        self.mode = TelebradescoMode.LOGIN_CONFIRM
        self.stage = TelebradescoStage.AWAIT_LOGIN_CONFIRM

    async def finish_ack(self, raw: bytes) -> None:
        decoded = bytes(_tele_reverse_bits8(value) for value in raw)
        code = decoded[-1] if decoded else 0
        logging.info(
            "%s TELE_ACK context=%s raw=%s decoded=%s code=%02X frame=%d seq=%d",
            self.peer, self.ack_context, raw.hex(" "), decoded.hex(" "), code, self.frame_index, self.sequence,
        )
        await self.send_raw(b"0\r", "resultado OK após ACK/NAK")
        self.mode = TelebradescoMode.COMMAND
        if self.ack_context == "hello":
            self.stage = TelebradescoStage.AWAIT_DATA_FRM
            return
        if code == 0x22:
            # 0x22 requests retransmission of the current Class-1 frame.
            # The sequence belongs to the frame and must remain unchanged;
            # advancing it turns the retry into a new frame and leaves a gap
            # in the cartridge receiver state.
            logging.info(
                "%s TELE_DATA_RETRY frame=%d seq=%d reason=22",
                self.peer, self.frame_index, self.sequence,
            )
            self.stage = TelebradescoStage.AWAIT_DATA_FRM
            return
        if code == 0x21:
            self.sequence = (self.sequence + 1) & 0xFF
            self.frame_index += 1
            self.stage = TelebradescoStage.AWAIT_TERMINAL if self.frame_index >= len(self.frames) else TelebradescoStage.AWAIT_DATA_FRM
            return
        logging.warning("%s TELE_ACK_UNKNOWN code=%02X; mantendo quadro atual", self.peer, code)
        self.stage = TelebradescoStage.AWAIT_DATA_FRM

    async def _close_later(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            self.writer.close()
        except asyncio.CancelledError:
            return
        except (ConnectionError, OSError) as exc:
            logging.debug("%s TELE_CLOSE_IGNORED error=%s", self.peer, exc)
