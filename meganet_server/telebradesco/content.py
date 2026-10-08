from __future__ import annotations

import copy
import datetime as _dt
import json
import logging
import os
import pathlib
import re
import tempfile
from typing import Optional

from ..common import utc_now_iso

TELEBRADESCO_DATA_CARTAO_DIGITS = 4
TELEBRADESCO_LOGIN_REMAINING_DIGITS = 8


def _tele_digits(value: object, length: int, label: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) != length:
        raise ValueError(f"{label}: informe exatamente {length} algarismo(s).")
    return digits

TELEBRADESCO_SECTION_MARKER = bytes([0x1D, 0x03])
TELEBRADESCO_CONTROL_999 = bytes([0x01, 0x30, 0x39, 0x39, 0x39, 0x04, 0x00])
TELEBRADESCO_FRAME_PAYLOAD_MAX = 121
TELEBRADESCO_FRAME_USEFUL_BYTES = 120
TELEBRADESCO_CARD_IDENTIFIER_FIELD = "data_cartao"
def _tele_ascii(value: object) -> str:
    text = str(value or "")
    replacements = {
        "á":"a", "à":"a", "â":"a", "ã":"a", "ä":"a", "Á":"A", "À":"A", "Â":"A", "Ã":"A", "Ä":"A",
        "é":"e", "ê":"e", "ë":"e", "É":"E", "Ê":"E", "Ë":"E",
        "í":"i", "ï":"i", "Í":"I", "Ï":"I",
        "ó":"o", "ô":"o", "õ":"o", "ö":"o", "Ó":"O", "Ô":"O", "Õ":"O", "Ö":"O",
        "ú":"u", "ü":"u", "Ú":"U", "Ü":"U",
        "ç":"c", "Ç":"C",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    return text.encode("ascii", "replace").decode("ascii")
def _tele_fixed(value: object, length: int, *, align: str = "left") -> bytes:
    raw = _tele_ascii(value).encode("ascii", "replace")
    if len(raw) > length:
        raw = raw[:length]
    padding = b" " * (length - len(raw))
    return padding + raw if align == "right" else raw + padding
def _tele_date_digits(value: object, length: int) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if length == 4:
        if len(digits) >= 4:
            return digits[:4]
        return digits.ljust(4, "0")
    if length == 6:
        if len(digits) >= 8:
            return digits[:4] + digits[-2:]
        if len(digits) >= 6:
            return digits[:6]
        return digits.ljust(6, "0")
    raise ValueError(f"Tamanho de data Telebradesco não suportado: {length}")
def _tele_card_date(value: object) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) >= 8:
        digits = digits[:4] + digits[-2:]
    elif len(digits) < 6:
        digits = digits.ljust(6, "0")
    else:
        digits = digits[:6]
    return f"{digits[0:2]}/{digits[2:4]}/{digits[4:6]}"
def _tele_transaction(record: dict) -> bytes:
    return (
        _tele_fixed(_tele_date_digits(record.get("data"), 4), 4)
        + _tele_fixed(record.get("historico") or record.get("descricao"), 15)
        + _tele_fixed(record.get("documento"), 7)
        + _tele_fixed(record.get("valor"), 19, align="right")
    )
def _tele_summary(entries: list[dict]) -> bytes:
    """Encode the original 26-byte summary layout used by section A."""
    source = entries if isinstance(entries, list) else []
    normalized = [item if isinstance(item, dict) else {} for item in source[:3]]
    while len(normalized) < 3:
        normalized.append({"data": "00/00/00", "valor": "0,00"})
    return b"".join(
        _tele_fixed(_tele_date_digits(item.get("data"), 6), 6)
        + _tele_fixed(item.get("valor"), 20, align="right")
        for item in normalized
    )
def _tele_current_account_summary(entries: list[dict]) -> bytes:
    """Encode section 1 using the original 26-byte date/value layout.

    The visible blank line between the date and the value on the cartridge is
    normal.  What must be preserved is the value aligned to the same right-side
    column occupied by the date.  Therefore each item keeps the confirmed
    6-byte date followed by a 20-byte right-aligned value field.
    """
    source = entries if isinstance(entries, list) else []
    normalized = [item if isinstance(item, dict) else {} for item in source[:3]]
    while len(normalized) < 3:
        normalized.append({"data": "00/00/00", "valor": "0,00"})
    return b"".join(
        _tele_fixed(_tele_date_digits(item.get("data"), 6), 6)
        + _tele_fixed(item.get("valor"), 20, align="right")
        for item in normalized
    )
def _tele_type5(section: dict, records_key: str) -> bytes:
    records = section.get(records_key, []) if isinstance(section, dict) else []
    if not isinstance(records, list):
        records = []
    return (
        _tele_fixed(section.get("titulo", ""), 32)
        + _tele_fixed(section.get("texto_auxiliar", ""), 28)
        + _tele_fixed(_tele_date_digits(section.get("data_saldo"), 6), 6)
        + _tele_fixed(section.get("saldo", "0,00"), 20, align="right")
        + b"".join(_tele_transaction(item if isinstance(item, dict) else {}) for item in records)
    )
