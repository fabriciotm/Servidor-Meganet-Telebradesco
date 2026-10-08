"""MegaNet ROM-facing editorial content, message formatting and budget rules.
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Iterable, Optional

from .accounts import (
    FIELD_NAMES, MEGANET_ROOT_DIR, _safe_ascii_text, meganet_email_for_name,
    normalize_email_address, normalize_mail_domain,
)

DEFAULT_MESSAGE = [
    "SEJA BEM-VINDO A MEGA NET, {usuario}.",
    "NAO ESQUECA DE ANOTAR SUA SENHA",
    "EM UM LOCAL SEGURO.",
]

DEFAULT_TECTOY_NEWS = [
    "TECTOY NEWS",
    "BEM-VINDO AO SERVICO DE NOTICIAS",
    "01/06/1995 - MEGA NET EM TESTE",
    "SERVIDOR EXPERIMENTAL ONLINE",
]

DEFAULT_OUTROS_GROUP = [
    "GRUPO OUTROS",
    "NENHUMA ATUALIZACAO",
]

DEFAULT_MAIL_GROUP = [
    "CORREIO ELETRONICO",
    "NENHUMA MENSAGEM NOVA",
]

CONTENT_RECORD_DEFINITIONS = [
    # TECTOY NEWS
    {"profile": "tectoy-news", "key": "tectoy_news.promocoes", "grupo": "TECTOY NEWS", "subgrupo": "TECTOY NEWS", "menu": "PROMOÇÕES", "path": "TECTOY NEWS > PROMOCOES", "default": ["PROMOCOES", "TECTOY NEWS ONLINE"]},
    {"profile": "tectoy-news", "key": "tectoy_news.produtos.g_gear", "grupo": "TECTOY NEWS", "subgrupo": "PRODUTOS", "menu": "G. GEAR", "path": "TECTOY NEWS > PRODUTOS > G. GEAR", "default": ["G. GEAR", "INFORMACOES SOBRE GAME GEAR"]},
    {"profile": "tectoy-news", "key": "tectoy_news.produtos.master", "grupo": "TECTOY NEWS", "subgrupo": "PRODUTOS", "menu": "MASTER", "path": "TECTOY NEWS > PRODUTOS > MASTER", "default": ["MASTER SYSTEM", "INFORMACOES SOBRE MASTER SYSTEM"]},
    {"profile": "tectoy-news", "key": "tectoy_news.produtos.mega", "grupo": "TECTOY NEWS", "subgrupo": "PRODUTOS", "menu": "MEGA", "path": "TECTOY NEWS > PRODUTOS > MEGA", "default": ["MEGA DRIVE", "INFORMACOES SOBRE MEGA DRIVE"]},
    {"profile": "tectoy-news", "key": "tectoy_news.produtos.32x", "grupo": "TECTOY NEWS", "subgrupo": "PRODUTOS", "menu": "32 X", "path": "TECTOY NEWS > PRODUTOS > 32 X", "default": ["32 X", "INFORMACOES SOBRE 32X"]},
    {"profile": "tectoy-news", "key": "tectoy_news.produtos.sega_cd", "grupo": "TECTOY NEWS", "subgrupo": "PRODUTOS", "menu": "SEGA CD", "path": "TECTOY NEWS > PRODUTOS > SEGA CD", "default": ["SEGA CD", "INFORMACOES SOBRE SEGA CD"]},
    {"profile": "tectoy-news", "key": "tectoy_news.produtos.sega_saturn", "grupo": "TECTOY NEWS", "subgrupo": "PRODUTOS", "menu": "SEGA SATURN", "path": "TECTOY NEWS > PRODUTOS > SEGA SATURN", "default": ["SEGA SATURN", "INFORMACOES SOBRE SATURN"]},
    {"profile": "tectoy-news", "key": "tectoy_news.produtos.pc_cd_rom", "grupo": "TECTOY NEWS", "subgrupo": "PRODUTOS", "menu": "PC CD ROM", "path": "TECTOY NEWS > PRODUTOS > PC CD ROM", "default": ["PC CD ROM", "INFORMACOES SOBRE PC CD ROM"]},
    {"profile": "tectoy-news", "key": "tectoy_news.produtos.outros", "grupo": "TECTOY NEWS", "subgrupo": "PRODUTOS", "menu": "OUTROS", "path": "TECTOY NEWS > PRODUTOS > OUTROS", "default": ["OUTROS PRODUTOS", "OUTRAS INFORMACOES DE PRODUTOS"]},
    {"profile": "tectoy-news", "key": "tectoy_news.lancamentos", "grupo": "TECTOY NEWS", "subgrupo": "TECTOY NEWS", "menu": "LANÇAMENTOS", "path": "TECTOY NEWS > LANCAMENTOS", "default": ["LANCAMENTOS", "NOVIDADES E LANCAMENTOS TECTOY"]},

    # DICAS
    {"profile": "tectoy-news", "key": "dicas.sega_cd", "grupo": "DICAS", "subgrupo": "DICAS", "menu": "SEGA CD", "path": "DICAS > SEGA CD", "default": ["DICAS - SEGA CD", "DICAS PARA JOGOS DE SEGA CD"]},
    {"profile": "tectoy-news", "key": "dicas.mega", "grupo": "DICAS", "subgrupo": "DICAS", "menu": "MEGA", "path": "DICAS > MEGA", "default": ["DICAS - MEGA", "DICAS PARA JOGOS DE MEGA DRIVE"]},
    {"profile": "tectoy-news", "key": "dicas.32x", "grupo": "DICAS", "subgrupo": "DICAS", "menu": "32 X", "path": "DICAS > 32 X", "default": ["DICAS - 32 X", "DICAS PARA JOGOS DE 32X"]},

    # SEGA CLUB
    {"profile": "tectoy-news", "key": "sega_club.o_que_e", "grupo": "SEGA CLUB", "subgrupo": "SEGA CLUB", "menu": "O QUE É?", "path": "SEGA CLUB > O QUE E", "default": ["SEGA CLUB - O QUE E", "INFORMACOES SOBRE O SEGA CLUB"]},
    {"profile": "tectoy-news", "key": "sega_club.p_ficar_socio", "grupo": "SEGA CLUB", "subgrupo": "SEGA CLUB", "menu": "P/ FICAR SÓCIO", "path": "SEGA CLUB > P/ FICAR SOCIO", "default": ["P/ FICAR SOCIO", "COMO PARTICIPAR DO SEGA CLUB"]},
    {"profile": "tectoy-news", "key": "sega_club.beneficios", "grupo": "SEGA CLUB", "subgrupo": "SEGA CLUB", "menu": "BENEFÍCIOS", "path": "SEGA CLUB > BENEFICIOS", "default": ["BENEFICIOS", "BENEFICIOS PARA SOCIOS"]},
    {"profile": "tectoy-news", "key": "sega_club.dicas", "grupo": "SEGA CLUB", "subgrupo": "SEGA CLUB", "menu": "DICAS", "path": "SEGA CLUB > DICAS", "default": ["SEGA CLUB - DICAS", "DICAS PARA SOCIOS"]},
    {"profile": "tectoy-news", "key": "sega_club.recordes", "grupo": "SEGA CLUB", "subgrupo": "SEGA CLUB", "menu": "RECORDES", "path": "SEGA CLUB > RECORDES", "default": ["RECORDES", "RECORDES DOS JOGADORES"]},
    {"profile": "tectoy-news", "key": "sega_club.infotocs", "grupo": "SEGA CLUB", "subgrupo": "SEGA CLUB", "menu": "INFOTOCS", "path": "SEGA CLUB > INFOTOCS", "default": ["INFOTOCS", "INFORMACOES E NOTAS DO CLUB"]},
    {"profile": "tectoy-news", "key": "sega_club.promocoes", "grupo": "SEGA CLUB", "subgrupo": "SEGA CLUB", "menu": "PROMOÇÕES", "path": "SEGA CLUB > PROMOCOES", "default": ["PROMOCOES", "PROMOCOES PARA SOCIOS"]},
    {"profile": "tectoy-news", "key": "sega_club.concursos", "grupo": "SEGA CLUB", "subgrupo": "SEGA CLUB", "menu": "CONCURSOS", "path": "SEGA CLUB > CONCURSOS", "default": ["CONCURSOS", "CONCURSOS E DESAFIOS"]},

    # INTERNET
    {"profile": "internet", "key": "internet.internet", "grupo": "INTERNET", "subgrupo": "INTERNET", "menu": "INTERNET", "path": "INTERNET > INTERNET", "default": ["INTERNET", "PORTAL MEGA NET"]},
    {"profile": "internet", "key": "internet.lista_mega_net", "grupo": "INTERNET", "subgrupo": "LISTA MEGA NET", "menu": "LISTA MEGA NET", "path": "INTERNET > LISTA MEGA NET", "default": ["LISTA MEGA NET", "ENDERECOS ONLINE"]},

    # REVISTA
    # Native complete Revista menu selected by the post-ID status token
    # #abril#. Record order is protocol-significant: slots 0-4 feed Comics
    # and slots 5-10 feed RPG after menu descriptor $016FF2.
    {"profile": "revista", "record_index": 0, "key": "revista.comics.lancamentos", "grupo": "REVISTA", "subgrupo": "COMICS", "menu": "LANÇAMENTOS", "path": "REVISTA > COMICS > LANCAMENTOS (slot 0)", "default": ["COMICS - LANCAMENTOS", "NOVIDADES DOS QUADRINHOS"]},
    {"profile": "revista", "record_index": 1, "key": "revista.comics.quem_e_quem", "grupo": "REVISTA", "subgrupo": "COMICS", "menu": "QUEM É QUEM", "path": "REVISTA > COMICS > QUEM E QUEM (slot 1)", "default": ["COMICS - QUEM E QUEM", "PERSONAGENS E AUTORES"]},
    {"profile": "revista", "record_index": 2, "key": "revista.comics.perfil", "grupo": "REVISTA", "subgrupo": "COMICS", "menu": "PERFIL", "path": "REVISTA > COMICS > PERFIL (slot 2)", "default": ["COMICS - PERFIL", "DESTAQUE DA EDICAO"]},
    {"profile": "revista", "record_index": 3, "key": "revista.comics.acontece", "grupo": "REVISTA", "subgrupo": "COMICS", "menu": "ACONTECE", "path": "REVISTA > COMICS > ACONTECE (slot 3)", "default": ["COMICS - ACONTECE", "NOTICIAS E EVENTOS"]},
    {"profile": "revista", "record_index": 4, "key": "revista.comics.home_shopping", "grupo": "REVISTA", "subgrupo": "COMICS", "menu": "HOME SHOPPING", "path": "REVISTA > COMICS > HOME SHOPPING (slot 4)", "default": ["COMICS - HOME SHOPPING", "PRODUTOS E OFERTAS"]},
    {"profile": "revista", "record_index": 5, "key": "revista.rpg.lancamentos", "grupo": "REVISTA", "subgrupo": "RPG", "menu": "LANÇAMENTOS", "path": "REVISTA > RPG > LANCAMENTOS (slot 5)", "default": ["RPG - LANCAMENTOS", "NOVIDADES DE RPG"]},
    {"profile": "revista", "record_index": 6, "key": "revista.rpg.quem_e_quem", "grupo": "REVISTA", "subgrupo": "RPG", "menu": "QUEM É QUEM", "path": "REVISTA > RPG > QUEM E QUEM (slot 6)", "default": ["RPG - QUEM E QUEM", "PERSONAGENS E CRIADORES"]},
    {"profile": "revista", "record_index": 7, "key": "revista.rpg.perfil", "grupo": "REVISTA", "subgrupo": "RPG", "menu": "PERFIL", "path": "REVISTA > RPG > PERFIL (slot 7)", "default": ["RPG - PERFIL", "SISTEMA EM DESTAQUE"]},
    {"profile": "revista", "record_index": 8, "key": "revista.rpg.dicas", "grupo": "REVISTA", "subgrupo": "RPG", "menu": "DICAS", "path": "REVISTA > RPG > DICAS (slot 8)", "default": ["RPG - DICAS", "DICAS PARA SUA AVENTURA"]},
    {"profile": "revista", "record_index": 9, "key": "revista.rpg.acontece", "grupo": "REVISTA", "subgrupo": "RPG", "menu": "ACONTECE", "path": "REVISTA > RPG > ACONTECE (slot 9)", "default": ["RPG - ACONTECE", "NOTICIAS E EVENTOS"]},
    {"profile": "revista", "record_index": 10, "key": "revista.rpg.home_shopping", "grupo": "REVISTA", "subgrupo": "RPG", "menu": "HOME SHOPPING", "path": "REVISTA > RPG > HOME SHOPPING (slot 10)", "default": ["RPG - HOME SHOPPING", "LIVROS E ACESSORIOS"]},

    # OUTROS
    {"profile": "outros", "key": "outros.outros", "grupo": "OUTROS", "subgrupo": "OUTROS", "menu": "OUTROS", "path": "OUTROS > OUTROS", "default": ["OUTROS", "SERVICOS MEGA NET"]},
    {"profile": "outros", "key": "outros.home_shopping.games", "grupo": "OUTROS", "subgrupo": "HOME SHOPPING", "menu": "GAMES", "path": "OUTROS > HOME SHOPPING > GAMES", "default": ["HOME SHOPPING", "GAMES", "OFERTAS DE JOGOS"]},
    {"profile": "outros", "key": "outros.home_shopping.variedades", "grupo": "OUTROS", "subgrupo": "HOME SHOPPING", "menu": "VARIEDADES", "path": "OUTROS > HOME SHOPPING > VARIEDADES", "default": ["HOME SHOPPING", "VARIEDADES", "OFERTAS DIVERSAS"]},
]

DEFAULT_CONTENT_RECORDS = {
    item["key"]: list(item["default"])
    for item in CONTENT_RECORD_DEFINITIONS
}

CONTENT_BLANK_LINE_MARKER = "<BR>"

CONTENT_BLANK_LINE_ALIASES = {"<BR>", "[BR]", "{BR}"}

DEFAULT_CONTENT_DIR = MEGANET_ROOT_DIR / "content"

DEFAULT_CONTENT_FILE = "content.json"

MAX_MESSAGE_LINE_LEN = 35

MAX_CONTENT_RECORD_LINES = 19

MAX_CONTENT_RECORD_TOTAL_LINES = 50

MAX_CONTENT_RECORD_PAGES = 3

MAX_CONTENT_GROUP_SAFE_BYTES = 0x3F00

MAX_CONTENT_GROUP_OBSERVED_BYTES = 0x4000

CONTENT_ITEM_STATUS_BYTES = len(b"#item#\r")

CONTENT_END_STATUS_BYTES = len(b"#end#\r")

CONTENT_RECORD_BODY_MAX_BYTES = 116

CONTENT_PROFILE_ORDER = ("tectoy-news", "internet", "revista", "outros")

CONTENT_PROFILE_LABELS = {
    "tectoy-news": "Tectoy News / Dicas / Sega Club",
    "internet": "Internet",
    "revista": "Revista",
    "outros": "Outros / Home Shopping",
}

CONNECTION_PANEL_PRESETS = {
    "unified": [
        "CONECTADO A MEGA NET",
        "CORREIO: {mail_count} MENSAGENS",
        "RECEBENDO SERVICOS",
    ],
    "tectoy-news": [
        "CONECTADO A TECTOY NEWS",
        "BAIXANDO NOVIDADES",
        "PEGUE SEU CONTROLE",
    ],
    "mail": [
        "CONECTADO AO CORREIO",
        "VERIFICANDO MENSAGENS",
        "NAO DESLIGUE O MEGA",
    ],
    "internet": [
        "CONECTADO A INTERNET",
        "ABRINDO PORTAL MEGA NET",
        "EXPLORE O MUNDO ONLINE",
    ],
    "revista": [
        "CONECTADO A REVISTA",
        "CARREGANDO MATERIAS",
        "BOA LEITURA",
    ],
    "outros": [
        "CONECTADO AO GRUPO OUTROS",
        "SINCRONIZANDO SERVICOS",
        "AGUARDE UM INSTANTE",
    ],
    "troca-senha": [
        "TROCA DE SENHA",
        "MAPEANDO PROTOCOLO",
        "NAO DESLIGUE O MEGA",
    ],
    "default": [
        "CONECTADO A MEGA NET",
        "RECEBENDO ATUALIZACOES",
        "AGUARDE",
    ],
}

def is_content_blank_line(value: object) -> bool:
    """Return True when a content line represents one blank ROM row."""
    return str(value or "").strip().upper() in CONTENT_BLANK_LINE_ALIASES

def content_line_wire_bytes(value: object) -> int:
    """Return bytes consumed on the MegaNet wire, including trailing CR."""
    if is_content_blank_line(value) or str(value or "") == "":
        return 1
    return len(str(value).encode("latin-1", "replace")) + 1

def is_content_mapping_line(value: object) -> bool:
    """Return True for internal mapping markers that should not be sent to clients."""
    text = _safe_ascii_text(str(value or "")).strip().upper()
    return bool(re.fullmatch(r"(?:MAP|INET|REV|OUT)-\d{2}", text))

def strip_content_mapping_lines(lines: object) -> list[str]:
    """Remove internal mapping IDs while preserving intentional blank rows.

    A physically empty row in the web editor is canonicalized to ``<BR>``.
    The aliases ``[BR]`` and ``{BR}`` are also accepted and saved as ``<BR>``.
    """
    if isinstance(lines, str):
        raw_lines = lines.splitlines()
    elif isinstance(lines, list):
        raw_lines = [str(x) for x in lines]
    else:
        raw_lines = []

    out: list[str] = []
    for raw in raw_lines:
        text = str(raw).rstrip("\r\n")
        if is_content_blank_line(text) or not text.strip():
            out.append(CONTENT_BLANK_LINE_MARKER)
            continue
        if is_content_mapping_line(text):
            continue
        clean = text.encode("latin-1", errors="replace").decode("latin-1")
        out.extend(split_line_for_rom(clean, MAX_MESSAGE_LINE_LEN))
    return out

def merge_content_records(value: object) -> dict[str, list[str]]:
    """Return content records with defaults filled and internal mapping markers removed."""
    merged: dict[str, list[str]] = {
        k: strip_content_mapping_lines(v)
        for k, v in DEFAULT_CONTENT_RECORDS.items()
    }
    if isinstance(value, dict):
        for key, raw in value.items():
            key = str(key)
            if key not in merged:
                continue
            cleaned = strip_content_mapping_lines(raw)
            if cleaned:
                merged[key] = cleaned
    return merged

def split_line_for_rom(value: object, max_len: int = MAX_MESSAGE_LINE_LEN) -> list[str]:
    """Split one logical message line into ROM-safe chunks.

    The MegaNet screen and packet routines are more stable when each visible
    line stays at 35 characters or less.  The split is hard rather than
    word-based so pasted fixed-width test lines are preserved exactly.
    """
    text = str(value or "").rstrip("\r\n")
    if not text:
        return []
    max_len = max(8, min(MAX_MESSAGE_LINE_LEN, int(max_len or MAX_MESSAGE_LINE_LEN)))
    return [text[i:i + max_len] for i in range(0, len(text), max_len)]

def normalize_message_lines(lines: object) -> list[str]:
    if lines is None:
        raw_lines: Iterable[object] = ()
    elif isinstance(lines, str):
        raw_lines = lines.splitlines()
    else:
        try:
            raw_lines = iter(lines)  # type: ignore[arg-type]
        except TypeError:
            raw_lines = ()

    out: list[str] = []
    for line in raw_lines:
        clean = str(line).rstrip("\r\n")
        if clean:
            # The ROM path is byte-oriented.  Latin-1 keeps Portuguese characters
            # representable for experimentation, while the defaults remain ASCII-safe.
            clean = clean.encode("latin-1", errors="replace").decode("latin-1")
            out.extend(split_line_for_rom(clean, MAX_MESSAGE_LINE_LEN))
    return out or DEFAULT_MESSAGE[:]

def _safe_latin1_content_text(value: object) -> str:
    """Return MegaNet editorial text preserving Portuguese Latin-1 glyphs.

    The original content path used `_safe_ascii_text`, which transliterated or
    replaced every accented character before the admin UI and cartridge could
    see it.  MegaNet transport is byte-oriented and the already validated
    connection-panel path uses Latin-1, so editorial records use the same
    encoding while removing protocol-reserved `#` and control characters.
    """
    text = str(value or "").replace("\x00", "")
    text = text.replace("\r", " ").replace("\n", " ")
    text = "".join(ch for ch in text if ch == "\t" or ord(ch) >= 32)
    return text.encode("latin-1", errors="replace").decode("latin-1").strip()

def normalize_content_record_lines(lines: object, max_lines: int = MAX_CONTENT_RECORD_LINES) -> list[str]:
    """Normalize one configured content panel for the MegaNet ROM.

    The validated transport strategy is preserved: a long logical item may be split
    across continuation frames, with #end# only in the final frame.  Callers
    choose the visible-line cap. The native screen-sized default remains 19;
    Group-content callers may explicitly request up to 50 lines; the
    maximum validated with the original ROM during the extended-content test.
    """
    if lines is None:
        raw_lines: Iterable[object] = ()
    elif isinstance(lines, str):
        raw_lines = lines.splitlines()
    else:
        try:
            raw_lines = iter(lines)  # type: ignore[arg-type]
        except TypeError:
            raw_lines = ()

    out: list[str] = []
    limit_enabled = int(max_lines or 0) > 0
    for line in raw_lines:
        raw = str(line or "")

        if is_content_blank_line(raw) or raw == "":
            if limit_enabled and len(out) >= int(max_lines):
                return out
            out.append(CONTENT_BLANK_LINE_MARKER)
            continue

        clean = _safe_latin1_content_text(raw).replace("#", "").strip()
        if not clean:
            continue
        for part in split_line_for_rom(clean, MAX_MESSAGE_LINE_LEN):
            if limit_enabled and len(out) >= int(max_lines):
                return out
            out.append(part)
    return out or ["SEM CONTEUDO"]

def _format_date_br(dt: Optional[_dt.datetime] = None) -> str:
    dt = dt or _dt.datetime.now()
    return dt.strftime("%d/%m/%Y")

def _format_time_br(dt: Optional[_dt.datetime] = None) -> str:
    dt = dt or _dt.datetime.now()
    return dt.strftime("%H:%M")

def _build_meganet_placeholder_map(fields: Optional[dict], *, usuario: str = "", email: str = "", mail_domain: str = "") -> dict[str, str]:
    """Build user-facing placeholders for MegaNet messages and welcome mail."""
    fields = dict(fields or {})
    usuario_text = str(usuario or fields.get("nome_meganet") or fields.get("nome") or "USUARIO").strip() or "USUARIO"
    email_text = normalize_email_address(email or fields.get("email_meganet") or meganet_email_for_name(usuario_text, mail_domain or "meganet.tectoy.com.br"), mail_domain or "meganet.tectoy.com.br")
    now = _dt.datetime.now()
    mapping = {
        "usuario": usuario_text,
        "nome_meganet": str(fields.get("nome_meganet") or usuario_text),
        "email": email_text,
        "email_meganet": email_text,
        "mail_domain": normalize_mail_domain(mail_domain or "meganet.tectoy.com.br"),
        "data": _format_date_br(now),
        "hora": _format_time_br(now),
    }
    for name in FIELD_NAMES:
        if name not in mapping:
            mapping[name] = str(fields.get(name) or "")
    # Common aliases shown in the web interface.
    mapping.setdefault("codigo", str(fields.get("codigo_acesso") or ""))
    mapping.setdefault("senha", str(fields.get("senha") or ""))
    expanded: dict[str, str] = {}
    for key, value in mapping.items():
        text = str(value or "")
        expanded["{" + key + "}"] = text
        expanded["{" + key.upper() + "}"] = text.upper()
    return expanded

def render_meganet_placeholders(value: object, fields: Optional[dict], *, usuario: str = "", email: str = "", mail_domain: str = "") -> str:
    text = str(value or "")
    for key, replacement in _build_meganet_placeholder_map(fields, usuario=usuario, email=email, mail_domain=mail_domain).items():
        text = text.replace(key, replacement)
    return text

def render_meganet_placeholder_lines(lines: object, fields: Optional[dict], *, usuario: str = "", email: str = "", mail_domain: str = "", max_lines: Optional[int] = None) -> list[str]:
    if isinstance(lines, str):
        raw_lines = lines.splitlines()
    elif isinstance(lines, list):
        raw_lines = [str(x) for x in lines]
    else:
        raw_lines = []
    rendered = [render_meganet_placeholders(line, fields, usuario=usuario, email=email, mail_domain=mail_domain) for line in raw_lines]
    normalized = normalize_content_record_lines(rendered, max_lines=max_lines or MAX_CONTENT_RECORD_LINES)
    return normalized

def count_normalized_content_lines(lines: Iterable[str]) -> int:
    count = 0
    for line in lines:
        raw = str(line or "")
        if is_content_blank_line(raw) or raw == "":
            count += 1
            continue
        clean = _safe_latin1_content_text(raw).replace("#", "").strip()
        if not clean:
            continue
        count += len(split_line_for_rom(clean, MAX_MESSAGE_LINE_LEN))
    return count

def normalized_content_record_body_bytes(lines: Iterable[str], *, max_lines: int = MAX_CONTENT_RECORD_TOTAL_LINES) -> int:
    """Return ROM body bytes for one item, including each line's trailing CR."""
    normalized = normalize_content_record_lines(lines, max_lines=max_lines)
    return sum(content_line_wire_bytes(line) for line in normalized)

