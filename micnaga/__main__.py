"""`python -m micnaga ...` equivale al comando `micnaga ...`."""

import sys

from micnaga.cli import main

if __name__ == "__main__":
    sys.exit(main())
