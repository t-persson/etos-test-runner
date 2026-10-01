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
"""Tests for the ETOS Test Runner metrics."""

import signal

import pytest
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics.export import MetricExportResult
from requests.exceptions import ConnectTimeout

from etos_test_runner import etr as etr_module
from etos_test_runner.lib import metrics as metrics_module
from etos_test_runner.lib.metrics import ETRMetrics, ETRTerminated
from tests.library.metrics import ALLOWED_ATTRIBUTES, CollectedMetrics

COUNTERS = {
    "etos.test_runner.startups": "{startup}",
    "etos.test_runner.executions": "{execution}",
    "etos.test_runner.result.publications": "{publication}",
}
HISTOGRAMS = {
    "etos.test_runner.startup.duration": "s",
    "etos.test_runner.execution.duration": "s",
    "etos.test_runner.result.publication.duration": "s",
}


def test_names_units_and_zero_series(monkeypatch):
    """Test that metric names and units are stable and known series start at zero.

    Approval criteria:
        - Counters shall have the documented names and units.
        - Known outcome series shall be reported as zero before anything happens.
    """
    monkeypatch.delenv("ETOS_VERSION", raising=False)
    collected = CollectedMetrics().collect()
    for name, unit in COUNTERS.items():
        assert collected.metric(name).unit == unit
        assert collected.metric(name).data.is_monotonic
        assert not collected.nonzero(name)
    assert set(collected.points("etos.test_runner.startups")) == {
        (("etos.outcome", outcome),) for outcome in ("success", "failure", "timeout", "cancelled")
    }
    assert set(collected.points("etos.test_runner.result.publications")) == {
        (("etos.outcome", outcome),)
        for outcome in ("success", "failure", "timeout", "cancelled", "unconfirmed")
    }
    assert set(collected.points("etos.test_runner.executions")) == {
        (("etos.conclusion", "successful"), ("etos.verdict", "passed")),
        (("etos.conclusion", "successful"), ("etos.verdict", "failed")),
        (("etos.conclusion", "failed"), ("etos.verdict", "inconclusive")),
    }
    # Histograms have no data until something is recorded.
    for name in HISTOGRAMS:
        with pytest.raises(KeyError):
            collected.metric(name)


def test_histogram_units_and_buckets():
    """Test that durations are recorded in seconds with second based buckets.

    Approval criteria:
        - Histograms shall have the documented names and unit 's'.
        - Execution duration buckets shall cover long running test suites.
    """
    collected = CollectedMetrics()
    collected.metrics.record_startup("success", 1.5)
    collected.metrics.record_execution({"conclusion": "SUCCESSFUL", "verdict": "PASSED"}, 3600)
    collected.metrics.record_result_publication("success", 0.2)
    collected.collect()
    for name, unit in HISTOGRAMS.items():
        assert collected.metric(name).unit == unit
    execution = collected.histogram(
        "etos.test_runner.execution.duration",
        **{"etos.conclusion": "successful", "etos.verdict": "passed"}
    )
    assert execution.count == 1
    assert execution.sum == 3600
    assert execution.explicit_bounds[-1] == 28800
    startup = collected.histogram(
        "etos.test_runner.startup.duration", **{"etos.outcome": "success"}
    )
    assert startup.sum == 1.5


def test_resource_carries_etos_version(monkeypatch):
    """Test that the ETOS version is a resource attribute and not a metric attribute.

    Approval criteria:
        - The resource shall have 'etos.version' from ETOS_VERSION.
        - The resource shall identify the Test Runner service.
        - No data point shall have an 'etos.version' attribute.
    """
    monkeypatch.setenv("ETOS_VERSION", "9.1.0")
    monkeypatch.setenv("HOSTNAME", "etr-pod")
    collected = CollectedMetrics()
    collected.metrics.record_startup("success", 1)
    collected.collect()
    attributes = collected.data.resource_metrics[0].resource.attributes
    assert attributes["etos.version"] == "9.1.0"
    assert attributes["service.name"] == "etos-test-runner"
    assert attributes["service.instance.id"] == "etr-pod"
    assert attributes["service.version"] == metrics_module.VERSION
    assert attributes["service.version"] != "9.1.0"
    assert collected.attribute_keys() <= ALLOWED_ATTRIBUTES


@pytest.mark.parametrize("value", [None, ""])
def test_etos_version_fallback(monkeypatch, value):
    """Test that the ETOS version falls back to 'unknown'.

    Approval criteria:
        - A missing or empty ETOS_VERSION shall result in etos.version 'unknown'.
    """
    if value is None:
        monkeypatch.delenv("ETOS_VERSION", raising=False)
    else:
        monkeypatch.setenv("ETOS_VERSION", value)
    collected = CollectedMetrics().collect()
    assert collected.data.resource_metrics[0].resource.attributes["etos.version"] == "unknown"


