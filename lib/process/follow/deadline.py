import signal
import threading
from contextlib import contextmanager

from lib.refusal import Refusal

TIMEOUT = 8


class Unverified(Refusal):
    pass


def expired(number, frame):
    raise Unverified("follow receiver exceeded its wall-clock budget; dispatch acceptance is unknown")


@contextmanager
def limit():
    if not hasattr(signal, "setitimer") or threading.current_thread() is not threading.main_thread():
        raise Unverified("follow receiver requires a POSIX synchronous worker")
    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise Unverified("follow receiver refuses an existing process timer")
    previous = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, TIMEOUT)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
