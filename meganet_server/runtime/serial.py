"""Serial transport adapter for the service-neutral runtime."""
from __future__ import annotations

import argparse
import asyncio
import logging

from .configuration import service_display_name
from .transport import handle_client

class _SerialAsyncReader:
    def __init__(self, ser: object) -> None:
        self.ser = ser

    async def read(self, n: int = 4096) -> bytes:
        while True:
            try:
                if not getattr(self.ser, "is_open", True):
                    return b""
                data = self.ser.read(n)
                if data:
                    return data
            except Exception as exc:
                logging.error("SERIAL_READ_ERROR error=%s", exc)
                return b""
            await asyncio.sleep(0.005)


class _SerialAsyncWriter:
    def __init__(self, ser: object, port: str, baud: int) -> None:
        self.ser = ser
        self.port = port
        self.baud = baud

    def get_extra_info(self, name: str, default: object = None) -> object:
        if name == "peername":
            return (self.port, self.baud)
        return default

    def write(self, data: bytes) -> None:
        try:
            self.ser.write(data)
        except (OSError, ValueError) as exc:
            logging.error("SERIAL_WRITE_ERROR error=%s", exc)
            raise

    async def drain(self) -> None:
        try:
            self.ser.flush()
        except (OSError, ValueError) as exc:
            logging.error("SERIAL_FLUSH_ERROR error=%s", exc)
            raise
        await asyncio.sleep(0)

    def close(self) -> None:
        try:
            self.ser.close()
        except (OSError, ValueError):
            pass

    def is_closing(self) -> bool:
        return not bool(getattr(self.ser, "is_open", True))

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)


async def serial_server_loop(args: argparse.Namespace) -> None:
    try:
        import serial  # type: ignore
    except ImportError as exc:
        logging.error("SERIAL_UNAVAILABLE service=%s error=%s install='pip install pyserial'", service_display_name(args.active_service), exc)
        while True:
            await asyncio.sleep(3600)

    port = str(getattr(args, 'serial_port', '') or '')
    baud = int(getattr(args, 'serial_baud', 115200) or 115200)
    service_name = service_display_name(getattr(args, 'active_service', 'meganet'))
    while True:
        ser = None
        try:
            logging.info('%s serial opening port=%s baud=%s', service_name, port, baud)
            ser = serial.Serial(port=port, baudrate=baud, timeout=0, write_timeout=1)
            reader = _SerialAsyncReader(ser)
            writer = _SerialAsyncWriter(ser, port, baud)
            logging.info('%s serial listening on %s @ %s bps', service_name, port, baud)
            await handle_client(reader, writer, args)
        except asyncio.CancelledError:
            if ser is not None:
                try:
                    ser.close()
                except (OSError, ValueError):
                    pass
            raise
        except Exception as exc:
            logging.error('SERIAL_SERVER_ERROR service=%s port=%s baud=%s error=%s', service_name, port, baud, exc)
            if ser is not None:
                try:
                    ser.close()
                except (OSError, ValueError):
                    pass
            await asyncio.sleep(2.0)