@pytest.mark.parametrize(
    "exception,outcome",
    [
        (TimeoutError("no sub suite"), "timeout"),
        (ConnectTimeout("connect timed out"), "timeout"),
        (ETRTerminated("ETR has been terminated."), "cancelled"),
        (KeyboardInterrupt(), "cancelled"),
        (ValueError("secret value in error"), "failure"),
    ],
)
def test_measure_startup_failure(exception, outcome):
    """Test that a failed startup is counted once with a bounded outcome.

    Approval criteria:
        - The exception shall be re-raised unchanged.
        - The startup shall be counted exactly once with the normalized outcome.
        - The error message shall not be part of the metric attributes.
    """
    collected = CollectedMetrics()
    with pytest.raises(type(exception)) as raised:
        with collected.metrics.measure_startup():
            raise exception
    assert raised.value is exception
    collected.collect()
    assert collected.nonzero("etos.test_runner.startups") == {(("etos.outcome", outcome),): 1}
    histogram = collected.histogram(
        "etos.test_runner.startup.duration", **{"etos.outcome": outcome}
    )
    assert histogram.count == 1
    assert histogram.sum >= 0
    assert collected.attribute_keys() <= ALLOWED_ATTRIBUTES


def test_measure_startup_success(monkeypatch):
    """Test that a successful startup is counted once with its duration.

    Approval criteria:
        - A successful startup shall be counted once as 'success' with its duration.
    """
    times = iter([100.0, 102.5])
    monkeypatch.setattr(metrics_module.time, "monotonic", lambda: next(times))
    collected = CollectedMetrics()
    with collected.metrics.measure_startup():
        pass
    collected.collect()
    assert collected.nonzero("etos.test_runner.startups") == {(("etos.outcome", "success"),): 1}
    histogram = collected.histogram(
        "etos.test_runner.startup.duration", **{"etos.outcome": "success"}
    )
    assert histogram.count == 1
    assert histogram.sum == 2.5


@pytest.mark.parametrize(
    "outcome,expected",
    [
        ({"conclusion": "SUCCESSFUL", "verdict": "FAILED"}, ("successful", "failed")),
        ({"conclusion": "TIMED_OUT", "verdict": "INCONCLUSIVE"}, ("timed_out", "inconclusive")),
        ({"conclusion": "custom rule value", "verdict": "maybe"}, ("other", "other")),
        ({}, ("other", "other")),
        (None, ("other", "other")),
    ],
)
def test_execution_attributes_are_bounded(outcome, expected):
    """Test that execution conclusions and verdicts are normalized to bounded values.

    Approval criteria:
        - Known conclusions and verdicts shall be lower cased.
        - Unknown values, e.g. from custom verdict rules, shall be reported as 'other'.
    """
    collected = CollectedMetrics()
    collected.metrics.record_execution(outcome, 1)
    collected.collect()
    conclusion, verdict = expected
    assert collected.nonzero("etos.test_runner.executions") == {
        (("etos.conclusion", conclusion), ("etos.verdict", verdict)): 1
    }


def test_from_environment_disabled(monkeypatch):
    """Test that metrics are no-op without an OpenTelemetry collector.

    Approval criteria:
        - Without OTEL_EXPORTER_OTLP_ENDPOINT no meter provider shall be created.
    """
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    metrics = ETRMetrics.from_environment()
    assert metrics.meter_provider is None
    with metrics.measure_startup():
        pass
    metrics.shutdown()


def test_from_environment_exports_on_shutdown(monkeypatch):
    """Test that metrics are exported over OTLP when the process shuts down.

    Approval criteria:
        - With OTEL_EXPORTER_OTLP_ENDPOINT, metrics shall be exported with the OTLP exporter.
        - Shutdown shall export the final values, including the etos.version resource.
        - Shutdown shall export the final values exactly once.
        - Shutdown shall be safe to call more than once.
    """
    exported = []

    def _export(_, metrics_data, *__, **___):
        """Capture exported metrics instead of sending them."""
        exported.append(metrics_data)
        return MetricExportResult.SUCCESS

    monkeypatch.setattr(OTLPMetricExporter, "export", _export)
    monkeypatch.setattr(OTLPMetricExporter, "shutdown", lambda *_, **__: None)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.setenv("ETOS_VERSION", "9.1.0")
    metrics = ETRMetrics.from_environment()
    assert metrics.meter_provider is not None
    metrics.record_startup("timeout", 60)
    metrics.shutdown()
    metrics.shutdown()
    assert len(exported) == 1
    resource_metrics = exported[0].resource_metrics[0]
    assert resource_metrics.resource.attributes["etos.version"] == "9.1.0"
    names = {
        metric.name: metric for scope in resource_metrics.scope_metrics for metric in scope.metrics
    }
    points = {
        tuple(point.attributes.items()): point.value
        for point in names["etos.test_runner.startups"].data.data_points
    }
    assert points[(("etos.outcome", "timeout"),)] == 1


class _FakeMetrics:  # pylint:disable=too-few-public-methods
    """Fake metrics recording whether they were shut down."""

    def __init__(self):
        """Initialize."""
        self.shutdowns = 0

    def shutdown(self):
        """Count shutdowns."""
        self.shutdowns += 1


