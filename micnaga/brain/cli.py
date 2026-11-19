"""``micnaga brain <accion>``: la Secretaria desde la linea de comandos.

* ``analyze <carpeta> [--force]``   pasada completa sobre una reunion ya transcrita.
* ``status``                        sesion de Claude Code detectada (``claude auth status``).
* ``live-asr <sesion> [--seconds N] [--threads N]``  proceso hijo del titulo en directo:
  transcribe los primeros N s y escribe una linea JSON en stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="micnaga brain", description="Secretaria (IA)")
    sub = parser.add_subparsers(dest="action", required=True)
    a = sub.add_parser("analyze", help="analizar una reunion")
    a.add_argument("folder", type=Path)
    a.add_argument("--force", action="store_true")
    sub.add_parser("status", help="sesion de Claude Code")
    live = sub.add_parser("live-asr", help="(interno) ASR de los primeros segundos")
    live.add_argument("session", type=Path)
    live.add_argument("--seconds", type=float, default=90.0)
    live.add_argument("--threads", type=int, default=2)
    args = parser.parse_args(argv)

    if args.action == "live-asr":
        from micnaga.brain.live_title import transcribe_head

        out = transcribe_head(args.session, args.seconds, args.threads)
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        return 0
    from micnaga.config import load_config

    cfg = load_config()
    if args.action == "status":
        from micnaga.brain.claude import ClaudeRunner

        info = ClaudeRunner(auth=cfg.secretaria.auth).auth_status()  # type: ignore[arg-type]
        keep = ("loggedIn", "authMethod", "subscriptionType")
        print(json.dumps({k: info.get(k) for k in keep}, ensure_ascii=False))
        return 0 if info.get("loggedIn") else 1
    from micnaga.brain.secretary import Secretary

    res = Secretary(cfg).analyze(args.folder.resolve(), force=args.force)
    print(
        json.dumps(
            {
                "folder": res.folder.name,
                "titulo": res.acta.get("titulo"),
                "propuestas": res.proposals,
            },
            ensure_ascii=False,
        )
    )
    return 0
