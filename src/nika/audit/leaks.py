"""Fault-name patterns shared by runtime probes and trajectory audits."""

_NATIVE_KEYWORDS = frozenset({"frag[-_]needed"})


def fault_tokens(fault: str) -> list[str]:
    """Regex alternatives that name ``fault`` (registry id and its phrases).

    Every run of two or more consecutive words of the id, joined by ``_`` or
    ``-``, plus the whole id without separators when it has three or more
    words (class names such as ``HostIncorrectDns``). Single words are left
    out: ``dns`` or ``loss`` appear in any healthy config.
    """
    words = [w for w in fault.lower().split("_") if w]
    tokens: list[str] = []
    for size in range(len(words), 1, -1):
        for start in range(len(words) - size + 1):
            tokens.append("[-_]".join(words[start : start + size]))
    if len(words) >= 3:
        tokens.append("".join(words))
    return [t for t in dict.fromkeys(tokens) if t not in _NATIVE_KEYWORDS]
