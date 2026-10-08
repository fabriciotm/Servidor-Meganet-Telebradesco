"""MegaNet TCP/stream session driver.

Keeps socket reading active while the protocol state machine waits for raw ACK/NAK.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Optional

from ..common import _close_client_writer
from .mail import DEFAULT_WELCOME_EMAIL_BODY, DEFAULT_WELCOME_EMAIL_SUBJECT
from .content_store import _prepare_meganet_session_args
from .session import MegaNetPacketDeliveryError, MegaNetSession

async def _meganet_socket_reader_loop(
    reader: object,
    session: MegaNetSession,
    protocol_queue: "asyncio.Queue[Optional[bytes]]",
) -> None:
    """Keep reader.read() active while protocol handlers await ACK/NAK."""
    close_reason = "EOF"
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                close_reason = "client EOF"
                break
            session.route_inbound_wire_data(data, protocol_queue)
    except asyncio.CancelledError:
        close_reason = "reader task cancelled"
        raise
    except (ConnectionError, OSError) as exc:
        close_reason = f"reader error: {exc}"
        logging.info("%s MEGANET_READER_CLOSED error=%s", session.peer, exc)
    except Exception as exc:
        close_reason = f"unexpected reader error: {exc}"
        logging.exception("%s MEGANET_READER_FAILED", session.peer)
    finally:
        session.flush_packet_wire_buffer(protocol_queue)
        session.notify_packet_reader_closed(close_reason)
        protocol_queue.put_nowait(None)

async def handle_meganet_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, args: argparse.Namespace) -> None:
    args = _prepare_meganet_session_args(args)
    peer_info = writer.get_extra_info("peername")
    peer = f"{peer_info[0]}:{peer_info[1]}" if peer_info else "unknown"
    session = MegaNetSession(
        writer,
        peer=peer,
        message_lines=args.message_lines,
        output_dir=args.output_dir,
        enable_session_logs=getattr(args, "enable_session_logs", False),
        cadastro_db_path=args.cadastro_db,
        online_start=args.online_start,
        auto_ready_seen=bool(getattr(args, "meganet_auto_ready_seen", False)),
        auth_mode=args.auth_mode,
        auth_password_source=args.auth_password_source,
        auth_check_bootstrap_login=args.auth_check_bootstrap_login,
        auth_reject_action=args.auth_reject_action,
        meganet_auth_mode=args.meganet_auth_mode,
        meganet_reject_action=args.meganet_reject_action,
        meganet_password_case=args.meganet_password_case,
        registration_duplicate_action=args.registration_duplicate_action,
        codigo_acesso_duplicate_action=args.codigo_acesso_duplicate_action,
        transaction_close=args.transaction_close,
        transaction_close_delay_seconds=args.transaction_close_delay,
        profile_idle_seconds=args.profile_idle,
        post_dle_delay_seconds=args.post_dle_delay,
        post_id_action=args.post_id_action,
        post_id_close_delay_seconds=args.post_id_close_delay,
        post_id_message_delay_seconds=args.post_id_message_delay,
        outbound_line_max_len=args.outbound_line_max_len,
        outbound_packet_delay_seconds=args.outbound_packet_delay,
        meganet_packet_ack_mode=args.meganet_packet_ack_mode,
        meganet_packet_ack_timeout_seconds=args.meganet_packet_ack_timeout,
        meganet_packet_max_retries=args.meganet_packet_max_retries,
        meganet_bootstrap_noise_tolerance=args.meganet_bootstrap_noise_tolerance,
        meganet_bootstrap_noise_window_seconds=args.meganet_bootstrap_noise_window,
        tectoy_news_lines=args.tectoy_news_lines,
        content_records=getattr(args, "content_records", None),
        connection_panel_lines=args.connection_panel_lines,
        connection_panel_mail_count=args.connection_panel_mail_count,
        unknown_token_action=args.unknown_token_action,
        content_profile=args.content_profile,
        routing_mode=args.routing_mode,
        news_route_map=args.news_route_map,
        mail_store_dir=args.mail_store_dir,
        mail_inbox_path=args.mail_inbox,
        mail_domain=args.mail_domain,
        admin_email=args.admin_email,
        welcome_email_enabled=getattr(args, "welcome_email_enabled", True),
        welcome_email_from=getattr(args, "welcome_email_from", "") or args.admin_email,
        welcome_email_subject=getattr(args, "welcome_email_subject", DEFAULT_WELCOME_EMAIL_SUBJECT),
        welcome_email_body=getattr(args, "welcome_email_body", "\n".join(DEFAULT_WELCOME_EMAIL_BODY)),
        password_change_store_dir=args.password_change_store_dir,
        password_change_apply=args.password_change_apply,
        raw_hayes_in_data_action=args.raw_hayes_in_data_action,
        raw_hayes_close_delay_seconds=args.raw_hayes_close_delay,
        access_identity_callback=(
            (lambda identity: getattr(args, "_service_runtime", None).update_access_identity("meganet", peer, identity))
            if getattr(args, "_service_runtime", None) else None
        ),
    )
    await session.start()
    protocol_queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue()
    reader_task = asyncio.create_task(
        _meganet_socket_reader_loop(reader, session, protocol_queue),
        name=f"MegaNetReader-{peer}",
    )
    try:
        while True:
            data = await protocol_queue.get()
            if data is None:
                break
            await session.feed(data)
            if session.bootstrap_terminate_requested or session.packet_delivery_failed:
                break
    except MegaNetPacketDeliveryError as exc:
        logging.error("%s MegaNet reliable delivery aborted: %s", peer, exc)
    except (ConnectionError, OSError) as exc:
        logging.info("%s MegaNet connection closed: %s", peer, exc)
    finally:
        if not reader_task.done():
            reader_task.cancel()
        await asyncio.gather(reader_task, return_exceptions=True)
        session.notify_packet_reader_closed("MegaNet handler cleanup")
        background_tasks = [
            getattr(session, "login_prompt_retry_task", None),
            getattr(session, "bootstrap_watchdog_task", None),
            getattr(session, "profile_idle_task", None),
            getattr(session, "post_dle_task", None),
            getattr(session, "close_task", None),
        ]
        current_task = asyncio.current_task()
        pending_background = []
        for task in background_tasks:
            if not isinstance(task, asyncio.Task) or task is current_task:
                continue
            if not task.done():
                task.cancel()
            pending_background.append(task)
        if pending_background:
            await asyncio.gather(*pending_background, return_exceptions=True)
        session.cancel_bootstrap_watchdog()
        logging.info(
            "%s MEGANET_PACKET_DELIVERY_SUMMARY mode=%s attempts=%d confirmed=%d "
            "retransmissions=%d naks=%d timeouts=%d failures=%d",
            peer,
            session.meganet_packet_ack_mode,
            session.packet_tx_attempts,
            session.packet_tx_confirmed,
            session.packet_tx_retransmissions,
            session.packet_tx_naks,
            session.packet_tx_timeouts,
            session.packet_tx_failures,
        )
        await _close_client_writer(writer, peer, "MegaNet")
        logging.info("%s disconnected", peer)

