# mypy: allow-untyped-defs
"""Discover and run std-library "unittest" style tests."""

from __future__ import annotations

import inspect
import sys
import traceback
import types
from typing import Any
from typing import Callable
from typing import Generator
from typing import Iterable
from typing import Tuple
from typing import Type
from typing import Any
from typing import TYPE_CHECKING
from unittest import TestCase
from typing import Union

import _pytest._code
from _pytest._code import ExceptionInfo
from _pytest.compat import assert_never
from _pytest.compat import is_async_function
from _pytest.config import hookimpl
from _pytest.fixtures import FixtureRequest
from _pytest.nodes import Collector
from _pytest.nodes import Item
from _pytest.outcomes import exit
from _pytest.outcomes import fail
from _pytest.outcomes import skip
from _pytest.outcomes import xfail
from _pytest.python import Class
from _pytest.python import Function
from _pytest.python import Module
from _pytest.runner import CallInfo
from _pytest.runner import check_interactive_exception
from _pytest.subtests import SubtestContext
from _pytest.subtests import SubtestReport
import pytest


if sys.version_info[:2] < (3, 11):
    from exceptiongroup import ExceptionGroup

if TYPE_CHECKING:
    from types import TracebackType
    import unittest

    import twisted.trial.unittest


_SysExcInfoType = Union[
    Tuple[Type[BaseException], BaseException, types.TracebackType],
    Tuple[None, None, None],
]


def pytest_pycollect_makeitem(
    collector: Module | Class, name: str, obj: object
) -> UnitTestCase | None:
    try:
        # Has unittest been imported?
        ut = sys.modules["unittest"]
        # Is obj a subclass of unittest.TestCase?
        # Type ignored because `ut` is an opaque module.
        if not issubclass(obj, ut.TestCase):  # type: ignore
            return None
    except Exception:
        return None
    # Is obj a concrete class?
    # Abstract classes can't be instantiated so no point collecting them.
    if inspect.isabstract(obj):
        return None
    # Yes, so let's collect it.
    return UnitTestCase.from_parent(collector, name=name, obj=obj)


class UnitTestCase(Class):
    # Marker for fixturemanger.getfixtureinfo()
    # to declare that our children do not support funcargs.
    nofuncargs = True

    def newinstance(self):
        # TestCase __init__ takes the method (test) name. The TestCase
        # constructor treats the name "runTest" as a special no-op, so it can be
        # used when a dummy instance is needed. While unittest.TestCase has a
        # default, some subclasses omit the default (#9610), so always supply
        # it.
        return self.obj("runTest")

    def collect(self) -> Iterable[Item | Collector]:
        from unittest import TestLoader

        cls = self.obj
        if not getattr(cls, "__test__", True):
            return

        skipped = _is_skipped(cls)
        if not skipped:
            self._register_unittest_setup_method_fixture(cls)
            self._register_unittest_setup_class_fixture(cls)
            self._register_setup_class_fixture()

        self.session._fixturemanager.parsefactories(self.newinstance(), self.nodeid)

        loader = TestLoader()
        foundsomething = False
        for name in loader.getTestCaseNames(self.obj):
            x = getattr(self.obj, name)
            if not getattr(x, "__test__", True):
                continue
            yield TestCaseFunction.from_parent(self, name=name)
            foundsomething = True

        if not foundsomething:
            runtest = getattr(self.obj, "runTest", None)
            if runtest is not None:
                ut = sys.modules.get("twisted.trial.unittest", None)
                if ut is None or runtest != ut.TestCase.runTest:
                    yield TestCaseFunction.from_parent(self, name="runTest")

    def _register_unittest_setup_class_fixture(self, cls: type) -> None:
        """Register an auto-use fixture to invoke setUpClass and
        tearDownClass (#517)."""
        setup = getattr(cls, "setUpClass", None)
        teardown = getattr(cls, "tearDownClass", None)
        if setup is None and teardown is None:
            return None
        cleanup = getattr(cls, "doClassCleanups", lambda: None)

        def process_teardown_exceptions() -> None:
            # tearDown_exceptions is a list set in the class containing exc_infos for errors during
            # teardown for the class.
            exc_infos = getattr(cls, "tearDown_exceptions", None)
            if not exc_infos:
                return
            exceptions = [exc for (_, exc, _) in exc_infos]
            # If a single exception, raise it directly as this provides a more readable
            # error (hopefully this will improve in #12255).
            if len(exceptions) == 1:
                raise exceptions[0]
            else:
                raise ExceptionGroup("Unittest class cleanup errors", exceptions)

        def unittest_setup_class_fixture(
            request: FixtureRequest,
        ) -> Generator[None]:
            cls = request.cls
            if _is_skipped(cls):
                reason = cls.__unittest_skip_why__
                raise pytest.skip.Exception(reason, _use_item_location=True)
            if setup is not None:
                try:
                    setup()
                # unittest does not call the cleanup function for every BaseException, so we
                # follow this here.
                except Exception:
                    cleanup()
                    process_teardown_exceptions()
                    raise
            yield
            try:
                if teardown is not None:
                    teardown()
            finally:
                cleanup()
                process_teardown_exceptions()

        self.session._fixturemanager._register_fixture(
            # Use a unique name to speed up lookup.
            name=f"_unittest_setUpClass_fixture_{cls.__qualname__}",
            func=unittest_setup_class_fixture,
            nodeid=self.nodeid,
            scope="class",
            autouse=True,
        )

    def _register_unittest_setup_method_fixture(self, cls: type) -> None:
        """Register an auto-use fixture to invoke setup_method and
        teardown_method (#517)."""
        setup = getattr(cls, "setup_method", None)
        teardown = getattr(cls, "teardown_method", None)
        if setup is None and teardown is None:
            return None

        def unittest_setup_method_fixture(
            request: FixtureRequest,
        ) -> Generator[None]:
            self = request.instance
            if _is_skipped(self):
                reason = self.__unittest_skip_why__
                raise pytest.skip.Exception(reason, _use_item_location=True)
            if setup is not None:
                setup(self, request.function)
            yield
            if teardown is not None:
                teardown(self, request.function)

        self.session._fixturemanager._register_fixture(
            # Use a unique name to speed up lookup.
            name=f"_unittest_setup_method_fixture_{cls.__qualname__}",
            func=unittest_setup_method_fixture,
            nodeid=self.nodeid,
            scope="function",
            autouse=True,
        )


