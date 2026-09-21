"""LEGACY (pre-2026-09-20 QUEUE/STATUS HAGs). Hash the QUEUE section of a queue file and
list its item ids (bold headings).

Prints ``<sha256[:12]> <n>`` on the first line, then one ``ITEM <id>`` line per numbered item.
Exit 2 with an ERROR line when the section cannot be read or is empty: an empty snapshot is
an error, never a baseline.
"""
import hashlib
import re
import sys


class QueueReadError(RuntimeError):
    pass


def read_queue(path: str) -> tuple[str, list[str]]:
    """Return (sha12, [item ids]) for the QUEUE section; raise on unreadable/empty.

    An empty snapshot is an error, never a baseline.
    """
    try:
        text = open(path).read()
        queue = text[text.index("## QUEUE"): text.index("## STATUS")]
    except Exception as exc:  # noqa: BLE001
        raise QueueReadError(f"{path}: {exc}") from exc
    if not queue.strip():
        raise QueueReadError(f"{path}: empty QUEUE section")
    ids = [re.sub(r"\s+", " ", m).strip()
           for m in re.findall(r"^\d+\.\s+\*\*(.+?)\*\*", queue, flags=re.M)]
    return hashlib.sha256(queue.encode()).hexdigest()[:12], ids


def main(argv: list[str]) -> int:
    try:
        h, ids = read_queue(argv[1])
    except (IndexError, QueueReadError) as exc:
        print("ERROR", exc)
        return 2
    print(h, len(ids))
    for item in ids:
        print("ITEM", item)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