def _tele_type2(account: dict, investments: list[dict]) -> bytes:
    """Encode the confirmed section 2/9 balance and product-list layout.

    Physical validation of proved that the fourth 18-byte field is not
    the visible screen title.  Keep it reserved and blank exactly as in the
    known-good payload.  The ROM takes the visible Conta Corrente/Investimentos
    title from the 32-byte title at the start of section 5.
    """
    data = (
        _tele_fixed(account.get("saldo_disponivel", "0,00"), 20, align="right")
        + _tele_fixed(account.get("saldo_vinculado", "0,00"), 18, align="right")
        + _tele_fixed(account.get("saldo_total", "0,00"), 20, align="right")
        + b" " * 18
    )
    for item in investments or []:
        item = item if isinstance(item, dict) else {}
        data += _tele_fixed(item.get("descricao"), 15)
        data += _tele_fixed(item.get("valor"), 18)
    return data
def _tele_type3(records: list[dict]) -> bytes:
    return b"".join(_tele_transaction(item if isinstance(item, dict) else {}) for item in (records or []))
def _tele_normalize_card_identifier(value: object) -> str:
    """Normalize the four-digit DDMM Data Cartão selector.

    Real trace evidence shows that the ROM sends an eight-digit tail after the
    password, but Data Cartão occupies only its last four digits. Accepting an
    old eight-digit value here is migration compatibility only: its final DDMM
    is retained. ``0000`` means that no card was requested.
    """
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == TELEBRADESCO_LOGIN_REMAINING_DIGITS:
        digits = digits[-TELEBRADESCO_DATA_CARTAO_DIGITS:]
    if not digits or set(digits) == {"0"}:
        return ""
    return digits
def _tele_card_purchases(card: object) -> list[dict]:
    if not isinstance(card, dict):
        return []
    purchases = card.get("compras", [])
    if not isinstance(purchases, list):
        return []
    return [item for item in purchases if isinstance(item, dict)]
def _tele_registered_cards(content: object) -> list[dict]:
    if not isinstance(content, dict):
        return []
    cards = content.get("cartoes", [])
    return [item for item in cards if isinstance(item, dict)] if isinstance(cards, list) else []
def _tele_find_card(content: object, data_cartao: object) -> Optional[dict]:
    """Return the card whose Data Cartão exactly matches the login selector."""
    requested = _tele_normalize_card_identifier(data_cartao)
    if len(requested) != TELEBRADESCO_DATA_CARTAO_DIGITS:
        return None
    for card in _tele_registered_cards(content):
        stored = _tele_normalize_card_identifier(card.get(TELEBRADESCO_CARD_IDENTIFIER_FIELD))
        if stored == requested:
            return card
    return None
def _tele_type6(card: dict) -> bytes:
    """Encode one already-selected credit card into section 6."""
    if not isinstance(card, dict):
        return b""
    brand = _tele_ascii(card.get("bandeira", "VISA")).upper().strip()
    final = re.sub(r"\D", "", str(card.get("final", "1234")))[-4:].rjust(4, "0")
    title = f"{brand} FINAL {final}".strip()
    header = _tele_fixed(title, 32) + b" " * 3 + b" " * 23
    records = []
    for item in _tele_card_purchases(card):
        records.append(
            b"@@@"
            + _tele_fixed(_tele_card_date(item.get("data")), 16)
            + _tele_fixed(item.get("descricao"), 28)
            + _tele_fixed(item.get("valor"), 20)
        )
    return header + b"".join(records)
def _tele_compress(data: bytes) -> bytes:
    out = bytearray()
    index = 0
    while index < len(data):
        value = data[index]
        if value not in (0x20, 0x30):
            out.append(value)
            index += 1
            continue
        end = index + 1
        while end < len(data) and data[end] == value:
            end += 1
        run = end - index
        while run:
            count = min(run, 15)
            if count == 1:
                out.append(value)
            else:
                out.append((0x90 if value == 0x20 else 0x80) | count)
            run -= count
        index = end
    return bytes(out)
def _tele_marker(section_type: str) -> bytes:
    return TELEBRADESCO_SECTION_MARKER + _tele_ascii(section_type)[:1].encode("ascii")
def default_telebradesco_content() -> dict:
    return {
        "schema": "telebradesco_content_v5",
        "updated_at": utc_now_iso(),
        "conta_corrente": {
            "titulo": "CONTA CORRENTE",
            "texto_auxiliar": "DADOS DE DEMONSTRACAO",
            "data_saldo": "13/06/26",
            "saldo": "1234,56",
            "saldo_disponivel": "1234,56",
            "saldo_vinculado": "100,00",
            "saldo_total": "1334,56",
            "extrato": [
                {"data": "13/06", "historico": "CREDITO TESTE", "documento": "0000001", "valor": "1234,56"}
            ],
            "ultimos_lancamentos": [
                {"data": "13/06/26", "valor": "1334,56"},
                {"data": "14/06/26", "valor": "100,00"},
                {"data": "15/06/26", "valor": "1434,56"}
            ],
            "lancamentos_futuros": [
                {"data": "13/06", "historico": "CREDITO TESTE", "documento": "0000001", "valor": "1234,56"}
            ]
        },
        "investimentos": [
            {"descricao": "CDB TESTE 001", "valor": "R$ 1.111,11"},
            {"descricao": "FUNDO TESTE 02", "valor": "R$ 2.222,22"},
            {"descricao": "POUPANCA TESTE", "valor": "R$ 3.333,33"}
        ],
        "poupanca": {
            "titulo": "POUPANCA",
            "texto_auxiliar": "DADOS DE DEMONSTRACAO",
            "data_saldo": "13/06/26",
            "saldo": "987,65",
            "saldo_disponivel": "987,65",
            "saldo_vinculado": "0,00",
            "saldo_total": "987,65",
            "extrato_total": [
                {"data": "13/06", "historico": "CREDITO POUP", "documento": "0000002", "valor": "50,00"}
            ],
            "ultimos_lancamentos": [
                {"data": "13/06", "historico": "CREDITO POUP", "documento": "0000002", "valor": "50,00"}
            ],
            "resumo": [
                {"data": "13/06/26", "valor": "987,65"},
                {"data": "14/06/26", "valor": "0,00"},
                {"data": "15/06/26", "valor": "987,65"}
            ]
        },
        # Credit-card data is selected exclusively by the last four DDMM
        # digits of the MEGABRA tail. New accounts start without cards.
        "cartoes": []
    }
