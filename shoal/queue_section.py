"""Hash the QUEUE section of a queue file and list its item ids (bold headings).

Prints ``<sha256[:12]> <n>`` on the first line, then one ``ITEM <id>`` line per numbered item.
Exit 2 with an ERROR line when the section cannot be read or is empty: an empty snapshot is
an error, never a baseline.
"""
import hashlib
import re
import sys

path = sys.argv[1]
try:
    text = open(path).read()
    queue = text[text.index("## QUEUE") : text.index("## STATUS")]
except Exception as exc:  # noqa: BLE001
    print("ERROR", exc)
    sys.exit(2)
if not queue.strip():
    print("ERROR empty QUEUE section")
    sys.exit(2)
ids = [re.sub(r"\s+", " ", m).strip() for m in re.findall(r"^\d+\.\s+\*\*(.+?)\*\*", queue, flags=re.M)]
print(hashlib.sha256(queue.encode()).hexdigest()[:12], len(ids))
for item in ids:
    print("ITEM", item)