def content_profile_definitions(profile: str) -> list[dict]:
    profile = str(profile or "").strip().lower()
    defs = [item for item in CONTENT_RECORD_DEFINITIONS if str(item.get("profile", "")).lower() == profile]
    if profile == "revista":
        defs.sort(key=lambda item: int(item.get("record_index", 0)))
    return defs

def content_group_budget(content_records: object, profile: str) -> dict:
    """Calculate the safe ROM payload budget used by one protocol group."""
    profile = str(profile or "").strip().lower()
    records = merge_content_records(content_records)
    defs = content_profile_definitions(profile)
    item_rows: list[dict] = []
    body_bytes = 0
    for item in defs:
        key = str(item.get("key", ""))
        fallback = [str(x) for x in item.get("default", [])]
        lines = normalize_content_record_lines(records.get(key) or fallback, max_lines=MAX_CONTENT_RECORD_TOTAL_LINES)
        used = sum(content_line_wire_bytes(line) for line in lines)
        body_bytes += used
        item_rows.append({
            "key": key,
            "path": str(item.get("path", key)),
            "lines": len(lines),
            "body_bytes": used,
        })
    status_bytes = 0
    if defs:
        status_bytes = max(0, len(defs) - 1) * CONTENT_ITEM_STATUS_BYTES + CONTENT_END_STATUS_BYTES
    total_bytes = body_bytes + status_bytes
    return {
        "profile": profile,
        "label": CONTENT_PROFILE_LABELS.get(profile, profile),
        "records": len(defs),
        "body_bytes": body_bytes,
        "status_bytes": status_bytes,
        "total_bytes": total_bytes,
        "limit_bytes": MAX_CONTENT_GROUP_SAFE_BYTES,
        "remaining_bytes": MAX_CONTENT_GROUP_SAFE_BYTES - total_bytes,
        "over_limit": total_bytes > MAX_CONTENT_GROUP_SAFE_BYTES,
        "items": item_rows,
    }

