"""Empaqueta el conector de Claude Desktop con la biblioteca estándar."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "packaging" / "claude"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--local-cli", type=Path, help="Ejecutable local para un paquete de prueba")
    args = parser.parse_args()
    manifest = json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))
    if args.local_cli is not None:
        cli = args.local_cli.resolve()
        if cli.name.lower() not in {"micnaga.exe", "micnaga-cli.exe"} or not cli.is_file():
            parser.error(f"no existe un ejecutable de Micnaga en {cli}")
        manifest["user_config"]["micnaga_cli"]["default"] = str(cli)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(args.output, "w", compression=ZIP_DEFLATED) as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        bundle.write(SOURCE / "server.js", "server.js")
        bundle.write(ROOT / "LICENSE", "LICENSE")
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
