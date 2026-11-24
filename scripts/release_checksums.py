"""Genera (o comprueba) ``SHA256SUMS.txt`` para una release de GitHub.

El actualizador de la app (``micnaga/updates.py``) solo ejecuta un instalador cuyo SHA-256
coincide con el publicado. Fuentes, por orden: el activo ``SHA256SUMS.txt`` de la release y el
``digest`` que GitHub calcula para cada activo (si estan los dos, deben coincidir).

Uso (despues de ``scripts\\build.ps1``)::

    python scripts/release_checksums.py dist/installer/Micnaga-Setup-0.1.2.exe
    gh release create v0.1.2 --title "Micnaga 0.1.2" --notes-file notas.md \\
        dist/installer/Micnaga-Setup-0.1.2.exe dist/installer/SHA256SUMS.txt
    python scripts/release_checksums.py --check dist/installer/SHA256SUMS.txt

Formato: el de ``sha256sum`` (``<hex>  <nombre>``, nombre sin ruta), UTF-8, ``\\n``.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

NAME = "SHA256SUMS.txt"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def render(files: list[Path]) -> str:
    return "".join(f"{sha256(f)}  {f.name}\n" for f in sorted(files, key=lambda p: p.name))


def check(sums: Path) -> list[str]:
    """Errores al comprobar ``sums`` contra los ficheros de su misma carpeta."""
    errors: list[str] = []
    for line in sums.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        hexd, _, name = line.partition("  ")
        target = sums.parent / name.strip()
        if not target.is_file():
            errors.append(f"missing: {name}")
        elif sha256(target) != hexd.strip().lower():
            errors.append(f"mismatch: {name}")
    return errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "files", nargs="+", type=Path, help="instaladores (o SHA256SUMS.txt con --check)"
    )
    ap.add_argument("-o", "--output", type=Path, help=f"destino (por defecto, {NAME} junto al 1.º)")
    ap.add_argument("--check", action="store_true", help="comprobar un SHA256SUMS.txt existente")
    args = ap.parse_args(argv)
    if args.check:
        errors = [e for s in args.files for e in check(s)]
        for e in errors:
            print(e, file=sys.stderr)
        return 1 if errors else 0
    missing = [f for f in args.files if not f.is_file()]
    if missing:
        print(f"not found: {', '.join(map(str, missing))}", file=sys.stderr)
        return 2
    out = args.output or args.files[0].parent / NAME
    out.write_text(render(args.files), encoding="utf-8", newline="\n")
    print(out.read_text(encoding="utf-8"), end="")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
