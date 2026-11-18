"""Mide una carpeta ``dist/<variante>``: tamano en disco, desglose y estimacion del instalador.

La estimacion comprime TODOS los ficheros como un unico flujo LZMA (equivalente a un instalador
Inno Setup con ``Compression=lzma2/max`` + ``SolidCompression=yes``). Es una **estimacion**: el
instalador real lo mide ``iscc``; este numero sirve para vigilar el presupuesto de la spec §2
(< 150 MB sin modelos).

Uso: python scripts/measure_dist.py dist/micnaga [--no-compress] [--top 15]
"""

from __future__ import annotations

import argparse
import lzma
import sys
from pathlib import Path

CHUNK = 1 << 20


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
        print(f"estimacion del instalador (LZMA solido): {est / mb:.1f} MiB (limite 150 MiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
