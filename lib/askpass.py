#!/usr/bin/env python3
"""Supply a password to OpenSSH or sudo through their askpass protocol."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from credentials import request_secret


def main() -> int:
    """Write only the requested password to the requesting program's pipe."""
    try:
        secret = request_secret(sys.argv[1])
        if secret == "":
            return 1
        sys.stdout.write(secret + "\n")
    except (OSError, ValueError, KeyError):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