class TestCaseFunction(Function):
    nofuncargs = True
    failfast = False
    _excinfo: list[_pytest._code.ExceptionInfo[BaseException]] | None = None

    def _getinstance(self):
        assert isinstance(self.parent, UnitTestCase)
        return self.parent.obj(self.name)

    # Backward compat for pytest-django; can be removed after pytest-django
    # updates + some slack.
    @property
    def _testcase(self):
        return self.instance

    def setup(self) -> None:
        # A bound method to be called during teardown() if set (see 'runtest()').
        self._explicit_tearDown: Callable[[], None] | None = None
        super().setup()

    def teardown(self) -> None:
        if self._explicit_tearDown is not None:
            self._explicit_tearDown()
            self._explicit_tearDown = None
        self._obj = None
        del self._instance
        super().teardown()

    def startTest(self, testcase: unittest.TestCase) -> None:
        pass

    def _addexcinfo(self, rawexcinfo: _SysExcInfoType) -> None:
        # Unwrap potential exception info (see twisted trial support below).
        rawexcinfo = getattr(rawexcinfo, "_rawexcinfo", rawexcinfo)
        try:
            excinfo = _pytest._code.ExceptionInfo[BaseException].from_exc_info(
                rawexcinfo  # type: ignore[arg-type]
            )
            # Invoke the attributes to trigger storing the traceback
            # trial causes some issue there.
            _ = excinfo.value
            _ = excinfo.traceback
        except TypeError:
            try:
                try:
                    values = traceback.format_exception(*rawexcinfo)
                    values.insert(
                        0,
                        "NOTE: Incompatible Exception Representation, "
                        "displaying natively:\n\n",
                    )
                    fail("".join(values), pytrace=False)
                except (fail.Exception, KeyboardInterrupt):
                    raise
                except BaseException:
                    fail(
                        "ERROR: Unknown Incompatible Exception "
                        f"representation:\n{rawexcinfo!r}",
                        pytrace=False,
                    )
            except KeyboardInterrupt:
                raise
            except fail.Exception:
                excinfo = _pytest._code.ExceptionInfo.from_current()
        self.__dict__.setdefault("_excinfo", []).append(excinfo)

    def addError(
        self, testcase: unittest.TestCase, rawexcinfo: _SysExcInfoType
    ) -> None:
        try:
            if isinstance(rawexcinfo[1], exit.Exception):
                exit(rawexcinfo[1].msg)
        except TypeError:
            pass
        self._addexcinfo(rawexcinfo)

    def addFailure(
        self, testcase: unittest.TestCase, rawexcinfo: _SysExcInfoType
    ) -> None:
        self._addexcinfo(rawexcinfo)

    def addSkip(
        self, testcase: unittest.TestCase, reason: str, *, handle_subtests: bool = True
    ) -> None:
        from unittest.case import _SubTest  # type: ignore[attr-defined]

        def add_skip() -> None:
            try:
                raise skip.Exception(reason, _use_item_location=True)
            except skip.Exception:
                self._addexcinfo(sys.exc_info())

        if not handle_subtests:
            add_skip()
            return

        if isinstance(testcase, _SubTest):
            add_skip()
            if self._excinfo is not None:
                exc_info = self._excinfo[-1]
                self.addSubTest(testcase.test_case, testcase, exc_info)
        else:
            # For python < 3.11: the non-subtest skips have to be added by `add_skip` only after all subtest
            # failures are processed by `_addSubTest`: `self.instance._outcome` has no attribute
            # `skipped/errors` anymore.
            # We also need to check if `self.instance._outcome` is `None` (this happens if the test
            # class/method is decorated with `unittest.skip`, see pytest-dev/pytest-subtests#173).
            if sys.version_info < (3, 11) and self.instance._outcome is not None:
                subtest_errors = [
                    x
                    for x, y in self.instance._outcome.errors
                    if isinstance(x, _SubTest) and y is not None
                ]
                if len(subtest_errors) == 0:
                    add_skip()
            else:
                add_skip()

    def addExpectedFailure(
        self,
        testcase: unittest.TestCase,
        rawexcinfo: _SysExcInfoType,
        reason: str = "",
    ) -> None:
        try:
            xfail(str(reason))
        except xfail.Exception:
            self._addexcinfo(sys.exc_info())

    def addUnexpectedSuccess(
        self,
        testcase: unittest.TestCase,
        reason: twisted.trial.unittest.Todo | None = None,
    ) -> None:
        msg = "Unexpected success"
        if reason:
            msg += f": {reason.reason}"
        # Preserve unittest behaviour - fail the test. Explicitly not an XPASS.
        try:
            fail(msg, pytrace=False)
        except fail.Exception:
            self._addexcinfo(sys.exc_info())

    def addSuccess(self, testcase: unittest.TestCase) -> None:
        pass

    def stopTest(self, testcase: unittest.TestCase) -> None:
        pass

    def addDuration(self, testcase: unittest.TestCase, elapsed: float) -> None:
        pass

    def runtest(self) -> None:
        from _pytest.debugging import maybe_wrap_pytest_function_for_tracing

        testcase = self.instance
        assert testcase is not None

        maybe_wrap_pytest_function_for_tracing(self)

        # Let the unittest framework handle async functions.
        if is_async_function(self.obj):
            testcase(result=self)
        else:
            # When --pdb is given, we want to postpone calling tearDown() otherwise
            # when entering the pdb prompt, tearDown() would have probably cleaned up
            # instance variables, which makes it difficult to debug.
            # Arguably we could always postpone tearDown(), but this changes the moment where the
            # TestCase instance interacts with the results object, so better to only do it
            # when absolutely needed.
            # We need to consider if the test itself is skipped, or the whole class.
            assert isinstance(self.parent, UnitTestCase)
            skipped = _is_skipped(self.obj) or _is_skipped(self.parent.obj)
            if self.config.getoption("usepdb") and not skipped:
                self._explicit_tearDown = testcase.tearDown
                setattr(testcase, "tearDown", lambda *args: None)

            # We need to update the actual bound method with self.obj, because
            # wrap_pytest_function_for_tracing replaces self.obj by a wrapper.
            setattr(testcase, self.name, self.obj)
            try:
                testcase(result=self)
            finally:
                delattr(testcase, self.name)

    def _traceback_filter(
        self, excinfo: _pytest._code.ExceptionInfo[BaseException]
    ) -> _pytest._code.Traceback:
        traceback = super()._traceback_filter(excinfo)
        ntraceback = traceback.filter(
            lambda x: not x.frame.f_globals.get("__unittest"),
        )
        if not ntraceback:
            ntraceback = traceback
        return ntraceback

    def addSubTest(
        self,
        test_case: Any,
        test: TestCase,
        exc_info: ExceptionInfo[BaseException]
        | tuple[type[BaseException], BaseException, TracebackType]
        | None,
    ) -> None:
        exception_info: ExceptionInfo[BaseException] | None
        match exc_info:
            case tuple():
                exception_info = ExceptionInfo(exc_info, _ispytest=True)
            case ExceptionInfo() | None:
                exception_info = exc_info
            case unreachable:
                assert_never(unreachable)

        call_info = CallInfo[None](
            None,
            exception_info,
            start=0,
            stop=0,
            duration=0,
            when="call",
            _ispytest=True,
        )
        msg = test._message if isinstance(test._message, str) else None  # type: ignore[attr-defined]
        report = self.ihook.pytest_runtest_makereport(item=self, call=call_info)
        sub_report = SubtestReport._new(
            report,
            SubtestContext(msg=msg, kwargs=dict(test.params)),  # type: ignore[attr-defined]
            captured_output=None,
            captured_logs=None,
        )
        self.ihook.pytest_runtest_logreport(report=sub_report)
        if check_interactive_exception(call_info, sub_report):
            self.ihook.pytest_exception_interact(
                node=self, call=call_info, report=sub_report
            )

        # For python < 3.11: add non-subtest skips once all subtest failures are processed by # `_addSubTest`.
        if sys.version_info < (3, 11):
            from unittest.case import _SubTest  # type: ignore[attr-defined]

            non_subtest_skip = [
                (x, y)
                for x, y in self.instance._outcome.skipped
                if not isinstance(x, _SubTest)
            ]
            subtest_errors = [
                (x, y)
                for x, y in self.instance._outcome.errors
                if isinstance(x, _SubTest) and y is not None
            ]
            # Check if we have non-subtest skips: if there are also sub failures, non-subtest skips are not treated in
            # `_addSubTest` and have to be added using `add_skip` after all subtest failures are processed.
            if len(non_subtest_skip) > 0 and len(subtest_errors) > 0:
                # Make sure we have processed the last subtest failure
                last_subset_error = subtest_errors[-1]
                if exc_info is last_subset_error[-1]:
                    # Add non-subtest skips (as they could not be treated in `_addSkip`)
                    for testcase, reason in non_subtest_skip:
                        self.addSkip(testcase, reason, handle_subtests=False)


