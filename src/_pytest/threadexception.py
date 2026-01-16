from __future__ import annotations

import collections
from collections.abc import Callable
import functools
import sys
import threading
import traceback
from typing import NamedTuple
from typing import TYPE_CHECKING
import warnings

from _pytest.config import Config
from _pytest.nodes import Item
from _pytest.stash import StashKey
from _pytest.tracemalloc import tracemalloc_message
import pytest


if TYPE_CHECKING:
    pass

if sys.version_info < (3, 11):
    from exceptiongroup import ExceptionGroup


class ThreadExceptionMeta(NamedTuple):
    msg: str
    cause_msg: str
    exc_value: BaseException | None


def thread_exception_runtest_hook() -> Generator[None]:
    with catch_threading_exception() as cm:
        try:
            yield
        finally:
            if cm.args:
                thread_name = (
                    "<unknown>" if cm.args.thread is None else cm.args.thread.name
                )
                msg = f"Exception in thread {thread_name}\n\n"
                msg += "".join(
                    traceback.format_exception(
                        cm.args.exc_type,
                        cm.args.exc_value,
                        cm.args.exc_traceback,
                    )
                )
                warnings.warn(pytest.PytestUnhandledThreadExceptionWarning(msg))
            except pytest.PytestUnhandledThreadExceptionWarning as e:
                # This except happens when the warning is treated as an error (e.g. `-Werror`).
                if meta.exc_value is not None:
                    # Exceptions have a better way to show the traceback, but
                    # warnings do not, so hide the traceback from the msg and
                    # set the cause so the traceback shows up in the right place.
                    e.args = (meta.cause_msg,)
                    e.__cause__ = meta.exc_value
                errors.append(e)

        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup("multiple thread exception warnings", errors)
    finally:
        del errors, meta, hook_error


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtest_setup() -> Generator[None]:
    yield from thread_exception_runtest_hook()


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_call() -> Generator[None]:
    yield from thread_exception_runtest_hook()


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown() -> Generator[None]:
    yield from thread_exception_runtest_hook()
