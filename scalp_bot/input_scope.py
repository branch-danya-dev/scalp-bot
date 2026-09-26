"""Processing scopes are causal spans, not additional replay commands."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from inspect import iscoroutinefunction, signature
import asyncio


class InputScopes:
    def __init__(self, journal):
        self.journal = journal
        self.current = ContextVar(f"input_scope_{id(self)}", default=None)
        self.serial = 0

    @contextmanager
    def enter(self, name, symbol=None):
        self.serial += 1
        identity = self.serial
        self.journal.append("scope", symbol, {"phase": "begin", "id": identity,
            "parentId": self.current.get(), "name": name})
        token = self.current.set(identity)
        outcome = "returned"
        try:
            yield identity
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except BaseException:
            outcome = "raised"
            raise
        finally:
            self.current.reset(token)
            self.journal.append("scope", symbol, {"phase": "end", "id": identity, "outcome": outcome})


def input_scope(name, *, symbol_arg=False):
    def decorate(fn):
        argument = list(signature(fn).parameters)[1] if symbol_arg else None
        def symbol(args, kwargs):
            if not symbol_arg:
                return None
            value = args[0] if args else kwargs.get(argument)
            return value if isinstance(value, str) else getattr(value, "symbol", None)

        if iscoroutinefunction(fn):
            @wraps(fn)
            async def wrapped(self, *args, **kwargs):
                if self.input_scopes is None:
                    return await fn(self, *args, **kwargs)
                with self.input_scopes.enter(name, symbol(args, kwargs)):
                    return await fn(self, *args, **kwargs)
        else:
            @wraps(fn)
            def wrapped(self, *args, **kwargs):
                if self.input_scopes is None:
                    return fn(self, *args, **kwargs)
                with self.input_scopes.enter(name, symbol(args, kwargs)):
                    return fn(self, *args, **kwargs)
        return wrapped
    return decorate