def blank_telebradesco_content() -> dict:
    """Return an intentionally empty Telebradesco content structure.

    Used by the administrative interface for new accounts and for the
    "Limpar dados da conta" action. Authentication/account metadata is
    preserved separately; this structure clears only the financial/product
    content displayed by the cartucho.
    """
    return {
        "schema": "telebradesco_content_v5",
        "updated_at": utc_now_iso(),
        "conta_corrente": {
            "titulo": "",
            "texto_auxiliar": "",
            "data_saldo": "",
            "saldo": "",
            "saldo_disponivel": "",
            "saldo_vinculado": "",
            "saldo_total": "",
            "extrato": [],
            "ultimos_lancamentos": [],
            "lancamentos_futuros": [],
        },
        "investimentos": [],
        "poupanca": {
            "titulo": "",
            "texto_auxiliar": "",
            "data_saldo": "",
            "saldo": "",
            "saldo_disponivel": "",
            "saldo_vinculado": "",
            "saldo_total": "",
            "extrato_total": [],
            "ultimos_lancamentos": [],
            "resumo": [],
        },
        "cartoes": [],
    }
def normalize_telebradesco_content(value: object) -> dict:
    """Normalize content and migrate older account/card schemas.

    Schema v1 inverted the two current-account list names. Schema v2 corrected
    those names but stored only one card and incorrectly treated purchase dates
    as the card selector. Schema v3 introduced ``cartoes`` with an incorrect eight-digit
    selector. Schema v4 stores the confirmed four-digit DDMM ``data_cartao``.
    """
    defaults = default_telebradesco_content()
    if not isinstance(value, dict):
        return defaults

    result = defaults
    source_schema = str(value.get("schema") or "telebradesco_content_v1")
    supported_schemas = {
        "telebradesco_content_v1",
        "telebradesco_content_v2",
        "telebradesco_content_v3",
        "telebradesco_content_v4",
        "telebradesco_content_v5",
    }
    if source_schema not in supported_schemas:
        raise ValueError(f"Schema de conteúdo Telebradesco não suportado: {source_schema}")

    cc_value = value.get("conta_corrente")
    if isinstance(cc_value, dict):
        # Scalar fields retain their original meanings. Collection fields are
        # copied only when their JSON shape is valid; malformed partial files
        # fall back to the empty lists from the default structure.
        for key in (
            "titulo", "texto_auxiliar", "data_saldo", "saldo",
            "saldo_disponivel", "saldo_vinculado", "saldo_total",
        ):
            if key in cc_value:
                result["conta_corrente"][key] = cc_value[key]
        if isinstance(cc_value.get("extrato"), list):
            result["conta_corrente"]["extrato"] = cc_value["extrato"]

        if source_schema in {"telebradesco_content_v2", "telebradesco_content_v3", "telebradesco_content_v4", "telebradesco_content_v5"}:
            if isinstance(cc_value.get("ultimos_lancamentos"), list):
                result["conta_corrente"]["ultimos_lancamentos"] = cc_value["ultimos_lancamentos"]
            if isinstance(cc_value.get("lancamentos_futuros"), list):
                result["conta_corrente"]["lancamentos_futuros"] = cc_value["lancamentos_futuros"]
        else:
            # Automatic, lossless migration from the inverted names.
            if isinstance(cc_value.get("lancamentos_futuros"), list):
                result["conta_corrente"]["ultimos_lancamentos"] = cc_value["lancamentos_futuros"]
            if isinstance(cc_value.get("ultimos_lancamentos"), list):
                result["conta_corrente"]["lancamentos_futuros"] = cc_value["ultimos_lancamentos"]

    savings_value = value.get("poupanca")
    if isinstance(savings_value, dict):
        for key in (
            "titulo", "texto_auxiliar", "data_saldo", "saldo",
            "saldo_disponivel", "saldo_vinculado", "saldo_total",
        ):
            if key in savings_value:
                result["poupanca"][key] = savings_value[key]
        for key in ("extrato_total", "ultimos_lancamentos", "resumo"):
            if isinstance(savings_value.get(key), list):
                result["poupanca"][key] = savings_value[key]

    # Schema stores zero or more cards selected by a four-digit DDMM.
    # eight-digit identifiers are migrated by retaining their final four
    # digits, matching the position confirmed in the real MEGABRA login trace.
    # A legacy single-card object remains unassigned unless it carries an
    # explicit identifier; unassigned data is never transmitted.
    cards: list[dict] = []
    source_cards = value.get("cartoes")
    if isinstance(source_cards, list):
        for raw_card in source_cards:
            if not isinstance(raw_card, dict):
                continue
            migrated = copy.deepcopy(raw_card)
            migrated[TELEBRADESCO_CARD_IDENTIFIER_FIELD] = _tele_normalize_card_identifier(
                migrated.get(TELEBRADESCO_CARD_IDENTIFIER_FIELD)
            )
            cards.append(migrated)
    else:
        legacy_card = value.get("cartao")
        if isinstance(legacy_card, dict) and any(
            legacy_card.get(key) not in (None, "", [], False)
            for key in ("data_cartao", "bandeira", "final", "compras")
        ):
            migrated = copy.deepcopy(legacy_card)
            migrated.pop("data_cartao_informada", None)
            migrated[TELEBRADESCO_CARD_IDENTIFIER_FIELD] = _tele_normalize_card_identifier(
                migrated.get(TELEBRADESCO_CARD_IDENTIFIER_FIELD)
            )
            cards = [migrated]
    result["cartoes"] = cards
    result.pop("cartao", None)

    if isinstance(value.get("investimentos"), list):
        result["investimentos"] = value["investimentos"]

    result["schema"] = "telebradesco_content_v5"
    result["updated_at"] = utc_now_iso()
    return result
