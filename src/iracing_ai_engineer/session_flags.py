"""Context-bound lap flag policy; never rewrite the captured SDK bitfield."""

# Yellow/red, waving yellow, pre-green/caution, DQ/repair and start lights.
UNSUITABLE_LAP_FLAGS = (
    0x0008 | 0x0010 | 0x0100 | 0x0200 | 0x0400 | 0x4000 | 0x8000
    | 0x020000 | 0x100000 | 0x200000 | 0x20000000 | 0x40000000
)


def unsuitable_lap_flags(session_type=None, session_state=None):
    """Only active offline testing can ignore its persistent pre-green bit.

    The caller must bind SessionType to the exact frame's SessionInfo update.
    SessionState must be the directly read integer Racing (4), not a guess
    from speed or lap progress. Unknown contexts keep the original mask;
    caution, repair, start lights and other flags are never exempted.
    """
    if (session_type == "Offline Testing" and type(session_state) is int
            and session_state == 4):
        return UNSUITABLE_LAP_FLAGS & ~0x0200
    return UNSUITABLE_LAP_FLAGS
