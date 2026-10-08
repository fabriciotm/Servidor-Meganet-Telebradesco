"""MegaNet internal e-mail storage, parsing and delivery.
"""
from __future__ import annotations

import copy
import datetime as _dt
import json
import logging
import pathlib
import re
import threading
from typing import Optional

from ..common import utc_now_iso
from ..storage import atomic_write_json
from .accounts import (
    CadastroDatabase, MEGANET_ROOT_DIR, _safe_ascii_text, email_local_part,
    normalize_email_address, normalize_mail_domain, normalize_user_key,
)
from .content import MAX_CONTENT_RECORD_LINES, normalize_content_record_lines

DEFAULT_WELCOME_EMAIL_SUBJECT = "BEM VINDO"

DEFAULT_WELCOME_EMAIL_BODY = [
    "BEM VINDO {nome_meganet}",
    "SEU EMAIL: {email_meganet}",
    "GUARDE SUA SENHA",
]

DEFAULT_MAIL_STORE_DIR = MEGANET_ROOT_DIR / "mail"

_MAIL_STORE_LOCK = threading.RLock()

def _safe_meganet_mail_text(value: object) -> str:
    """Return text safe for the MegaNet mail reader while preserving high bytes.

    The e-mail composer in the cartridge uses several glyphs/emoticons encoded as
    single Latin-1/high-ASCII bytes.  They must not pass through the normal ASCII
    transliteration routine, otherwise bytes such as C4/E6/CC become A/?/I or ?.
    This helper only removes protocol delimiters and control line separators.
    """
    text = str(value or "")
    text = text.replace("#", "")
    text = text.replace("\r", " ").replace("\n", " ")
    # Drop C0 controls except tab; preserve printable Latin-1 and high-ASCII glyphs.
    text = "".join(ch for ch in text if ch == "\t" or ord(ch) >= 0x20)
    return text.encode("latin-1", "replace").decode("latin-1").strip()