def read_telebradesco_content(path: pathlib.Path, *, create_if_missing: bool = True) -> dict:
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            normalized = normalize_telebradesco_content(raw)
            if create_if_missing and isinstance(raw, dict) and raw.get("schema") != "telebradesco_content_v5":
                try:
                    write_telebradesco_content(path, normalized)
                except OSError as exc:
                    logging.warning(
                        "TELE_CONTENT_MIGRATION_WRITE_FAILED path=%s from=%s error=%s",
                        path,
                        raw.get("schema") or "telebradesco_content_v1",
                        exc,
                    )
                else:
                    logging.info(
                        "TELE_CONTENT_MIGRATED path=%s from=%s to=telebradesco_content_v5",
                        path,
                        raw.get("schema") or "telebradesco_content_v1",
                    )
            return normalized
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logging.warning("TELE_CONTENT_READ_FAILED path=%s error=%s", path, exc)
            # Existing content may be malformed or from a newer schema.  It is
            # safer to degrade to the default in memory than to overwrite the
            # only recoverable copy merely because create_if_missing is enabled.
            return default_telebradesco_content()
    content = default_telebradesco_content()
    if create_if_missing:
        write_telebradesco_content(path, content)
    return content
def write_telebradesco_content(path: pathlib.Path, content: dict) -> None:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = normalize_telebradesco_content(content)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = pathlib.Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(normalized, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
def _tele_normalize_status_slots(status_slots: tuple[str, str, str] | list[str] | None = None) -> tuple[str, str, str]:
    """Return the three application-status slots expected at the stream tail.

    A successful download ends with exactly three ``999`` records.  Error
    status must replace the corresponding slot; appending an extra record
    changes the grammar consumed by the cartridge after the content marker.
    """
    if status_slots is None:
        status_slots = ("999", "999", "999")
    if len(status_slots) != 3:
        raise ValueError("O trailer Telebradesco exige exatamente três códigos de estado.")
    return tuple(_tele_digits(code, 3, "Código de estado Telebradesco") for code in status_slots)
TELEBRADESCO_BLOCK_MODES = {"native-020", "deny-login"}
def normalize_telebradesco_block_mode(value: object) -> str:
    """Normalize the blocked-account behavior for the untouched original ROM.

    ``native-020`` preserves the cartridge's own semantics: login completes,
    code 020 is stored in the Conta Corrente and Poupanca status slots, no
    account content is transmitted, and the ROM displays ``C/C BLOQUEADA``
    when section 2 or section 9 is requested.

    ``deny-login`` maps the logical block to terminal code 010, which rejects
    login immediately but uses the ROM's fixed ``SENHA NAO GRAVADA`` text.
    """
    mode = str(value or "native-020").strip().lower()
    if mode not in TELEBRADESCO_BLOCK_MODES:
        logging.warning("TELE_BLOCK_MODE_INVALID value=%r fallback=native-020", value)
        return "native-020"
    return mode
def normalize_telebradesco_error_hold_seconds(value: object) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = 2.0
    return max(0.0, min(5.0, seconds))
def _tele_auth_wire_code(status_code: str, block_mode: object = "native-020") -> str:
    """Map logical authentication state to the code sent to the original ROM."""
    logical = _tele_digits(status_code, 3, "Código lógico de autenticação Telebradesco")
    mode = normalize_telebradesco_block_mode(block_mode)
    if logical == "020":
        return "020" if mode == "native-020" else "010"
    if logical == "040":
        return "040"
    if logical == "042":
        return "042"
    if logical in {"999", "001", "010", "011"}:
        return logical
    return "001"
def _tele_auth_status_slots(
    status_code: str,
    block_mode: object = "native-020",
) -> tuple[str, str, str]:
    logical = _tele_digits(status_code, 3, "Código lógico de autenticação Telebradesco")
    mode = normalize_telebradesco_block_mode(block_mode)
    wire_code = _tele_auth_wire_code(logical, mode)
    if wire_code == "999":
        return ("999", "999", "999")
    if logical == "020" and mode == "native-020":
        # FF353C selects slot 0 for Conta Corrente and slot 1 for Poupanca.
        # The original ROM displays C/C BLOQUEADA for operation 2 or 9 when
        # the corresponding section is absent and the selected slot is 020.
        return ("020", "020", "999")
    if logical == "040":
        # Agência incorreta invalidates the whole customer context, not only
        # one product group. Store 040 in all three slots so any missing
        # operation section can reach the ROM's AGENCIA INCORRETA handler.
        return ("040", "040", "040")
    if logical == "042":
        # Conta inexistente follows the same native-operation path: omit the
        # data sections and let the missing section lookup dispatch slot 0/1/2.
        return ("042", "042", "042")
    return (wire_code, "999", "999")
def _tele_frames_preserving_atomic_spans(
    raw_stream: bytes,
    atomic_spans: list[tuple[int, int]],
) -> list[bytes]:
    """Split a Telebradesco stream without cutting protocol atoms.

    The ROM accepts arbitrary section payload bytes across V.27 data frames, but
    section markers and the 7-byte application-status records are parser atoms.
    Splitting one of those records at a 120-byte frame boundary can make the ROM
    consume enough content to enter the menus and then stop acknowledging the
    transport.  Move only the affected boundary backwards; the byte stream itself
    is unchanged.
    """
    if not raw_stream:
        return [b" "]

    normalized: list[tuple[int, int]] = []
    stream_len = len(raw_stream)
    for start, end in atomic_spans:
        start = max(0, int(start))
        end = min(stream_len, int(end))
        if end <= start:
            continue
        if end - start > TELEBRADESCO_FRAME_USEFUL_BYTES:
            raise ValueError(
                "Um átomo do protocolo Telebradesco excedeu o payload útil de um quadro."
            )
        normalized.append((start, end))
    normalized.sort()

    frames: list[bytes] = []
    offset = 0
    while offset < stream_len:
        end = min(offset + TELEBRADESCO_FRAME_USEFUL_BYTES, stream_len)
        original_end = end
        if end < stream_len:
            for atom_start, atom_end in normalized:
                if atom_start < end < atom_end:
                    end = atom_start
                    break
        if end <= offset:
            # This should be impossible because protocol atoms are at most 7
            # bytes, but fail closed rather than emitting an empty frame.
            raise ValueError(
                f"Não foi possível posicionar uma fronteira segura de quadro Telebradesco em {offset}."
            )
        if end != original_end:
            logging.debug(
                "TELE_FRAME_BOUNDARY_SHIFT offset=%d original_end=%d safe_end=%d shifted_bytes=%d",
                offset,
                original_end,
                end,
                original_end - end,
            )
        frames.append(raw_stream[offset:end] + b" ")
        offset = end

    return frames
def compile_telebradesco_content(
    content: dict,
    *,
    data_cartao: object = "",
    status_slots: tuple[str, str, str] | list[str] | None = None,
    empty_sections: set[str] | tuple[str, ...] | list[str] | None = None,
    omitted_sections: set[str] | tuple[str, ...] | list[str] | None = None,
) -> dict:
    content = normalize_telebradesco_content(content)
    normalized_status_slots = _tele_normalize_status_slots(status_slots)
    cc = content["conta_corrente"]
    savings = content["poupanca"]
    requested_card_identifier = _tele_normalize_card_identifier(data_cartao)
    selected_card = _tele_find_card(content, requested_card_identifier)
    card_present = isinstance(selected_card, dict)
    investments = content.get("investimentos", [])

    # A validação no cartucho físico confirmou que não existe um cabeçalho
    # independente para Investimentos no conteúdo baixado. A seção 5 mantém
    # exclusivamente o título configurado em Conta Corrente.
    sections = {
        "5": _tele_type5(cc, "extrato"),
        "2": _tele_type2(
            cc,
            investments if isinstance(investments, list) else [],
        ),
        # Section 3 keeps the confirmed 45-byte transaction layout and its
        # 19-byte right-aligned value field. Section 1 uses the original 26-byte
        # layout: 6-byte date plus 20-byte right-aligned value.
        "3": _tele_type3(cc.get("lancamentos_futuros", [])),
        "1": _tele_current_account_summary(cc.get("ultimos_lancamentos", [])),
        "6": _tele_type6(selected_card) if card_present else b"",
        "8": _tele_type5({
            "titulo": savings.get("titulo", "POUPANCA"),
            "texto_auxiliar": savings.get("texto_auxiliar", ""),
            "data_saldo": savings.get("data_saldo", "00/00/00"),
            "saldo": savings.get("saldo", "0,00"),
            "extrato_total": savings.get("extrato_total", []),
        }, "extrato_total"),
        "9": _tele_type2(savings, []),
        "A": _tele_summary(savings.get("resumo", [])) + _tele_type3(savings.get("ultimos_lancamentos", [])),
    }

    requested_empty_sections = {
        str(section).upper() for section in (empty_sections or ())
    }
    requested_omitted_sections = {
        str(section).upper() for section in (omitted_sections or ())
    }
    if not card_present:
        # Section 6 is selected exclusively by the final four DDMM digits
        # of the MEGABRA tail. No selector, 0000, or an unknown identifier omits both
        # the payload and the section marker, even when cards exist in JSON.
        requested_empty_sections.discard("6")
        requested_omitted_sections.add("6")
    unknown_empty_sections = requested_empty_sections.difference(sections)
    if unknown_empty_sections:
        raise ValueError(
            "Seções Telebradesco desconhecidas para esvaziamento: "
            + ", ".join(sorted(unknown_empty_sections))
        )
    unknown_omitted_sections = requested_omitted_sections.difference(sections)
    if unknown_omitted_sections:
        raise ValueError(
            "Seções Telebradesco desconhecidas para omissão: "
            + ", ".join(sorted(unknown_omitted_sections))
        )
    overlap = requested_empty_sections.intersection(requested_omitted_sections)
    if overlap:
        raise ValueError(
            "Uma seção Telebradesco não pode ser esvaziada e omitida ao mesmo tempo: "
            + ", ".join(sorted(overlap))
        )
    for section_type in requested_empty_sections.union(requested_omitted_sections):
        sections[section_type] = b""

    stream = bytearray()
    atomic_spans: list[tuple[int, int]] = []

    def append_atomic(blob: bytes) -> None:
        start = len(stream)
        stream.extend(blob)
        atomic_spans.append((start, len(stream)))

    first = True
    for section_type in ("5", "2", "3", "1", "6", "8", "9", "A"):
        if section_type in requested_omitted_sections:
            continue
        packed = _tele_compress(sections[section_type])
        if first:
            append_atomic(bytes([0x01, ord(section_type)]))
            first = False
        else:
            append_atomic(_tele_marker(section_type))
        stream.extend(packed)
    append_atomic(_tele_marker("0"))
    for status_code in normalized_status_slots:
        append_atomic(_tele_control_record(status_code))

    raw_stream = bytes(stream)
    frames = _tele_frames_preserving_atomic_spans(raw_stream, atomic_spans)
    if any(len(frame) > TELEBRADESCO_FRAME_PAYLOAD_MAX for frame in frames):
        raise ValueError("Um quadro Telebradesco excedeu 121 bytes.")
    if len(raw_stream) > 0xC000:
        raise ValueError("O conteúdo Telebradesco é grande demais para o buffer seguro da ROM.")

    return {
        "content": content,
        "sections": sections,
        "section_sizes": {key: len(value) for key, value in sections.items()},
        "stream": raw_stream,
        "stream_size": len(raw_stream),
        "status_slots": normalized_status_slots,
        "requested_data_cartao": requested_card_identifier,
        "selected_data_cartao": (
            _tele_normalize_card_identifier(selected_card.get(TELEBRADESCO_CARD_IDENTIFIER_FIELD))
            if isinstance(selected_card, dict) else ""
        ),
        "empty_sections": tuple(sorted(requested_empty_sections)),
        "omitted_sections": tuple(sorted(requested_omitted_sections)),
        "frames": frames,
    }
def _tele_control_record(code: str) -> bytes:
    digits = _tele_digits(code, 3, "Código de estado Telebradesco")
    return bytes([0x01, 0x30]) + digits.encode("ascii") + bytes([0x04, 0x00])
def _tele_frames_from_stream(raw_stream: bytes) -> list[bytes]:
    """Compatibility splitter that protects a recognized Telebradesco trailer."""
    spans: list[tuple[int, int]] = []
    trailer_len = len(_tele_marker("0")) + 3 * len(_tele_control_record("999"))
    if len(raw_stream) >= trailer_len:
        trailer_start = len(raw_stream) - trailer_len
        if raw_stream[trailer_start:trailer_start + 3] == _tele_marker("0"):
            spans.append((trailer_start, trailer_start + 3))
            pos = trailer_start + 3
            for _ in range(3):
                spans.append((pos, pos + 7))
                pos += 7
    return _tele_frames_preserving_atomic_spans(raw_stream, spans)
def _tele_status_envelope_content() -> dict:
    """Build a structurally complete envelope without account data."""
    return normalize_telebradesco_content({
        "schema": "telebradesco_content_v5",
        "conta_corrente": {
            "titulo": "CONTA CORRENTE",
            "texto_auxiliar": "",
            "data_saldo": "00/00/00",
            "saldo": "0,00",
            "saldo_disponivel": "0,00",
            "saldo_vinculado": "0,00",
            "saldo_total": "0,00",
            "extrato": [],
            "ultimos_lancamentos": [],
            "lancamentos_futuros": [],
        },
        "investimentos": [],
        "poupanca": {
            "titulo": "POUPANCA",
            "texto_auxiliar": "",
            "data_saldo": "00/00/00",
            "saldo": "0,00",
            "saldo_disponivel": "0,00",
            "saldo_vinculado": "0,00",
            "saldo_total": "0,00",
            "extrato_total": [],
            "ultimos_lancamentos": [],
            "resumo": [],
        },
        "cartoes": [],
    })
def compile_telebradesco_status(
    status_code: str,
    content: dict | None = None,
    *,
    block_mode: object = "native-020",
) -> dict:
    """Compile an authentication result for the untouched original ROM.

    Native blocking uses a status-only envelope: slots 0 and 1 carry 020,
    sections 2 and 9 are absent, and no account or financial payload is sent.
    The original ROM then reaches C/C BLOQUEADA from Conta Corrente > Saldo
    and Poupanca > Saldo. Strict mode maps the same logical state to terminal
    code 010. Native 040 and 042 also use status-only envelopes so the ROM can
    surface AGENCIA INCORRETA or CONTA INEXISTENTE from the operation screens.
    Other rejected sessions use a neutral envelope and never expose real account content.
    """
    logical_code = _tele_digits(status_code, 3, "Código lógico de estado Telebradesco")
    mode = normalize_telebradesco_block_mode(block_mode)
    wire_code = _tele_auth_wire_code(logical_code, mode)
    slots = _tele_auth_status_slots(logical_code, mode)
    native_block = logical_code == "020" and mode == "native-020"
    native_invalid_agency = logical_code == "040"
    native_account_not_found = logical_code == "042"
    if native_block or native_invalid_agency or native_account_not_found:
        # Do not transmit account content for native blocked, invalid-agency
        # or missing-account results. Section 5 is retained only as an empty structural first
        # record so the byte stream follows the grammar already accepted by
        # the original ROM. Every real content section is omitted, forcing the
        # ROM to consult the appropriate operation status slot.
        compiled = compile_telebradesco_content(
            _tele_status_envelope_content(),
            status_slots=slots,
            empty_sections={"5"},
            omitted_sections={"1", "2", "3", "6", "8", "9", "A"},
        )
    else:
        compiled = compile_telebradesco_content(
            content if isinstance(content, dict) else _tele_status_envelope_content(),
            status_slots=slots,
        )
    return {
        "status_code": logical_code,
        "logical_status_code": logical_code,
        "wire_status_code": wire_code,
        "block_mode": mode,
        "status_slots": slots,
        "empty_sections": compiled["empty_sections"],
        "omitted_sections": compiled["omitted_sections"],
        "section_sizes": compiled["section_sizes"],
        "stream": compiled["stream"],
        "stream_size": compiled["stream_size"],
        "frames": compiled["frames"],
        "content": compiled["content"],
    }

# Validação estruturada de conteúdo
_TELE_DATE_DDMM_RE = re.compile(r"^\d{2}/\d{2}$")
_TELE_DATE_DDMMYY_RE = re.compile(r"^\d{2}/\d{2}/\d{2}$")
def _tele_validate_text(value: object, max_len: int, label: str, *, required: bool = False) -> str:
    text = str(value or "").strip()
    normalized = _tele_ascii(text)
    if required and not normalized:
        raise ValueError(f"{label}: preenchimento obrigatório.")
    if len(normalized.encode("ascii", "replace")) > max_len:
        raise ValueError(f"{label}: máximo de {max_len} caracteres.")
    return text
def _tele_validate_date(value: object, mode: str, label: str, *, required: bool = True) -> str:
    text = str(value or "").strip()
    if not text:
        if required:
            raise ValueError(f"{label}: preenchimento obrigatório.")
        return ""

    expected = "DD/MM" if mode == "ddmm" else "DD/MM/AA"
    pattern = _TELE_DATE_DDMM_RE if mode == "ddmm" else _TELE_DATE_DDMMYY_RE
    if not pattern.fullmatch(text):
        raise ValueError(f"{label}: use o formato {expected}.")

    parts = [int(part) for part in text.split("/")]
    day, month = parts[0], parts[1]
    year = 2000 if mode == "ddmm" else 2000 + parts[2]
    try:
        _dt.datetime(year, month, day)
    except ValueError:
        raise ValueError(f"{label}: data inválida.")
    return text
def _tele_validate_transaction_list(records: object, label: str) -> None:
    if not isinstance(records, list):
        raise ValueError(f"{label}: estrutura inválida.")
    for index, raw in enumerate(records, start=1):
        item = raw if isinstance(raw, dict) else {}
        _tele_validate_date(item.get("data"), "ddmm", f"{label}, linha {index}, data")
        _tele_validate_text(item.get("historico") or item.get("descricao"), 15, f"{label}, linha {index}, histórico", required=True)
        _tele_validate_text(item.get("documento"), 7, f"{label}, linha {index}, documento")
        _tele_validate_text(item.get("valor"), 19, f"{label}, linha {index}, valor", required=True)
def _tele_validate_summary_list(records: object, label: str, *, value_max_len: int = 20) -> None:
    if not isinstance(records, list):
        raise ValueError(f"{label}: estrutura inválida.")
    if len(records) > 3:
        raise ValueError(f"{label}: são permitidas no máximo 3 linhas.")
    for index, raw in enumerate(records, start=1):
        item = raw if isinstance(raw, dict) else {}
        _tele_validate_date(item.get("data"), "ddmmyy", f"{label}, linha {index}, data")
        _tele_validate_text(item.get("valor"), value_max_len, f"{label}, linha {index}, valor", required=True)
def _tele_validate_input_shape(content: object) -> None:
    """Reject malformed editor input before tolerant recovery normalization."""
    if not isinstance(content, dict):
        raise ValueError("Conteúdo Telebradesco: estrutura inválida; informe um objeto JSON.")

    for key, label in (("conta_corrente", "Conta Corrente"), ("poupanca", "Poupança")):
        section = content.get(key)
        if key in content and not isinstance(section, dict):
            raise ValueError(f"{label}: estrutura inválida.")

    for key, label in (("investimentos", "Investimentos"), ("cartoes", "Cartões")):
        collection = content.get(key)
        if key in content and not isinstance(collection, list):
            raise ValueError(f"{label}: estrutura inválida.")

    cc = content.get("conta_corrente")
    if isinstance(cc, dict):
        for key, label in (
            ("extrato", "Conta Corrente — Extrato"),
            ("ultimos_lancamentos", "Conta Corrente — Últimos Lançamentos"),
            ("lancamentos_futuros", "Conta Corrente — Lançamentos Futuros"),
        ):
            if key in cc and not isinstance(cc.get(key), list):
                raise ValueError(f"{label}: estrutura inválida.")

    savings = content.get("poupanca")
    if isinstance(savings, dict):
        for key, label in (
            ("extrato_total", "Poupança — Extrato Total"),
            ("ultimos_lancamentos", "Poupança — Últimos Lançamentos"),
            ("resumo", "Poupança — Resumo"),
        ):
            if key in savings and not isinstance(savings.get(key), list):
                raise ValueError(f"{label}: estrutura inválida.")

    cards = content.get("cartoes")
    if isinstance(cards, list):
        for index, card in enumerate(cards, start=1):
            if not isinstance(card, dict):
                raise ValueError(f"Cartão {index}: estrutura inválida.")


def validate_telebradesco_content(content: object) -> dict:
    _tele_validate_input_shape(content)
    normalized = normalize_telebradesco_content(content)
    cc = normalized["conta_corrente"]
    savings = normalized["poupanca"]
    cards = normalized.get("cartoes", [])
    investments = normalized.get("investimentos", [])

    for label, section in (("Conta Corrente", cc), ("Poupança", savings)):
        # The administrative interface may intentionally keep a new or cleared
        # account without financial data.  Empty fields are therefore accepted
        # and compiled as blank content; length and date validation still apply
        # whenever the operator fills a value.
        _tele_validate_text(section.get("titulo"), 32, f"{label}, título")
        _tele_validate_text(section.get("texto_auxiliar"), 28, f"{label}, texto auxiliar")
        _tele_validate_date(section.get("data_saldo"), "ddmmyy", f"{label}, data do saldo", required=False)
        _tele_validate_text(section.get("saldo"), 20, f"{label}, saldo")
        _tele_validate_text(section.get("saldo_disponivel"), 20, f"{label}, saldo disponível")
        _tele_validate_text(section.get("saldo_vinculado"), 18, f"{label}, saldo vinculado")
        _tele_validate_text(section.get("saldo_total"), 20, f"{label}, saldo total")

    _tele_validate_transaction_list(cc.get("extrato", []), "Conta Corrente — Extrato")
    _tele_validate_summary_list(
        cc.get("ultimos_lancamentos", []),
        "Conta Corrente — Últimos Lançamentos",
        value_max_len=14,
    )
    _tele_validate_transaction_list(cc.get("lancamentos_futuros", []), "Conta Corrente — Lançamentos Futuros")

    if not isinstance(investments, list):
        raise ValueError("Investimentos: estrutura inválida.")
    for index, raw in enumerate(investments, start=1):
        item = raw if isinstance(raw, dict) else {}
        _tele_validate_text(item.get("descricao"), 15, f"Investimento {index}, descrição", required=True)
        _tele_validate_text(item.get("valor"), 18, f"Investimento {index}, valor", required=True)

    _tele_validate_transaction_list(savings.get("extrato_total", []), "Poupança — Extrato Total")
    _tele_validate_transaction_list(savings.get("ultimos_lancamentos", []), "Poupança — Últimos Lançamentos")
    _tele_validate_summary_list(savings.get("resumo", []), "Poupança — Resumo")

    if not isinstance(cards, list):
        raise ValueError("Cartões: estrutura inválida.")
    seen_card_identifiers: set[str] = set()
    for card_index, raw_card in enumerate(cards, start=1):
        card = raw_card if isinstance(raw_card, dict) else {}
        identifier_raw = str(card.get(TELEBRADESCO_CARD_IDENTIFIER_FIELD) or "").strip()
        identifier = _tele_normalize_card_identifier(identifier_raw)
        if identifier_raw and len(identifier) != TELEBRADESCO_DATA_CARTAO_DIGITS:
            raise ValueError(
                f"Cartão {card_index}, Data Cartão: informe exatamente "
                f"{TELEBRADESCO_DATA_CARTAO_DIGITS} algarismos no formato DDMM."
            )
        if identifier:
            if identifier in seen_card_identifiers:
                raise ValueError(f"Cartão {card_index}: Data Cartão duplicada ({identifier}).")
            seen_card_identifiers.add(identifier)

        purchases = card.get("compras", [])
        if not isinstance(purchases, list):
            raise ValueError(f"Cartão {card_index}, compras: estrutura inválida.")

        # Unassigned legacy cards may remain stored for manual recovery, but
        # they are never selectable or transmitted until Data Cartão is filled.
        has_content = any(
            str(card.get(key) or "").strip()
            for key in ("bandeira", "final")
        ) or bool(purchases)
        if not identifier and not has_content:
            continue

        brand = _tele_validate_text(
            card.get("bandeira"), 21, f"Cartão {card_index}, bandeira", required=True
        ).upper()
        final = re.sub(r"\D", "", str(card.get("final", "")))
        if len(final) != 4:
            raise ValueError(f"Cartão {card_index}, final: informe exatamente 4 algarismos.")
        title = f"{_tele_ascii(brand).upper()} FINAL {final}"
        if len(title) > 32:
            raise ValueError(f"Cartão {card_index}: o título BANDEIRA FINAL 0000 excede 32 caracteres.")

        for purchase_index, raw in enumerate(purchases, start=1):
            item = raw if isinstance(raw, dict) else {}
            _tele_validate_date(item.get("data"), "ddmmyy", f"Cartão {card_index}, compra {purchase_index}, data")
            _tele_validate_text(item.get("descricao"), 28, f"Cartão {card_index}, compra {purchase_index}, descrição", required=True)
            _tele_validate_text(item.get("valor"), 20, f"Cartão {card_index}, compra {purchase_index}, valor", required=True)

    # Validation never selects a card. Selection is session-specific and comes
    # only from the Data Cartão received in MEGABRA.
    compile_telebradesco_content(normalized, data_cartao="")
    return normalized
