"""Administración del acceso usando el volumen persistente del servidor."""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

from micnaga.remote_protocol import DEVICE_ID
from remote.store import Store


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m remote")
    parser.add_argument(
        "--db", default=os.environ.get("MICNAGA_REMOTE_DB", "/data/meetings.sqlite3")
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("create-device").add_argument("workspace")
    for command in ("grant", "revoke"):
        member = commands.add_parser(command)
        member.add_argument("workspace")
        member.add_argument("subject", help="ID del usuario en el proveedor OAuth (claim sub)")
    commands.add_parser("revoke-device").add_argument("device")
    purge = commands.add_parser("purge-device")
    purge.add_argument("device")
    purge.add_argument("--confirm", action="store_true", required=True)
    args = parser.parse_args()
    if (
        hasattr(args, "workspace")
        and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", args.workspace) is None
    ):
        parser.error("biblioteca no válida, usa minúsculas ASCII, números, guion o guion bajo")
    if hasattr(args, "device") and DEVICE_ID.fullmatch(args.device) is None:
        parser.error("ID de equipo no válido")
    store = Store(Path(args.db))
    if args.command == "create-device":
        device, key = store.add_device(args.workspace)
        print(f"Equipo {device}\nClave {key}")
    elif args.command in ("grant", "revoke"):
        store.grant(args.workspace, args.subject, allow=args.command == "grant")
    else:
        store.revoke_device(args.device, purge=args.command == "purge-device")


if __name__ == "__main__":
    main()