@hookimpl(tryfirst=True)
def pytest_runtest_makereport(item: Item, call: CallInfo[None]) -> None:
    if isinstance(item, TestCaseFunction):
        if item._excinfo:
            call.excinfo = item._excinfo.pop(0)
            try:
                del call.result
            except AttributeError:
                pass

    # Convert unittest.SkipTest to pytest.skip.
    # This is actually only needed for nose, which reuses unittest.SkipTest for
    # its own nose.SkipTest. For unittest TestCases, SkipTest is already
    # handled internally, and doesn't reach here.
    unittest = sys.modules.get("unittest")
    if unittest and call.excinfo and isinstance(call.excinfo.value, unittest.SkipTest):
        excinfo = call.excinfo
        call2 = CallInfo[None].from_call(
            lambda: pytest.skip(str(excinfo.value)), call.when
        )
        call.excinfo = call2.excinfo


# Twisted trial support.
classImplements_has_run = False


@hookimpl(wrapper=True)
def pytest_runtest_protocol(item: Item) -> Generator[None, object, object]:
    if isinstance(item, TestCaseFunction) and "twisted.trial.unittest" in sys.modules:
        ut: Any = sys.modules["twisted.python.failure"]
        global classImplements_has_run
        Failure__init__ = ut.Failure.__init__
        if not classImplements_has_run:
            from twisted.trial.itrial import IReporter
            from zope.interface import classImplements

            classImplements(TestCaseFunction, IReporter)
            classImplements_has_run = True

        def excstore(
            self, exc_value=None, exc_type=None, exc_tb=None, captureVars=None
        ):
            if exc_value is None:
                self._rawexcinfo = sys.exc_info()
            else:
                if exc_type is None:
                    exc_type = type(exc_value)
                self._rawexcinfo = (exc_type, exc_value, exc_tb)
            try:
                Failure__init__(
                    self, exc_value, exc_type, exc_tb, captureVars=captureVars
                )
            except TypeError:
                Failure__init__(self, exc_value, exc_type, exc_tb)

        ut.Failure.__init__ = excstore
        try:
            res = yield
        finally:
            ut.Failure.__init__ = Failure__init__
    else:
        res = yield
    return res


def _is_skipped(obj) -> bool:
    """Return True if the given object has been marked with @unittest.skip."""
    return bool(getattr(obj, "__unittest_skip__", False))
