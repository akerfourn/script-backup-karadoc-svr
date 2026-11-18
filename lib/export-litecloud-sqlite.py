"""Stream a private, consistent copy of the running LiteCloud database."""

import shutil
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path


def export(source: Path) -> None:
    """Include committed WAL transactions in a standalone database file."""
    source_uri = source.resolve(strict=True).as_uri() + "?mode=ro"
    deadline = time.monotonic() + 300

    def check_deadline(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("délai de création de la copie SQLite dépassé")

    with tempfile.TemporaryDirectory(prefix="litecloud-db-") as temporary:
        snapshot = Path(temporary) / "snapshot.sqlite3"
        with closing(sqlite3.connect(source_uri, uri=True)) as live:
            with closing(sqlite3.connect(snapshot)) as copy:
                live.backup(copy, pages=256, progress=check_deadline)
                if copy.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
                    raise ValueError("impossible de produire une base SQLite autonome")
        with snapshot.open("rb") as data:
            shutil.copyfileobj(data, sys.stdout.buffer)
            sys.stdout.buffer.flush()


if __name__ == "__main__":
    try:
        export(Path(sys.argv[1]))
    except (OSError, sqlite3.Error, ValueError) as error:
        sys.exit(f"Export SQLite LiteCloud : {error}")