def content_budget_report(content_records: object) -> dict[str, dict]:
    return {profile: content_group_budget(content_records, profile) for profile in CONTENT_PROFILE_ORDER}

def fit_profile_lines_to_safe_budget(profile: str, profile_lines: dict[str, list[str]]) -> tuple[dict[str, list[str]], dict, dict, bool]:
    """Clamp manually edited/legacy content without blanking later ROM records.

    Every item first retains up to the native 19-line screen.  Remaining lines
    are then restored in protocol order until the 0x3F00 safety budget is used.
    Normal saves are rejected before this is needed; this is runtime protection
    for JSON files modified outside the administrative interface.
    """
    defs = content_profile_definitions(profile)
    normalized: dict[str, list[str]] = {}
    for item in defs:
        key = str(item.get("key", ""))
        fallback = [str(x) for x in item.get("default", [])]
        normalized[key] = normalize_content_record_lines(profile_lines.get(key) or fallback, max_lines=MAX_CONTENT_RECORD_TOTAL_LINES)

    def _report(mapping: dict[str, list[str]]) -> dict:
        body = 0
        rows = []
        for item in defs:
            key = str(item.get("key", ""))
            lines = mapping.get(key) or ["SEM CONTEUDO"]
            used = sum(content_line_wire_bytes(line) for line in lines)
            body += used
            rows.append({"key": key, "lines": len(lines), "body_bytes": used})
        status = max(0, len(defs) - 1) * CONTENT_ITEM_STATUS_BYTES + (CONTENT_END_STATUS_BYTES if defs else 0)
        total = body + status
        return {"profile": profile, "body_bytes": body, "status_bytes": status, "total_bytes": total, "limit_bytes": MAX_CONTENT_GROUP_SAFE_BYTES, "items": rows}

    before = _report(normalized)
    if before["total_bytes"] <= MAX_CONTENT_GROUP_SAFE_BYTES:
        return normalized, before, before, False

    fitted: dict[str, list[str]] = {}
    for item in defs:
        key = str(item.get("key", ""))
        lines = normalized[key]
        fitted[key] = lines[:MAX_CONTENT_RECORD_LINES] or ["SEM CONTEUDO"]

    current = _report(fitted)
    if current["total_bytes"] > MAX_CONTENT_GROUP_SAFE_BYTES:
        # Defensive path; current known profiles fit at 19 lines per record.
        for item in reversed(defs):
            key = str(item.get("key", ""))
            while len(fitted[key]) > 1 and current["total_bytes"] > MAX_CONTENT_GROUP_SAFE_BYTES:
                fitted[key].pop()
                current = _report(fitted)

    for item in defs:
        key = str(item.get("key", ""))
        source = normalized[key]
        for line in source[len(fitted[key]):]:
            add_bytes = content_line_wire_bytes(line)
            if current["total_bytes"] + add_bytes > MAX_CONTENT_GROUP_SAFE_BYTES:
                break
            fitted[key].append(line)
            current["total_bytes"] += add_bytes
            current["body_bytes"] += add_bytes

    after = _report(fitted)
    return fitted, before, after, True