class MailStore:
    """Persistent MegaNet internal e-mail store.

    Implements user-to-user delivery inside the emulated MegaNet domain.
    Files stay simple and editable by hand:
      meganet/mail/inbox.json                         legacy/global inbox
      meganet/mail/outbox_email.json                  all non-suggestion uploads
      meganet/mail/outbox_sugestoes.json              suggestions uploads
      meganet/mail/mailboxes/<local>.json             per-user mailbox

    Users receive addresses as <nome_meganet>@meganet.tectoy.com.br.
    The server manager owns admin@meganet.tectoy.com.br.
    """

    SUGESTOES_TO = "sugestoes@meganet.tectoy.com.br"

    def __init__(
        self,
        base_dir: pathlib.Path,
        inbox_path: Optional[pathlib.Path] = None,
        *,
        mail_domain: str = "meganet.tectoy.com.br",
        admin_email: str = "admin@meganet.tectoy.com.br",
    ) -> None:
        self.base_dir = pathlib.Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.mail_domain = normalize_mail_domain(mail_domain)
        self.admin_email = normalize_email_address(admin_email, self.mail_domain) or f"admin@{self.mail_domain}"
        self.sugestoes_email = normalize_email_address(self.SUGESTOES_TO, self.mail_domain)
        self.inbox_path = pathlib.Path(inbox_path) if inbox_path else self.base_dir / "inbox.json"
        self.outbox_email_path = self.base_dir / "outbox_email.json"
        self.outbox_sugestoes_path = self.base_dir / "outbox_sugestoes.json"
        self.mailboxes_dir = self.base_dir / "mailboxes"
        self.mailboxes_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_inbox()
        self._ensure_list_file(self.outbox_email_path)
        self._ensure_list_file(self.outbox_sugestoes_path)
        self._ensure_mailbox(self.admin_email)

    def _ensure_list_file(self, path: pathlib.Path) -> None:
        with _MAIL_STORE_LOCK:
            if not path.exists():
                atomic_write_json(path, {"messages": []}, ensure_ascii=False, indent=2)

    def _read_list_file(self, path: pathlib.Path) -> dict:
        self._ensure_list_file(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            data = {"messages": []}
        if isinstance(data, list):
            data = {"messages": data}
        if not isinstance(data, dict):
            data = {"messages": []}
        messages = data.setdefault("messages", [])
        if not isinstance(messages, list):
            data["messages"] = []
        return data

    def _write_list_file(self, path: pathlib.Path, data: dict) -> None:
        atomic_write_json(path, data, ensure_ascii=False, indent=2)

    def _ensure_inbox(self) -> None:
        with _MAIL_STORE_LOCK:
            if self.inbox_path.exists():
                return
            payload = {"messages": [{"id": "MAIL-00", "from": self.admin_email, "subject": "MEGA NET", "body": ["CAIXA POSTAL OK"]}]}
            atomic_write_json(self.inbox_path, payload, ensure_ascii=False, indent=2)

    def _mailbox_key(self, email: object) -> str:
        normalized = normalize_email_address(email, self.mail_domain)
        local = email_local_part(normalized)
        return local or "unknown"

    def mailbox_path(self, email: object) -> pathlib.Path:
        return self.mailboxes_dir / f"{self._mailbox_key(email)}.json"

    def _ensure_mailbox(self, email: object) -> pathlib.Path:
        with _MAIL_STORE_LOCK:
            path = self.mailbox_path(email)
            if not path.exists():
                payload = {"email": normalize_email_address(email, self.mail_domain), "created_at": utc_now_iso(), "messages": []}
                self._write_list_file(path, payload)
            return path

    def load_inbox_messages(self) -> list[dict]:
        self._ensure_inbox()
        try:
            data = json.loads(self.inbox_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            logging.error("MAIL_STORE failed to read inbox=%s error=%s", self.inbox_path, exc)
            return []
        if isinstance(data, list):
            messages = data
        else:
            messages = data.get("messages", []) if isinstance(data, dict) else []
        return [m for m in messages if isinstance(m, dict)]

    def load_mailbox_messages(self, email: object, *, include_global_when_empty: bool = False) -> list[dict]:
        normalized = normalize_email_address(email, self.mail_domain)
        if not normalized:
            return self.load_inbox_messages()
        path = self._ensure_mailbox(normalized)
        data = self._read_list_file(path)
        messages = [m for m in data.get("messages", []) if isinstance(m, dict)]
        if include_global_when_empty and not messages:
            return self.load_inbox_messages()
        return messages

    def mailbox_panel_notice_count(self, email: object) -> int:
        """Return the count that should be shown in the MegaNet connection panel.

        The ROM connection panel is rendered immediately after #id.  If a user
        sends an e-mail to their own mailbox and then downloads it in the same
        session, the mailbox can be empty by the next connection even though the
        user did receive a new message.  Keep a lightweight mailbox-level notice
        counter so the next connection panel can still announce the arrival once.
        Pending mailbox records always take precedence over this notice counter.
        """
        normalized = normalize_email_address(email, self.mail_domain)
        if not normalized:
            return len(self.load_inbox_messages())
        path = self._ensure_mailbox(normalized)
        data = self._read_list_file(path)
        messages = [m for m in data.get("messages", []) if isinstance(m, dict)]
        if messages:
            return len(messages)
        try:
            return max(0, int(data.get("panel_notice_count") or 0))
        except (TypeError, ValueError):
            return 0

    def clear_mailbox_panel_notice(self, email: object, *, reason: str = "connection_panel_shown") -> tuple[int, pathlib.Path]:
        """Clear the one-shot connection-panel notice counter for a mailbox."""
        with _MAIL_STORE_LOCK:
            normalized = normalize_email_address(email, self.mail_domain)
            path = self._ensure_mailbox(normalized)
            data = self._read_list_file(path)
            try:
                count = max(0, int(data.get("panel_notice_count") or 0))
            except (TypeError, ValueError):
                count = 0
            if count:
                data["panel_notice_count"] = 0
                data["panel_notice_cleared_at"] = utc_now_iso()
                data["panel_notice_clear_reason"] = reason
                self._write_list_file(path, data)
            return count, path

    def clear_mailbox_messages(self, email: object, *, archive: bool = True, reason: str = "downloaded") -> tuple[int, pathlib.Path]:
        """Delete all pending messages from a user's mailbox after a successful ROM download.

        The ROM treats mail download as a mailbox retrieval operation.  Once the
        server has sent all records and the final #ready# without a transport
        error, messages should disappear from the inbox so they are not received
        again on the next connection.  When archive=True, a copy is appended to
        downloaded_mail.json for server-side audit/debugging.
        """
        with _MAIL_STORE_LOCK:
            normalized = normalize_email_address(email, self.mail_domain)
            path = self._ensure_mailbox(normalized)
            data = self._read_list_file(path)
            messages = data.get("messages", [])
            if not isinstance(messages, list) or not messages:
                data["messages"] = []
                self._write_list_file(path, data)
                return 0, path
            count = len(messages)
            now = utc_now_iso()
            archive_path: Optional[pathlib.Path] = None
            archive_before: Optional[dict] = None
            if archive:
                archive_path = self.base_dir / "downloaded_mail.json"
                archive_data = self._read_list_file(archive_path)
                archive_before = copy.deepcopy(archive_data)
                archived = archive_data.setdefault("messages", [])
                if not isinstance(archived, list):
                    archive_data["messages"] = archived = []
                for msg in messages:
                    if isinstance(msg, dict):
                        archived.append({**msg, "downloaded_at": now, "downloaded_mailbox": normalized, "download_reason": reason})
                archive_data["updated_at"] = now
                self._write_list_file(archive_path, archive_data)

            # The ROM may receive a just-delivered message in the same connection in
            # which it was sent, especially when the sender addresses their own
            # mailbox.  In that case the mailbox is emptied immediately after the
            # transfer and a later connection panel would otherwise see zero pending
            # messages.  Preserve a one-shot notice count so {mail_count} can still
            # announce that arrival on the next Painel de Conexão.
            try:
                previous_notice = max(0, int(data.get("panel_notice_count") or 0))
            except (TypeError, ValueError):
                previous_notice = 0
            data["panel_notice_count"] = max(previous_notice, count)
            data["panel_notice_at"] = now
            data["panel_notice_reason"] = reason

            data["messages"] = []
            data["updated_at"] = now
            data["last_downloaded_at"] = now
            data["last_downloaded_count"] = count
            try:
                self._write_list_file(path, data)
            except (OSError, UnicodeError, TypeError, ValueError):
                if archive_path is not None and archive_before is not None:
                    try:
                        self._write_list_file(archive_path, archive_before)
                    except (OSError, UnicodeError, TypeError, ValueError) as rollback_exc:
                        logging.error(
                            "MAILBOX_ARCHIVE_ROLLBACK_FAILED mailbox=%s archive=%s error=%s",
                            normalized, archive_path, rollback_exc,
                        )
                raise
            return count, path
    def append_to_mailbox(self, email: object, message: dict) -> pathlib.Path:
        with _MAIL_STORE_LOCK:
            normalized = normalize_email_address(email, self.mail_domain)
            path = self._ensure_mailbox(normalized)
            data = self._read_list_file(path)
            data["email"] = normalized
            messages = data.setdefault("messages", [])
            if not isinstance(messages, list):
                data["messages"] = messages = []
            messages.append(message)
            now = utc_now_iso()
            data["updated_at"] = now
            try:
                data["panel_notice_count"] = max(0, int(data.get("panel_notice_count") or 0)) + 1
            except (TypeError, ValueError):
                data["panel_notice_count"] = 1
            data["panel_notice_at"] = now
            self._write_list_file(path, data)
            logging.info(
                "MAILBOX_APPEND mailbox=%s messages=%d path=%s subject=%s",
                normalized, len(messages), path, _safe_ascii_text(message.get("subject") if isinstance(message, dict) else "")[:80],
            )
            return path
    def append_welcome_message(
        self,
        email: object,
        nome_meganet: object,
        *,
        subject: object = DEFAULT_WELCOME_EMAIL_SUBJECT,
        body_lines: Optional[list[str]] = None,
        sender: object = None,
    ) -> pathlib.Path:
        normalized = normalize_email_address(email, self.mail_domain)
        nome = str(nome_meganet or email_local_part(normalized) or "USUARIO").strip()
        if body_lines is None:
            body_lines = [f"BEM VINDO {nome}", f"EMAIL {email_local_part(normalized)}", "GUARDE SUA SENHA"]
        safe_body = normalize_content_record_lines([str(x) for x in body_lines], max_lines=MAX_CONTENT_RECORD_LINES)
        msg = {
            "id": f"WELCOME-{_dt.datetime.now().strftime('%Y%m%d%H%M%S')}",
            "timestamp": _dt.datetime.now().isoformat(timespec="seconds"),
            "from": normalize_email_address(sender or self.admin_email, self.mail_domain),
            "to": normalized,
            "subject": _safe_ascii_text(subject or DEFAULT_WELCOME_EMAIL_SUBJECT)[:40] or DEFAULT_WELCOME_EMAIL_SUBJECT,
            "body": safe_body,
            "type": "welcome",
            "delivery_status": "delivered-internal",
        }
        return self.append_to_mailbox(normalized, msg)

    def append_outgoing(self, entry: dict, cadastro_db: Optional[CadastroDatabase] = None) -> pathlib.Path:
        with _MAIL_STORE_LOCK:
            destination = normalize_email_address(entry.get("to") or "", self.mail_domain)
            source = normalize_email_address(entry.get("from") or entry.get("user") or self.admin_email, self.mail_domain)
            entry["to"] = destination
            entry["from"] = source
            entry.setdefault("timestamp", _dt.datetime.now().isoformat(timespec="seconds"))
            is_sugestao = destination == self.sugestoes_email
            entry["type"] = "sugestao" if is_sugestao else "email"
            entry.setdefault("delivery_status", "queued")
            entry.setdefault("delivered_to", "")
            path = self.outbox_sugestoes_path if is_sugestao else self.outbox_email_path
            data = self._read_list_file(path)
            outbox_before = copy.deepcopy(data)
            messages = data.setdefault("messages", [])
            if not isinstance(messages, list):
                data["messages"] = messages = []
            messages.append(entry)

            mailbox_path: Optional[pathlib.Path] = None
            mailbox_before: Optional[dict] = None
            try:
                # Persist the queued state first, as before. If delivery or final
                # outbox status persistence fails, both primary JSON stores are
                # rolled back so a caller retry cannot duplicate the message.
                self._write_list_file(path, data)
                self._write_csv_best_effort(path.with_suffix(".csv"), data["messages"])

                delivery_email = ""
                delivery_message: Optional[dict] = None
                if is_sugestao:
                    entry["delivery_status"] = "delivered-sugestao"
                    entry["delivered_to"] = self.sugestoes_email
                    delivery_email = self.admin_email
                    delivery_message = {
                        **entry,
                        "id": f"SUG-{_dt.datetime.now().strftime('%Y%m%d%H%M%S')}",
                        "subject": entry.get("subject") or "SUGESTAO",
                    }
                elif destination == self.admin_email:
                    entry["delivery_status"] = "delivered-admin"
                    entry["delivered_to"] = self.admin_email
                    delivery_email = self.admin_email
                    delivery_message = {**entry, "id": f"ADM-{_dt.datetime.now().strftime('%Y%m%d%H%M%S')}"}
                elif destination.endswith("@" + self.mail_domain):
                    found = cadastro_db.find_user_by_meganet_email(destination, self.mail_domain) if cadastro_db else None
                    if not found and cadastro_db:
                        # the ROM may address the full internal e-mail while the
                        # cadastro record is keyed only by the local MegaNet name.
                        found = cadastro_db.find_user_by_meganet_name(email_local_part(destination))
                    if found:
                        fields = found[1].get("fields", {}) if isinstance(found[1], dict) else {}
                        recipient_email = normalize_email_address(fields.get("email_meganet") or destination, self.mail_domain)
                        entry["delivery_status"] = "delivered-internal"
                        entry["delivered_to"] = recipient_email
                        delivery_email = recipient_email
                        delivery_message = {**entry, "id": f"MSG-{_dt.datetime.now().strftime('%Y%m%d%H%M%S')}"}
                    elif self.mailbox_path(destination).exists():
                        # Safe fallback: deliver to an already existing internal mailbox,
                        # but do not create phantom mailboxes for mistyped recipients.
                        entry["delivery_status"] = "delivered-internal-mailbox"
                        entry["delivered_to"] = destination
                        delivery_email = destination
                        delivery_message = {**entry, "id": f"MSG-{_dt.datetime.now().strftime('%Y%m%d%H%M%S')}"}
                    else:
                        entry["delivery_status"] = "unknown-internal-recipient"
                else:
                    entry["delivery_status"] = "external-queued"

                if delivery_email and delivery_message is not None:
                    mailbox_path = self._ensure_mailbox(delivery_email)
                    mailbox_before = copy.deepcopy(self._read_list_file(mailbox_path))
                    self.append_to_mailbox(delivery_email, delivery_message)

                data["messages"][-1] = entry
                self._write_list_file(path, data)
                self._write_csv_best_effort(path.with_suffix(".csv"), data["messages"])
                return path
            except (OSError, UnicodeError, TypeError, ValueError):
                if mailbox_path is not None and mailbox_before is not None:
                    try:
                        self._write_list_file(mailbox_path, mailbox_before)
                    except (OSError, UnicodeError, TypeError, ValueError) as rollback_exc:
                        logging.error(
                            "MAIL_STORE_MAILBOX_ROLLBACK_FAILED mailbox=%s error=%s",
                            mailbox_path, rollback_exc,
                        )
                try:
                    self._write_list_file(path, outbox_before)
                except (OSError, UnicodeError, TypeError, ValueError) as rollback_exc:
                    logging.error(
                        "MAIL_STORE_OUTBOX_ROLLBACK_FAILED outbox=%s error=%s",
                        path, rollback_exc,
                    )
                raise

    def _write_csv_best_effort(self, path: pathlib.Path, messages: list[dict]) -> None:
        try:
            self._write_csv(path, messages)
        except (OSError, UnicodeError) as exc:
            # CSV is a derived/operator-friendly view. The JSON outbox/mailbox
            # remains the source of truth and a CSV filesystem failure must not
            # invalidate an otherwise successful ROM mail transaction.
            logging.warning("MAIL_STORE_CSV_WRITE_FAILED path=%s error=%s", path, exc)

    def _write_csv(self, path: pathlib.Path, messages: list[dict]) -> None:
        with path.open("w", encoding="utf-8", newline="") as f:
            f.write('timestamp,type,user,from,to,subject,delivery_status,delivered_to,body\n')
            for m in messages:
                def cell(v: object) -> str:
                    t = str(v or "").replace('"', '""').replace("\r", " ").replace("\n", " ")
                    return f'"{t}"'
                f.write(",".join([cell(m.get("timestamp")), cell(m.get("type")), cell(m.get("user")), cell(m.get("from")), cell(m.get("to")), cell(m.get("subject")), cell(m.get("delivery_status")), cell(m.get("delivered_to")), cell(m.get("body"))]) + "\n")

    @staticmethod
    def parse_upload(raw_chunks: list[bytes]) -> dict:
        """Parse an e-mail uploaded by the MegaNet cartridge.

        The captured ROM upload format is not guaranteed to use English labels.
        Older builds only recognized ``to:`` and ``subject:``, which meant that
        messages typed in the cartridge could be stored in the outbox but not
        delivered to a mailbox.  This parser accepts Portuguese labels and also
        the common label-free order used by the cartridge: destinatário,
        assunto, then message body.
        """
        decoded_chunks = [bytes(chunk).decode("latin-1", "replace") for chunk in raw_chunks]
        raw_text = "\n".join(decoded_chunks)
        normalized = raw_text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")

        def clean_line(value: str) -> str:
            text = str(value or "").strip()
            text = re.sub(r"#\s*(?:begin|end)\s+mail\s*#", "", text, flags=re.IGNORECASE).strip()
            text = re.sub(r"#\s*(?:sep|item)\s*#", "", text, flags=re.IGNORECASE).strip()
            return text.strip("# ").strip()

        lines = [clean_line(line) for line in normalized.split("\n")]
        lines = [line for line in lines if line]

        def label_key(value: str) -> str:
            key = str(value or "").strip().lower()
            key = key.translate(str.maketrans({
                "á": "a", "à": "a", "â": "a", "ã": "a", "ä": "a",
                "é": "e", "ê": "e", "è": "e", "ë": "e",
                "í": "i", "ì": "i", "î": "i", "ï": "i",
                "ó": "o", "ô": "o", "õ": "o", "ò": "o", "ö": "o",
                "ú": "u", "ù": "u", "û": "u", "ü": "u",
                "ç": "c",
            }))
            key = re.sub(r"[^a-z0-9]+", "", key)
            return key

        to_labels = {"to", "para", "destino", "destinatario", "email", "e-mail", "mail", "correio"}
        to_keys = {label_key(x) for x in to_labels}
        subject_labels = {"subject", "assunto", "titulo", "title"}
        subject_keys = {label_key(x) for x in subject_labels}
        body_labels = {"body", "message", "mensagem", "texto", "corpo"}
        body_keys = {label_key(x) for x in body_labels}
        from_keys = {label_key(x) for x in {"from", "de", "remetente"}}
        all_label_keys = to_keys | subject_keys | body_keys | from_keys

        label_word_re = r"[A-Za-zÀ-ÿ0-9 _./-]{1,28}"

        def split_labeled_line(line: str) -> tuple[str, str]:
            # Accept ``PARA: admin``, ``ASSUNTO=Teste`` and ``DE - nome``.
            m = re.match(rf"^({label_word_re})\s*[:=\-]\s*(.*)$", line, flags=re.IGNORECASE)
            if m:
                key = label_key(m.group(1))
                if key in all_label_keys:
                    return key, m.group(2).strip()
            # Accept ``PARA admin`` and ``ASSUNTO Teste``.
            m = re.match(rf"^({label_word_re})\s+(.+)$", line, flags=re.IGNORECASE)
            if m:
                key = label_key(m.group(1))
                if key in all_label_keys:
                    return key, m.group(2).strip()
            key = label_key(line)
            if key in all_label_keys:
                return key, ""
            return "", line.strip()

        def looks_like_recipient(value: str) -> bool:
            text = str(value or "").strip().strip("<>").lower()
            if not text:
                return False
            if "@" in text:
                return bool(re.fullmatch(r"[a-z0-9_.+\-]+@[a-z0-9_.\-]+", text))
            if text in {"admin", "sugestoes", "sugestões"}:
                return True
            # The cartridge often sends only the MegaNet local name.
            return bool(re.fullmatch(r"[a-z0-9_.\-]{2,64}", normalize_user_key(text)))

        to = ""
        subject = ""
        body_lines: list[str] = []
        consumed: set[int] = set()
        pending_field = ""
        body_mode = False

        for idx, line in enumerate(lines):
            key, value = split_labeled_line(line)

            if pending_field and not key:
                if pending_field == "to":
                    to = value
                    consumed.add(idx)
                elif pending_field == "subject":
                    subject = value
                    consumed.add(idx)
                elif pending_field == "body":
                    body_lines.append(value)
                    consumed.add(idx)
                    body_mode = True
                elif pending_field == "from":
                    consumed.add(idx)
                pending_field = ""
                continue

            if key in to_keys:
                consumed.add(idx)
                body_mode = False
                if value:
                    to = value
                else:
                    pending_field = "to"
                continue
            if key in subject_keys:
                consumed.add(idx)
                body_mode = False
                if value:
                    subject = value
                else:
                    pending_field = "subject"
                continue
            if key in from_keys:
                consumed.add(idx)
                body_mode = False
                if not value:
                    pending_field = "from"
                continue
            if key in body_keys:
                consumed.add(idx)
                body_mode = True
                if value:
                    body_lines.append(value)
                else:
                    pending_field = "body"
                continue

            if body_mode:
                body_lines.append(line)
                consumed.add(idx)

        # Fallback for the label-free form: recipient, subject, body.
        if not to:
            for idx, line in enumerate(lines[:6]):
                if idx in consumed:
                    continue
                if looks_like_recipient(line):
                    to = line.strip()
                    consumed.add(idx)
                    break

        if not subject:
            for idx, line in enumerate(lines):
                if idx in consumed:
                    continue
                # A short first remaining line is most likely the subject.
                subject = line.strip()[:80]
                consumed.add(idx)
                break

        if not body_lines:
            body_lines = [line for idx, line in enumerate(lines) if idx not in consumed]
        else:
            # Preserve any unconsumed lines after explicit body text; this helps
            # with uploads split into several frames after a ``TEXTO:`` label.
            for idx, line in enumerate(lines):
                if idx not in consumed and line not in body_lines:
                    body_lines.append(line)

        body = "\n".join(body_lines).strip() or "MENSAGEM SEM TEXTO"
        return {
            "to": to.strip(),
            "subject": (subject.strip() or "MEGA NET")[:80],
            "body": body,
            "raw_text": raw_text,
            "chunks_hex": [c.hex(" ") for c in raw_chunks],
        }

