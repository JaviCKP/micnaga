"""Mide una carpeta ``dist/<variante>``: tamano en disco, desglose y estimacion del instalador.

La estimacion comprime TODOS los ficheros como un unico flujo LZMA (equivalente a un instalador
Inno Setup con ``Compression=lzma2/max`` + ``SolidCompression=yes``). Es una **estimacion**: el
instalador real lo mide ``iscc`` y sale bastante mas pequeno (``lzma2/ultra64`` con diccionario de
64 MiB: 69,7 MiB reales frente a 107 MiB estimados sin torch).

Presupuesto: la spec §2 pide < 150 MB sin modelos. Con la diarizacion ``diarize`` + torch CPU
(``torch_cpu.dll`` son 256 MiB por si sola) se sigue cumpliendo solo gracias al recorte del
``.spec`` (DECISIONS §31: 131 MiB estimados, 123,7 MiB reales); sin el, 279 MiB estimados.
``BUDGET_MIB`` avisa si el recorte se pierde.

Uso: python scripts/measure_dist.py dist/micnaga [--no-compress] [--top 15] [--budget MiB]
"""

from __future__ import annotations

import argparse
import lzma
import sys
from pathlib import Path

CHUNK = 1 << 20
BUDGET_MIB = 150.0
"""Limite de la estimacion LZMA (spec §2: instalador < 150 MB sin modelos)."""


def dir_sizes(root: Path) -> tuple[int, dict[str, int]]:
    total = 0
    top: dict[str, int] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        size = path.stat().st_size
        total += size
        rel = path.relative_to(root).parts
        key = "/".join(rel[:2]) if rel[0] == "_internal" and len(rel) > 2 else rel[0]
        top[key] = top.get(key, 0) + size
    return total, top


def lzma_estimate(root: Path, preset: int = 6) -> int:
    """Bytes tras comprimir todos los ficheros (ordenados por extension) como un solo flujo."""
    comp = lzma.LZMACompressor(
        format=lzma.FORMAT_XZ, filters=[{"id": lzma.FILTER_LZMA2, "preset": preset}]
    )
    out = 0
    files = sorted((p for p in root.rglob("*") if p.is_file()), key=lambda p: (p.suffix, p.name))
    for path in files:
        with path.open("rb") as fh:
            while chunk := fh.read(CHUNK):
                out += len(comp.compress(chunk))
    out += len(comp.flush())
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", type=Path)
    parser.add_argument("--no-compress", action="store_true")
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--budget", type=float, default=BUDGET_MIB, help="MiB (estimacion)")
    args = parser.parse_args(argv)
    if not args.folder.is_dir():
        print(f"no existe {args.folder}", file=sys.stderr)
        return 2
    total, top = dir_sizes(args.folder)
    mb = 1024 * 1024
    print(f"{args.folder}: {total / mb:.1f} MiB en disco")
    for name, size in sorted(top.items(), key=lambda kv: -kv[1])[: args.top]:
        print(f"  {size / mb:8.1f} MiB  {name}")
    if not args.no_compress:
        est = lzma_estimate(args.folder)
        verdict = "OK" if est / mb <= args.budget else "SUPERA EL LIMITE"
        print(
            f"estimacion del instalador (LZMA solido): {est / mb:.1f} MiB "
            f"(limite {args.budget:.0f} MiB: {verdict})"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
