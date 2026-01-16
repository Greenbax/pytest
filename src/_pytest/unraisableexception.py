from __future__ import annotations

import collections
from collections.abc import Callable
import functools
import gc
import sys
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


# This is a stash item and not a simple constant to allow pytester to override it.
gc_collect_iterations_key = StashKey[int]()


def unraisable_exception_runtest_hook() -> Generator[None]:
    with catch_unraisable_exception() as cm:
        try:
            yield
        finally:
            if cm.unraisable:
                if cm.unraisable.err_msg is not None:
                    err_msg = cm.unraisable.err_msg
                else:
                    err_msg = "Exception ignored in"
                msg = f"{err_msg}: {cm.unraisable.object!r}\n\n"
                msg += "".join(
                    traceback.format_exception(
                        cm.unraisable.exc_type,
                        cm.unraisable.exc_value,
                        cm.unraisable.exc_traceback,
                    )
                )
                warnings.warn(pytest.PytestUnraisableExceptionWarning(msg))
            except pytest.PytestUnraisableExceptionWarning as e:
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
            raise ExceptionGroup("multiple unraisable exception warnings", errors)
    finally:
        del errors, meta, hook_error


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_setup() -> Generator[None]:
    yield from unraisable_exception_runtest_hook()


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_call() -> Generator[None]:
    yield from unraisable_exception_runtest_hook()


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown() -> Generator[None]:
    yield from unraisable_exception_runtest_hook()
