"""Number formatting for everything a reader sees: Indian digit grouping (1,64,986)."""
from __future__ import annotations


def indian(v) -> str:
    """An integer with Indian grouping: 999, 1,000, 1,00,000, 1,64,986. None -> "—"."""
    if v is None:
        return "—"
    n = int(v)
    sign, s = ("-" if n < 0 else ""), str(abs(n))
    if len(s) <= 3:
        return sign + s
    head, tail = s[:-3], s[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    return sign + ",".join([head, *groups, tail])


def pct(v, digits: int = 1) -> str:
    return "—" if v is None else f"{v:.{digits}f}%"
