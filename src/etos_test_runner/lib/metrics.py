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
"""OpenTelemetry metrics for the ETOS Test Runner.

Metrics are exported over OTLP only when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set. Otherwise
every instrument is a no-op. See the README for the metric names and their semantics.
"""

import logging
import os
import time
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
from typing import Iterator, Optional

from opentelemetry.metrics import Meter, NoOpMeterProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_INSTANCE_ID, SERVICE_NAME, SERVICE_VERSION
from opentelemetry.sdk.resources import Resource
from requests.exceptions import Timeout as RequestsTimeout

try:
    VERSION = version("etos_test_runner")
except PackageNotFoundError:
    VERSION = "unknown"

_LOGGER = logging.getLogger(__name__)

METER_NAME = "etos_test_runner"
SERVICE = "etos-test-runner"
ETOS_VERSION_ATTRIBUTE = "etos.version"
ETOS_VERSION_ENVIRONMENT_VARIABLE = "ETOS_VERSION"
UNKNOWN = "unknown"

STARTUPS = "etos.test_runner.startups"
STARTUP_DURATION = "etos.test_runner.startup.duration"
EXECUTIONS = "etos.test_runner.executions"
EXECUTION_DURATION = "etos.test_runner.execution.duration"
RESULT_PUBLICATIONS = "etos.test_runner.result.publications"
RESULT_PUBLICATION_DURATION = "etos.test_runner.result.publication.duration"

OUTCOME = "etos.outcome"
CONCLUSION = "etos.conclusion"
VERDICT = "etos.verdict"

SUCCESS = "success"
FAILURE = "failure"
TIMEOUT = "timeout"
CANCELLED = "cancelled"
UNCONFIRMED = "unconfirmed"
OTHER = "other"

STARTUP_OUTCOMES = (SUCCESS, FAILURE, TIMEOUT, CANCELLED)
PUBLICATION_OUTCOMES = (SUCCESS, FAILURE, TIMEOUT, CANCELLED, UNCONFIRMED)
CONCLUSIONS = ("successful", "failed", "aborted", "timed_out", "inconclusive")
VERDICTS = ("passed", "failed", "inconclusive")
# Conclusion and verdict pairs produced by the Test Runner without custom verdict rules.
STANDARD_EXECUTION_RESULTS = (
    ("successful", "passed"),
    ("successful", "failed"),
    ("failed", "inconclusive"),
)

SHORT_DURATION_BUCKETS = (0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, 1200, 1800)
LONG_DURATION_BUCKETS = (1, 5, 10, 30, 60, 120, 300, 600, 1200, 1800, 3600, 7200, 14400, 28800)

SHUTDOWN_TIMEOUT_MILLIS = 10000


class ETRTerminated(Exception):
    """Raised when the ETOS Test Runner receives SIGTERM."""


def outcome_from_exception(exception: Optional[BaseException]) -> str:
    """Normalize an exception into a bounded outcome.

    :param exception: Exception that ended an operation, None if it succeeded.
    :return: One of 'success', 'timeout', 'cancelled' or 'failure'.
    """
    if exception is None:
        return SUCCESS
    if isinstance(exception, (TimeoutError, RequestsTimeout)):
        return TIMEOUT
    if isinstance(exception, (ETRTerminated, KeyboardInterrupt)):
        return CANCELLED
    return FAILURE


def _normalize(value: Optional[str], allowed: tuple[str, ...]) -> str:
    """Normalize a value to a lower case allowlisted value or 'other'.

    :param value: Value to normalize.
    :param allowed: Allowed lower case values.
    :return: The normalized value.
    """
    if not isinstance(value, str):
        return OTHER
    normalized = value.lower()
    return normalized if normalized in allowed else OTHER


def etos_version() -> str:
    """Get the ETOS version provided by the ETOS controller.

    :return: Value of ETOS_VERSION, 'unknown' if not set.
    """
    return os.getenv(ETOS_VERSION_ENVIRONMENT_VARIABLE) or UNKNOWN


def resource() -> Resource:
    """Create the OpenTelemetry resource describing this Test Runner process.

    :return: A resource including the ETOS version as an `etos.version` attribute.
    """
    attributes = {
        SERVICE_NAME: SERVICE,
        SERVICE_VERSION: VERSION,
        ETOS_VERSION_ATTRIBUTE: etos_version(),
    }
    if os.getenv("HOSTNAME"):
        attributes[SERVICE_INSTANCE_ID] = os.getenv("HOSTNAME")
    return Resource.create(attributes)


