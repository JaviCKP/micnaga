"""Linea de comandos ``micnaga``: solo despacha; cada modulo implementa su subcomando.

Convencion (para no tener que editar este fichero)
--------------------------------------------------
``micnaga <comando> [args...]`` importa de forma perezosa ``<modulo>:<funcion>`` y le pasa
el resto de ``argv`` tal cual (sin parsear), asi que cada modulo define su propio
``argparse``, incluido ``--help``::

    def run(argv: list[str]) -> int: ...      # devuelve el codigo de salida

=========  ============================================  =================================
comando    funcion                                       dueno
=========  ============================================  =================================
record     ``micnaga.capture.cli:run``                   capture/
process    ``micnaga.pipeline.cli:run_process``          pipeline/
worker     ``micnaga.pipeline.cli:run_worker``           pipeline/
bench      ``micnaga.bench.cli:run``                     bench/  (``python -m micnaga.bench``)
app        ``micnaga.app:run``                           app.py (bandeja, vigilante, UI)
tools      ``micnaga.brain.mcp:run``                     brain/ (servidor MCP stdio)
brain      ``micnaga.brain.cli:run``                     brain/ (Secretaria)
hook-guard ``micnaga.brain.google.guard:main``           brain/google (gancho PreToolUse)
=========  ============================================  =================================

Si el modulo aun no existe (``ModuleNotFoundError`` de ese modulo o de un paquete
``micnaga.*`` que lo contiene) o la funcion lanza ``NotImplementedError``, se imprime
«not implemented» y se sale con codigo 2. Cualquier otro error de importacion (dependencia
que falta, bug) se propaga.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Callable, Sequence

from micnaga import __version__

COMMANDS: dict[str, tuple[str, str]] = {
    "record": ("micnaga.capture.cli", "run"),
    "process": ("micnaga.pipeline.cli", "run_process"),
    "worker": ("micnaga.pipeline.cli", "run_worker"),
    "bench": ("micnaga.bench.cli", "run"),
    "app": ("micnaga.app", "run"),
    "tools": ("micnaga.brain.mcp", "run"),
    "brain": ("micnaga.brain.cli", "run"),
    "hook-guard": ("micnaga.brain.google.guard", "main"),
}

HELP: dict[str, str] = {
    "record": "grabar micro + sistema en dos pistas",
    "process": "procesar una sesion grabada (transcribir)",
    "worker": "proceso de trabajo: consume la cola de procesado",
    "bench": "banco de pruebas",
    "app": "bandeja, vigilante y ventanas",
    "tools": "servidor MCP de solo lectura de la Secretaria (lo lanza claude -p)",
    "brain": "Secretaria: analizar una reunion, probar Claude/Odoo, titulo en directo",
    "hook-guard": "(interno) gancho PreToolUse: solo deja pasar lo aprobado",
}

EXIT_NOT_IMPLEMENTED = 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="micnaga", description="Grabacion y transcripcion local de reuniones de Meet."
    )
    parser.add_argument("--version", action="version", version=f"micnaga {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<comando>")
    for name, text in HELP.items():
        # add_help=False: `micnaga record --help` lo atiende el modulo, no este parser.
        sub.add_parser(name, help=text, add_help=False)
    return parser


def load_command(name: str) -> Callable[[list[str]], int] | None:
    """Resuelve el subcomando; ``None`` si su modulo todavia no existe."""
    module_name, func_name = COMMANDS[name]
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        # Solo "falta ese modulo (o un paquete micnaga.* que lo contiene)"; una dependencia
        # de terceros ausente tiene otro exc.name y se propaga.
        if exc.name == module_name or (
            exc.name is not None
            and exc.name.startswith("micnaga")
            and module_name.startswith(exc.name + ".")
        ):
            return None
        raise
    func: Callable[[list[str]], int] = getattr(module, func_name)
    return func


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    if not args or args[0] in ("-h", "--help", "--version"):
        parser.parse_args(args or ["--help"])
        return 0
    if args[0] not in COMMANDS:
        parser.parse_args(args)  # error de argparse (sale con codigo 2)
        return EXIT_NOT_IMPLEMENTED  # pragma: no cover
    command, rest = args[0], args[1:]
    func = load_command(command)
    if func is None:
        print(f"micnaga {command}: not implemented", file=sys.stderr)
        return EXIT_NOT_IMPLEMENTED
    try:
        return int(func(rest))
    except NotImplementedError:
        print(f"micnaga {command}: not implemented", file=sys.stderr)
        return EXIT_NOT_IMPLEMENTED


if __name__ == "__main__":
    sys.exit(main())
