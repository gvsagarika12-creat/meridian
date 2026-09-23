"""A small in-process cache, and a short list of what may go in it.

The app runs in two shapes and they cache differently:

* **The desktop copy** is one process. A value cached here is seen by every
  request, and clearing it clears it for everybody.
* **The hosted copy** is serverless. Several instances answer requests at once,
  each with its own memory, and they are started and discarded constantly. A
  value cached in one is invisible to the others, and an instance that has just
  started has an empty cache.

That difference is the whole design constraint, and it rules two things out.

**Nothing security-critical may live here.** A failed-login counter kept in
process memory gives an attacker one fresh allowance per instance, which is not
a lockout; that belongs in the database. The same reasoning covers anything the
app *refuses* on - a refusal that a second instance does not know about is not
a refusal.

**Nothing clinical may live here.** A stale medication list is not a slow page,
it is a wrong one, and no page-load saving is worth a clinician reading last
week's allergies. The right fix for a slow clinical screen is to ask the
database for less, not to remember more - see `records_for_many`, which removed
far more time than any cache here will.

What is left is the small, cheap, self-refreshing stuff: the counts beside the
navigation, which every page draws and nobody makes a decision on. Being a few
seconds out of date costs nothing there, and the badge is right again on the
next tick.

So: short TTLs, tiny values, and an explicit `drop()` on the write paths that
would otherwise leave a badge visibly wrong while somebody is looking at it.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

#  Deliberately short. A long TTL on a serverless platform means one instance
#  serving a number another instance already knows is wrong, and the badge
#  disagreeing with the page it sits next to is worse than the query it saved.
DEFAULT_TTL = 20.0

_lock = threading.Lock()
_store: dict[str, tuple[float, Any]] = {}

#  Counted, not logged. "Is the cache doing anything?" deserves an answer that
#  is not a guess, and a counter costs nothing; a log line per hit would be
#  noise at one per page draw.
_stats = {"hits": 0, "misses": 0, "drops": 0}


def get_or_set(key: str, produce: Callable[[], Any], ttl: float = DEFAULT_TTL) -> Any:
    """The cached value for `key`, computing and storing it if it is absent.

    `produce` is called outside the lock. Holding a lock across a database query
    would serialise every request behind the slowest one - a cache that makes
    the app slower under load is a cache that should not have been added.
    """
    now = time.monotonic()
    with _lock:
        hit = _store.get(key)
        if hit and hit[0] > now:
            _stats["hits"] += 1
            return hit[1]
        _stats["misses"] += 1

    value = produce()

    with _lock:
        _store[key] = (now + ttl, value)
    return value


def drop(*keys: str) -> None:
    """Forget these keys now, because something just made them wrong."""
    with _lock:
        for key in keys:
            if _store.pop(key, None) is not None:
                _stats["drops"] += 1


def drop_prefix(prefix: str) -> None:
    with _lock:
        for key in [k for k in _store if k.startswith(prefix)]:
            del _store[key]
            _stats["drops"] += 1


def clear() -> None:
    with _lock:
        _store.clear()


def stats() -> dict:
    """Hits, misses and size - for a diagnostics screen or a test."""
    with _lock:
        total = _stats["hits"] + _stats["misses"]
        return {**_stats, "entries": len(_store),
                "hit_rate": round(_stats["hits"] / total, 3) if total else 0.0}


# --- the keys that are allowed to exist ---------------------------------
#
# Named here rather than spelled at each call site. A cache keyed by strings
# typed in by hand acquires a second spelling of one key eventually, and the two
# then disagree forever in a way that reads as a caching bug rather than a typo.

NAV_UNREAD = "nav:unread"
NAV_DUE = "nav:due"


def drop_nav() -> None:
    """Both navigation badges, after something that changes either.

    One function rather than two calls, because the two are always wrong
    together in practice - a submission arriving moves the unread count and can
    move the reminder count, and remembering only one of them at the call site
    is the bug this exists to prevent.
    """
    drop(NAV_UNREAD, NAV_DUE)
