"""``python -m micnaga.bench`` equivale a ``micnaga bench`` (logica en ``bench.cli:run``)."""

import sys

from micnaga.cli import main

if __name__ == "__main__":
    sys.exit(main(["bench", *sys.argv[1:]]))