@pytest.mark.parametrize(
    "result,exception",
    [
        (0, SystemExit),
        ({"verdict": "FAILED"}, SystemExit),
        (TimeoutError("sub suite"), TimeoutError),
        (ETRTerminated("ETR has been terminated."), ETRTerminated),
    ],
)
def test_main_flushes_metrics_on_every_exit_path(monkeypatch, result, exception):
    """Test that main shuts down metrics on success, failure, timeout and termination.

    Approval criteria:
        - Metrics shall be shut down exactly once before main exits.
        - The exit behavior of main shall be unchanged.
    """
    fake_metrics = _FakeMetrics()

    class _FakeETR:  # pylint:disable=too-few-public-methods
        """Fake ETR returning or raising the parametrized result."""

        def __init__(self, metrics):
            """Check that the metrics are passed on."""
            assert metrics is fake_metrics

        @staticmethod
        def run_etr():
            """Return or raise the result."""
            if isinstance(result, BaseException):
                raise result
            return result

    monkeypatch.setattr(etr_module.ETRMetrics, "from_environment", lambda: fake_metrics)
    monkeypatch.setattr(etr_module, "ETR", _FakeETR)
    with pytest.raises(exception) as raised:
        etr_module.main()
    if exception is SystemExit:
        assert raised.value.code == result
    assert fake_metrics.shutdowns == 1


def test_sigterm_raises_cancellation():
    """Test that SIGTERM raises an exception classified as a cancellation.

    Approval criteria:
        - The SIGTERM handler shall still raise an Exception with the same message.
        - The exception shall be classified as 'cancelled'.
    """
    with pytest.raises(Exception, match="ETR has been terminated.") as raised:
        etr_module.ETR.graceful_shutdown()
    assert metrics_module.outcome_from_exception(raised.value) == "cancelled"


def test_startup_spans_begin_until_end_and_is_counted_once(monkeypatch):
    """Test that the startup measurement starts at begin_startup and is recorded once.

    Approval criteria:
        - A later measure_startup block shall not restart an already begun measurement.
        - The startup shall be counted exactly once, even if ended again.
    """
    times = iter([10.0, 12.0, 15.0])
    monkeypatch.setattr(metrics_module.time, "monotonic", lambda: next(times))
    collected = CollectedMetrics()
    collected.metrics.begin_startup()
    with collected.metrics.measure_startup():
        collected.metrics.begin_startup()
    collected.metrics.end_startup(ValueError("late failure"))
    collected.collect()
    assert collected.nonzero("etos.test_runner.startups") == {(("etos.outcome", "success"),): 1}
    histogram = collected.histogram(
        "etos.test_runner.startup.duration", **{"etos.outcome": "success"}
    )
    assert histogram.count == 1
    assert histogram.sum == 2.0


@pytest.fixture(name="sigterm_guard")
def _sigterm_guard():
    """Fail instead of killing pytest if main does not install its SIGTERM handler."""

    def _not_replaced(*_):
        """Raise a distinguishable error instead of terminating the test process."""
        raise RuntimeError("SIGTERM handler was not installed")

    previous = signal.signal(signal.SIGTERM, _not_replaced)
    yield
    signal.signal(signal.SIGTERM, previous)


@pytest.mark.usefixtures("sigterm_guard")
def test_main_installs_sigterm_handler_before_initialization(monkeypatch):
    """Test that the SIGTERM handler is installed before metrics and ETOS are initialized.

    Approval criteria:
        - The ETR SIGTERM handler shall be active when the meter provider is created.
        - A SIGTERM during meter provider creation shall raise ETRTerminated.
    """

    def _terminated_while_creating_metrics():
        """Check the active handler, then receive SIGTERM."""
        assert signal.getsignal(signal.SIGTERM) == etr_module.graceful_shutdown
        signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(
        etr_module.ETRMetrics, "from_environment", _terminated_while_creating_metrics
    )
    with pytest.raises(ETRTerminated):
        etr_module.main()


@pytest.mark.usefixtures("sigterm_guard")
def test_main_exports_metrics_on_sigterm_during_runner_construction(monkeypatch):
    """Test that a SIGTERM while constructing the runner still shuts down metrics.

    Approval criteria:
        - main shall exit with ETRTerminated, as before.
        - Metrics shall be shut down exactly once, exporting the final values.
    """
    fake_metrics = _FakeMetrics()

    class _TerminatedETR:  # pylint:disable=too-few-public-methods
        """Fake ETR receiving SIGTERM while it is being constructed."""

        def __init__(self, _):
            """Receive SIGTERM."""
            signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(etr_module.ETRMetrics, "from_environment", lambda: fake_metrics)
    monkeypatch.setattr(etr_module, "ETR", _TerminatedETR)
    with pytest.raises(ETRTerminated):
        etr_module.main()
    assert fake_metrics.shutdowns == 1