def render_message_lines(lines: Iterable[str], usuario: str = "") -> list[str]:
    safe_user = str(usuario or "USUARIO").strip() or "USUARIO"
    rendered: list[str] = []
    for line in lines:
        text = str(line)
        text = text.replace("{usuario}", safe_user)
        text = text.replace("{USUARIO}", safe_user.upper())
        rendered.append(text)
    return normalize_message_lines(rendered)

def wrap_text_line(text: str, max_len: int) -> list[str]:
    """Wrap one MegaNet display line conservatively for the ROM UI.

    Link-layer packets can carry longer payloads, but the MegaNet message screen
    appears to be sensitive to long logical text rows.  Keep rows short and
    word-wrapped so the ROM does not remain stuck on the identification screen.
    """
    text = str(text).strip()
    if not text:
        return []
    max_len = max(8, int(max_len or 28))
    words = text.split()
    out: list[str] = []
    cur = ""
    for word in words:
        # If a single token is too long, split it hard.
        if len(word) > max_len:
            if cur:
                out.append(cur)
                cur = ""
            while len(word) > max_len:
                out.append(word[:max_len])
                word = word[max_len:]
            if word:
                cur = word
            continue
        candidate = word if not cur else cur + " " + word
        if len(candidate) <= max_len:
            cur = candidate
        else:
            if cur:
                out.append(cur)
            cur = word
    if cur:
        out.append(cur)
    return out

def normalize_display_message_lines(lines: Iterable[str]) -> list[str]:
    """Normalize display-message rows while preserving intentional blanks.

    Unlike ``normalize_message_lines()``, this path recognizes the canonical
    ``<BR>`` marker (plus [BR] and {BR}) and keeps it until the final packet
    preparation stage.  It is used by the connection panel and message blocks.
    """
    out: list[str] = []
    for line in lines:
        raw = str(line or "").rstrip("\r\n")
        if is_content_blank_line(raw) or raw == "":
            out.append(CONTENT_BLANK_LINE_MARKER)
            continue
        clean = raw.encode("latin-1", errors="replace").decode("latin-1")
        out.extend(split_line_for_rom(clean, MAX_MESSAGE_LINE_LEN))
    return out

def prepare_display_lines(lines: Optional[list[str]], max_len: int) -> list[str]:
    """Prepare visible message rows, converting <BR> into one empty CR row."""
    wrapped: list[str] = []
    for line in normalize_display_message_lines(lines or []):
        if is_content_blank_line(line):
            wrapped.append("")
            continue
        wrapped.extend(wrap_text_line(line, max_len))
    return wrapped