class ETRMetrics:  # pylint:disable=too-many-instance-attributes
    """Metric instruments for the ETOS Test Runner lifecycle."""

    def __init__(self, meter_provider: Optional[MeterProvider] = None) -> None:
        """Create the instruments.

        :param meter_provider: Meter provider to record metrics with. No-op if None.
        """
        self.meter_provider = meter_provider
        self._startup_started: Optional[float] = None
        self._startup_recorded = False
        provider = meter_provider if meter_provider is not None else NoOpMeterProvider()
        meter: Meter = provider.get_meter(METER_NAME, VERSION)
        self.startups = meter.create_counter(
            STARTUPS,
            unit="{startup}",
            description="Test Runner startups by outcome, counted once per process.",
        )
        self.startup_duration = meter.create_histogram(
            STARTUP_DURATION,
            unit="s",
            description="Time from Test Runner start until it is ready to execute tests.",
            explicit_bucket_boundaries_advisory=SHORT_DURATION_BUCKETS,
        )
        self.executions = meter.create_counter(
            EXECUTIONS,
            unit="{execution}",
            description="Test suite executions by normalized conclusion and verdict.",
        )
        self.execution_duration = meter.create_histogram(
            EXECUTION_DURATION,
            unit="s",
            description="Time from test suite execution start until its outcome is known.",
            explicit_bucket_boundaries_advisory=LONG_DURATION_BUCKETS,
        )
        self.result_publications = meter.create_counter(
            RESULT_PUBLICATIONS,
            unit="{publication}",
            description="Terminal test suite result publications by delivery outcome.",
        )
        self.result_publication_duration = meter.create_histogram(
            RESULT_PUBLICATION_DURATION,
            unit="s",
            description="Time from terminal result publication until delivery is confirmed.",
            explicit_bucket_boundaries_advisory=SHORT_DURATION_BUCKETS,
        )
        self._initialize_series()

    @classmethod
    def from_environment(cls) -> "ETRMetrics":
        """Create metrics exporting over OTLP if OTEL_EXPORTER_OTLP_ENDPOINT is set.

        :return: Metrics backed by an OTLP meter provider, or no-op metrics.
        """
        if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
            _LOGGER.info("OpenTelemetry metrics not enabled. OTEL_EXPORTER_OTLP_ENDPOINT not set.")
            return cls()
        # Imported here so that the gRPC runtime is only loaded when metrics are exported. Test
        # Runners without an OTLP endpoint, such as those started by ETOS v0, never need it.
        # pylint:disable=import-outside-toplevel
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

        reader = PeriodicExportingMetricReader(OTLPMetricExporter())
        return cls.with_readers([reader])

    @classmethod
    def with_readers(cls, readers: list[MetricReader]) -> "ETRMetrics":
        """Create metrics backed by a meter provider using the given readers.

        :param readers: Metric readers to collect metrics with.
        :return: Metrics backed by a new meter provider.
        """
        provider = MeterProvider(
            metric_readers=readers, resource=resource(), shutdown_on_exit=False
        )
        return cls(provider)

    def _initialize_series(self) -> None:
        """Report zero for known series so that missing telemetry is distinguishable from zero."""
        for outcome in STARTUP_OUTCOMES:
            self.startups.add(0, {OUTCOME: outcome})
        for conclusion, verdict in STANDARD_EXECUTION_RESULTS:
            self.executions.add(0, {CONCLUSION: conclusion, VERDICT: verdict})
        for outcome in PUBLICATION_OUTCOMES:
            self.result_publications.add(0, {OUTCOME: outcome})

    def record_startup(self, outcome: str, duration: float) -> None:
        """Record a Test Runner startup outcome.

        :param outcome: Normalized startup outcome.
        :param duration: Startup duration in seconds.
        """
        attributes = {OUTCOME: outcome}
        self.startups.add(1, attributes)
        self.startup_duration.record(duration, attributes)

    def begin_startup(self) -> None:
        """Mark the start of the Test Runner startup, unless it has already started."""
        if self._startup_started is None:
            self._startup_started = time.monotonic()

    def end_startup(self, exception: Optional[BaseException] = None) -> None:
        """Record the startup outcome and duration, once per process.

        :param exception: Exception that ended the startup, None if the Test Runner is ready.
        """
        if self._startup_recorded:
            return
        self.begin_startup()
        self._startup_recorded = True
        self.record_startup(
            outcome_from_exception(exception), time.monotonic() - self._startup_started
        )

    @contextmanager
    def measure_startup(self) -> Iterator[None]:
        """Measure the Test Runner startup until the block exits.

        The measurement starts at `begin_startup` if it was called earlier, otherwise when
        entering the block. The outcome is recorded once; exceptions are re-raised unchanged.
        """
        self.begin_startup()
        try:
            yield
        except BaseException as exception:
            self.end_startup(exception)
            raise
        self.end_startup()

    def record_execution(self, outcome: Optional[dict], duration: float) -> None:
        """Record a finished test suite execution.

        :param outcome: Test suite outcome with 'conclusion' and 'verdict' keys.
        :param duration: Execution duration in seconds.
        """
        outcome = outcome or {}
        attributes = {
            CONCLUSION: _normalize(outcome.get("conclusion"), CONCLUSIONS),
            VERDICT: _normalize(outcome.get("verdict"), VERDICTS),
        }
        self.executions.add(1, attributes)
        self.execution_duration.record(duration, attributes)

    def record_result_publication(self, outcome: str, duration: float) -> None:
        """Record the outcome of the terminal test suite result publication.

        :param outcome: Normalized publication outcome.
        :param duration: Publication duration in seconds.
        """
        attributes = {OUTCOME: outcome}
        self.result_publications.add(1, attributes)
        self.result_publication_duration.record(duration, attributes)

    def shutdown(self) -> None:
        """Shut down the meter provider, exporting the final values, never raising.

        Called before the process exits so that the final values of this short lived
        process are exported, regardless of how the Test Runner ended. The periodic
        reader collects and exports once more when it is shut down, so no separate
        flush is done; a flush would export the same cumulative values twice.
        """
        if self.meter_provider is None:
            return
        try:
            self.meter_provider.shutdown(timeout_millis=SHUTDOWN_TIMEOUT_MILLIS)
        except Exception:  # pylint:disable=broad-except
            _LOGGER.warning("Failed to shut down OpenTelemetry metrics.", exc_info=True)
        self.meter_provider = None
