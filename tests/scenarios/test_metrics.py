# Copyright Axis Communications AB.
#
# For a full list of individual contributors, please see the commit history.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Scenario tests for the metrics recorded during an ETR execution."""

import signal
from functools import partial
from types import SimpleNamespace

import pytest
from etos_lib.lib.debug import Debug
from etos_lib.lib.events import Events

from etos_test_runner import etr as etr_module
from etos_test_runner.etr import ETR
from etos_test_runner.lib import metrics as metrics_module
from etos_test_runner.lib import testrunner
from tests.library.fake_server import FakeServer
from tests.library.handler import Handler
from tests.library.metrics import ALLOWED_ATTRIBUTES, CollectedMetrics
from tests.scenarios.test_full_execution import REGEX, V1_SUITE

PASSING_TEST = """#!/bin/bash
echo ==== test_name_yo ====
echo == STARTED ==
echo == PASSED ==
exit 0
"""

FAILING_TEST = """#!/bin/bash
echo ==== test_name_yo ====
echo == STARTED ==
echo == FAILED ==
exit 1
"""

STARTUPS = "etos.test_runner.startups"
EXECUTIONS = "etos.test_runner.executions"
PUBLICATIONS = "etos.test_runner.result.publications"


@pytest.fixture(name="etr_environment")
def _etr_environment(tmp_path, monkeypatch):
    """Run ETR against a fake server serving a v1 sub suite, without network checkouts."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "regex.json").write_text(REGEX, encoding="utf-8")
    suite = V1_SUITE.model_dump()
    suite["testExecutions"][0]["execution"]["checkout"] = [f"cp {tmp_path}/test.sh ."]
    handler = partial(Handler, suite)
    with FakeServer(handler) as server:
        suite["logArea"]["upload"]["url"] = f"{server.host}/{{name}}"
        for key, value in {
            "ETOS_DISABLE_SENDING_EVENTS": "1",
            "ETOS_DISABLE_RECEIVING_EVENTS": "1",
            "ETOS_GRAPHQL_SERVER": server.host,
            "ENVIRONMENT_ID": "12345678-1234-5678-1234-567812345678",
            "ENVIRONMENT_URL": server.host,
            "TEST_REGEX": str(tmp_path / "regex.json"),
            "HOME": str(tmp_path),
        }.items():
            monkeypatch.setenv(key, value)
        yield tmp_path
    Handler.reset()


def _write_test(path, script):
    """Write the test script that the sub suite executes.

    :param path: Directory to write the script to.
    :param script: The script content.
    """
    (path / "test.sh").write_text(script, encoding="utf-8")


def _assert_single_counts(collected, name, expected):
    """Assert the non-zero counter values and that each has exactly one duration.

    :param collected: Collected metrics.
    :param name: Counter name.
    :param expected: Expected non-zero values keyed by attribute tuples.
    """
    assert collected.nonzero(name) == expected
    histogram = {
        STARTUPS: "etos.test_runner.startup.duration",
        EXECUTIONS: "etos.test_runner.execution.duration",
        PUBLICATIONS: "etos.test_runner.result.publication.duration",
    }[name]
    for attributes, count in expected.items():
        point = collected.histogram(histogram, **dict(attributes))
        assert point.count == count
        assert point.sum >= 0


def _event_names():
    """Get the names of the Eiffel events published by ETR, in order."""
    return [event.meta.type for event in Debug().events_published]


def test_passing_execution(etr_environment):
    """Test the metrics of a successful startup and passing test suite execution.

    Approval criteria:
        - Startup, execution and terminal result publication shall be counted exactly once.
        - Metric attributes shall be bounded.
        - The result and Eiffel events of ETR shall be unchanged.
    """
    _write_test(etr_environment, PASSING_TEST)
    collected = CollectedMetrics()
    result = ETR(collected.metrics).run_etr()
    collected.collect()

    assert result == 0
    assert _event_names()[0] == "EiffelTestSuiteStartedEvent"
    assert _event_names()[-1] == "EiffelTestSuiteFinishedEvent"
    _assert_single_counts(collected, STARTUPS, {(("etos.outcome", "success"),): 1})
    _assert_single_counts(
        collected, EXECUTIONS, {(("etos.conclusion", "successful"), ("etos.verdict", "passed")): 1}
    )
    _assert_single_counts(collected, PUBLICATIONS, {(("etos.outcome", "success"),): 1})
    assert collected.attribute_keys() <= ALLOWED_ATTRIBUTES


def test_failing_execution(etr_environment):
    """Test that a failing test payload is a successful execution with a failed verdict.

    Approval criteria:
        - The execution shall be counted once as conclusion 'successful', verdict 'failed'.
        - The startup and publication shall still be successful.
        - ETR shall still return the outcome.
    """
    _write_test(etr_environment, FAILING_TEST)
    collected = CollectedMetrics()
    result = ETR(collected.metrics).run_etr()
    collected.collect()

    assert result["verdict"] == "FAILED"
    _assert_single_counts(collected, STARTUPS, {(("etos.outcome", "success"),): 1})
    _assert_single_counts(
        collected, EXECUTIONS, {(("etos.conclusion", "successful"), ("etos.verdict", "failed")): 1}
    )
    _assert_single_counts(collected, PUBLICATIONS, {(("etos.outcome", "success"),): 1})


@pytest.mark.usefixtures("etr_environment")
def test_startup_timeout(monkeypatch):
    """Test that a sub suite that never becomes available is a startup timeout.

    Approval criteria:
        - The TimeoutError shall be raised as before.
        - The startup shall be counted once as 'timeout' and nothing else counted.
    """
    monkeypatch.delenv("ENVIRONMENT_URL")
    monkeypatch.setattr(ETR, "get_sub_suite_url", lambda *_: None)
    collected = CollectedMetrics()
    with pytest.raises(TimeoutError):
        ETR(collected.metrics).run_etr()
    collected.collect()

    _assert_single_counts(collected, STARTUPS, {(("etos.outcome", "timeout"),): 1})
    assert not collected.nonzero(EXECUTIONS)
    assert not collected.nonzero(PUBLICATIONS)
    assert _event_names() == []


@pytest.mark.usefixtures("etr_environment")
def test_startup_failure(monkeypatch):
    """Test that a failure to load the sub suite is a startup failure.

    Approval criteria:
        - The exception shall be raised as before.
        - The startup shall be counted once as 'failure' and nothing else counted.
    """

    def _fail(*_):
        """Fail loading the sub suite."""
        raise ValueError("Invalid sub suite with a secret")

    monkeypatch.setattr(ETR, "download_and_load", _fail)
    collected = CollectedMetrics()
    with pytest.raises(ValueError):
        ETR(collected.metrics).run_etr()
    collected.collect()

    _assert_single_counts(collected, STARTUPS, {(("etos.outcome", "failure"),): 1})
    assert not collected.nonzero(EXECUTIONS)
    assert not collected.nonzero(PUBLICATIONS)


def test_execution_error(etr_environment, monkeypatch):
    """Test that an execution error is counted once and its delivery is unconfirmed.

    Approval criteria:
        - The exception shall be raised as before, after the terminal event was sent.
        - The execution shall be counted once as conclusion 'failed', verdict 'inconclusive'.
        - The publication shall be counted once as 'unconfirmed'.
    """
    _write_test(etr_environment, PASSING_TEST)

    def _fail(*_):
        """Fail the test execution."""
        raise RuntimeError("Executor failed")

    monkeypatch.setattr(testrunner.TestRunner, "_run_and_finalize", _fail)
    collected = CollectedMetrics()
    with pytest.raises(RuntimeError):
        ETR(collected.metrics).run_etr()
    collected.collect()

    assert _event_names()[-1] == "EiffelTestSuiteFinishedEvent"
    _assert_single_counts(collected, STARTUPS, {(("etos.outcome", "success"),): 1})
    _assert_single_counts(
        collected,
        EXECUTIONS,
        {(("etos.conclusion", "failed"), ("etos.verdict", "inconclusive")): 1},
    )
    _assert_single_counts(collected, PUBLICATIONS, {(("etos.outcome", "unconfirmed"),): 1})


def test_test_suite_started_failure(etr_environment, monkeypatch):
    """Test the metrics when the test suite started event fails and is resent.

    Approval criteria:
        - The execution shall be counted once as conclusion 'failed', verdict 'inconclusive'.
        - The publication shall be counted once as 'unconfirmed'.
        - Started and finished events shall still be sent in order.
    """
    _write_test(etr_environment, PASSING_TEST)
    original = testrunner.TestRunner.test_suite_started
    calls = []

    def _fail_once(self):
        """Fail the first test suite started event."""
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("Could not send event")
        return original(self)

    monkeypatch.setattr(testrunner.TestRunner, "test_suite_started", _fail_once)
    collected = CollectedMetrics()
    with pytest.raises(RuntimeError):
        ETR(collected.metrics).run_etr()
    collected.collect()

    assert _event_names() == ["EiffelTestSuiteStartedEvent", "EiffelTestSuiteFinishedEvent"]
    _assert_single_counts(
        collected,
        EXECUTIONS,
        {(("etos.conclusion", "failed"), ("etos.verdict", "inconclusive")): 1},
    )
    _assert_single_counts(collected, PUBLICATIONS, {(("etos.outcome", "unconfirmed"),): 1})


def test_terminal_publication_failure(etr_environment, monkeypatch):
    """Test that a failure to send the terminal event is a publication failure.

    Approval criteria:
        - The exception shall be raised as before.
        - The execution shall be counted once with its outcome.
        - The publication shall be counted once as 'failure'.
    """
    _write_test(etr_environment, PASSING_TEST)

    def _fail(*_, **__):
        """Fail sending the test suite finished event."""
        raise ConnectionError("Publisher is down")

    monkeypatch.setattr(Events, "send_test_suite_finished", _fail)
    collected = CollectedMetrics()
    with pytest.raises(ConnectionError):
        ETR(collected.metrics).run_etr()
    collected.collect()

    _assert_single_counts(
        collected, EXECUTIONS, {(("etos.conclusion", "successful"), ("etos.verdict", "passed")): 1}
    )
    _assert_single_counts(collected, PUBLICATIONS, {(("etos.outcome", "failure"),): 1})


def test_terminal_delivery_timeout(etr_environment, monkeypatch):
    """Test that events not delivered in time is a publication timeout.

    Approval criteria:
        - The TimeoutError shall be raised as before.
        - The publication shall be counted once as 'timeout'.
    """
    _write_test(etr_environment, PASSING_TEST)

    def _timeout(*_):
        """Time out waiting for event delivery."""
        raise TimeoutError("Eiffel publisher did not deliver all eiffel events.")

    monkeypatch.setattr(testrunner.TestRunner, "wait_for_events", _timeout)
    collected = CollectedMetrics()
    with pytest.raises(TimeoutError):
        ETR(collected.metrics).run_etr()
    collected.collect()

    _assert_single_counts(
        collected, EXECUTIONS, {(("etos.conclusion", "successful"), ("etos.verdict", "passed")): 1}
    )
    _assert_single_counts(collected, PUBLICATIONS, {(("etos.outcome", "timeout"),): 1})


@pytest.fixture(name="restore_sigterm")
def _restore_sigterm():
    """Restore the SIGTERM handler that ETR installs."""
    previous = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, previous)


@pytest.mark.usefixtures("restore_sigterm")
def test_startup_includes_runner_construction(etr_environment, monkeypatch):
    """Test that the startup duration starts before the runner is constructed.

    Approval criteria:
        - Time spent constructing ETR (e.g. the ETOS library) shall be part of the startup.
        - The startup shall be counted once as 'success' when the runner is ready.
    """
    _write_test(etr_environment, PASSING_TEST)
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(metrics_module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    etos = etr_module.ETOS

    def _slow_etos(*args, **kwargs):
        """Construct the ETOS library, taking 30 seconds."""
        clock.now += 30
        return etos(*args, **kwargs)

    monkeypatch.setattr(etr_module, "ETOS", _slow_etos)
    collected = CollectedMetrics()
    etr = ETR(collected.metrics)
    clock.now += 5
    assert etr.run_etr() == 0
    collected.collect()

    _assert_single_counts(collected, STARTUPS, {(("etos.outcome", "success"),): 1})
    histogram = collected.histogram(
        "etos.test_runner.startup.duration", **{"etos.outcome": "success"}
    )
    assert histogram.sum == 35


@pytest.mark.usefixtures("etr_environment", "restore_sigterm")
def test_runner_construction_failure(monkeypatch):
    """Test that a failure while constructing the runner is a startup failure.

    Approval criteria:
        - The exception shall be raised as before.
        - The startup shall be counted once as 'failure' and nothing else counted.
    """

    def _fail(*_, **__):
        """Fail constructing the ETOS library."""
        raise ConnectionError("Could not connect to a secret host")

    monkeypatch.setattr(etr_module, "ETOS", _fail)
    collected = CollectedMetrics()
    with pytest.raises(ConnectionError):
        ETR(collected.metrics)
    collected.collect()

    _assert_single_counts(collected, STARTUPS, {(("etos.outcome", "failure"),): 1})
    assert not collected.nonzero(EXECUTIONS)
    assert not collected.nonzero(PUBLICATIONS)
    assert collected.attribute_keys() <= ALLOWED_ATTRIBUTES


@pytest.mark.usefixtures("etr_environment", "restore_sigterm")
def test_sigterm_during_runner_construction(monkeypatch):
    """Test that SIGTERM while constructing the runner is a cancelled startup.

    Approval criteria:
        - The SIGTERM handler shall be installed before the ETOS library is constructed.
        - ETRTerminated shall be raised and the startup counted once as 'cancelled'.
    """

    def _terminated(*_, **__):
        """Receive SIGTERM while constructing the ETOS library."""
        signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(etr_module, "ETOS", _terminated)
    collected = CollectedMetrics()
    with pytest.raises(metrics_module.ETRTerminated):
        ETR(collected.metrics)
    collected.collect()

    _assert_single_counts(collected, STARTUPS, {(("etos.outcome", "cancelled"),): 1})
