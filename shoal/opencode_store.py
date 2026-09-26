"""The opencode message store as shoal reads it (read-only), across store versions.

opencode 1.x keeps messages in `message` (session_id, time_created, data). opencode 2.x
migrates the store in place on first start: it copies them into `session_message`
(session_id, type, seq, time_created, data) beside `session_v2`, and keeps the 1.x tables,
which then stop growing. A reader that kept querying `message` after the upgrade would see
a quiet world forever, so every reader asks this module which table to use.

In a 2.x store a failed provider send is an `assistant` row whose data carries an `error`
object; `user` rows are excluded because a person quoting an error message is not an error.
"""
from __future__ import annotations

import os
import sqlite3


def db_path() -> str:
    """`$OPENCODE_DB` (honoured by opencode 2.x as well), else the default store."""
    return os.environ.get("OPENCODE_DB") or os.path.join(
        os.path.expanduser("~"), ".local", "share", "opencode", "opencode.db")


def connect_ro(path: str = "", timeout: float = 20) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path or db_path()}?mode=ro", uri=True, timeout=timeout)


def layout(con: sqlite3.Connection) -> str:
    """"v2" when the 2.x session tables exist, else "v1"."""
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return "v2" if {"session_v2", "session_message"} <= tables else "v1"


def message_source(con: sqlite3.Connection) -> tuple[str, str, str]:
    """(layout, table, extra WHERE clause starting with AND, or "") for message scans."""
    lay = layout(con)
    if lay == "v2":
        return lay, "session_message", " AND type != 'user'"
    return lay, "message", ""
