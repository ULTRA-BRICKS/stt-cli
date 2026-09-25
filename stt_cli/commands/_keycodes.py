"""_keycodes — named curses input codes shared across `configtui`'s handlers.

WHY THIS EXISTS
    A terminal has more than one way to send "Escape", "Enter" or "Backspace", and curses
    hands `getch()` back a bare ``int`` for all of them. `_handle`, `_handle_search` and
    `_prompt` in ``configtui.py`` each re-typed the same six literals (27, 10, 13, 9, 127, 8)
    with nothing to say which control code any of them meant, or to keep three separate
    copies from drifting if a fourth handler needed the same check. One shared module, named
    once, so the next handler this screen grows imports a name instead of re-guessing.

CONTRACT
    Every raw literal these handlers compare against lives here, grouped as the tuple a
    call site actually needs (``ENTER_KEYS``, ``BACKSPACE_KEYS``, ...) rather than as loose
    single constants a call site would have to re-assemble — that grouping is what keeps
    ``configtui.py`` itself free of the bare integers this module exists to name.
"""

from __future__ import annotations

import curses

KEY_ESCAPE = 27
KEY_TAB = 9
KEY_LF = 10  # \n — what most terminals send for Enter
KEY_CR = 13  # \r — what some terminals, and pasted input, send for Enter instead
KEY_DEL = 127  # ASCII delete — what most terminals actually send for the Backspace key
KEY_BS = 8  # ASCII backspace / Ctrl-H — what some terminals send instead of KEY_DEL

# Curses also defines its own symbolic codes (``curses.KEY_ENTER``, ``curses.KEY_BACKSPACE``)
# for the same keys, sent instead of the raw ASCII codes above by some terminals/configs —
# both have to be checked, which is exactly the repetition worth naming once.
ESCAPE_KEYS = (KEY_ESCAPE,)
ENTER_KEYS = (KEY_LF, KEY_CR, curses.KEY_ENTER)
BACKSPACE_KEYS = (curses.KEY_BACKSPACE, KEY_DEL, KEY_BS)

# The printable-ASCII window a typed character (search box, edit prompt) is accepted from —
# space through, but not including, DEL.
PRINTABLE_LOW = 32
PRINTABLE_HIGH = KEY_DEL
