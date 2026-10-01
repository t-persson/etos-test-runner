================
ETOS Test Runner
================

.. image:: https://img.shields.io/badge/Stage-Sandbox-yellow.svg
  :target: https://github.com/eiffel-community/community/blob/master/PROJECT_LIFECYCLE.md#stage-sandbox

ETOS (Eiffel Test Orchestration System) Test Runner


Description
===========

The ETOS Test Runner is responsible for executing test suites in ETOS.
ETR should, ideally, be executed within a test runner docker container.


Installation
============

   pip install etos_test_runner


Metrics
=======

When ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, which the ETOS Execution Space Provider does
for the Test Runners it starts, the Test Runner exports OpenTelemetry metrics over OTLP/gRPC.
``OTEL_EXPORTER_OTLP_INSECURE`` and the other standard ``OTEL_EXPORTER_OTLP_*`` variables are
respected. Without an endpoint no metrics are recorded or exported.

The metrics are exported periodically and once more when the process exits, on success as well
as after failures, timeouts and SIGTERM. The SIGTERM handler is installed before any other
initialization, so a termination at any point still reaches the final export. The final export
is bounded (10s) and never changes the exit status.

The metrics stability is alpha; names and attributes may change in a future release.

Resource attributes
-------------------

=================== =================================================================
Attribute           Value
=================== =================================================================
service.name        ``etos-test-runner``
service.version     Version of the ``etos_test_runner`` package.
service.instance.id ``HOSTNAME``, i.e. the Test Runner pod name, if set.
etos.version        ``ETOS_VERSION``, the ETOS release that launched the Test Runner.
                    ``unknown`` if not set.
=================== =================================================================

``etos.version`` is a resource attribute only, never a metric attribute.

Instruments
-----------

================================================ ========== ============= =============================
Name                                             Type       Unit          Attributes
================================================ ========== ============= =============================
etos.test_runner.startups                        Counter    {startup}     etos.outcome
etos.test_runner.startup.duration                Histogram  s             etos.outcome
etos.test_runner.executions                      Counter    {execution}   etos.conclusion, etos.verdict
etos.test_runner.execution.duration              Histogram  s             etos.conclusion, etos.verdict
etos.test_runner.result.publications             Counter    {publication} etos.outcome
etos.test_runner.result.publication.duration     Histogram  s             etos.outcome
================================================ ========== ============= =============================

**Startup** begins when the Test Runner starts constructing the runner, including the ETOS
library, and ends when it is ready to execute the test suite: the sub suite has been retrieved
and loaded, the Eiffel publisher configured, the plugins loaded and the IUT prepared. It is
counted once per Test Runner process. Retries within startup, such as HTTP retries of the sub
suite download or waiting for the environment event, are part of the same startup and its
duration. A restarted Test Runner pod is a new process and is counted again. ``etos.outcome`` is
one of:

- ``success``: the Test Runner is ready to execute.
- ``timeout``: a time bounded wait expired, e.g. no sub suite environment event was found or
  an HTTP request timed out.
- ``cancelled``: the Test Runner received SIGTERM.
- ``failure``: any other error, including an exhausted HTTP retry budget.

**Execution** begins when the test suite execution starts and ends when its outcome is known,
before the terminal ``EiffelTestSuiteFinishedEvent`` is sent. It includes the test payloads, IUT
monitoring and the log and artifact upload. It is counted once per test suite execution.
``etos.conclusion`` and ``etos.verdict`` are the lower cased Eiffel conclusion (``successful``,
``failed``, ``aborted``, ``timed_out``, ``inconclusive``) and verdict (``passed``, ``failed``,
``inconclusive``) sent in that event, or ``other`` for any other value, e.g. one produced by
custom verdict rules. A failing test payload is ``successful``/``failed``; a test suite that
could not execute is ``failed``/``inconclusive``.

**Result publication** begins when the terminal ``EiffelTestSuiteFinishedEvent`` (and, if the
test suite never started, the ``EiffelTestSuiteStartedEvent`` it requires) is sent and ends
when the Eiffel publisher has confirmed delivery of all outstanding events. It is counted once
per test suite execution. ``etos.outcome`` is one of:

- ``success``: all events were delivered.
- ``failure``: the terminal event could not be sent.
- ``timeout``: the events were not delivered in time.
- ``cancelled``: the Test Runner received SIGTERM while waiting for delivery.
- ``unconfirmed``: the terminal event was sent, but the execution failed with an error and the
  Test Runner exited without waiting for delivery.

Publication retries and redeliveries happen inside the ETOS library's Eiffel publisher and are
not counted separately; their time is part of the publication duration. Status events on the
internal ETOS message bus are not included.

Attributes never contain TestRun, suite, artifact or resource identifiers, test case names,
event contents or error messages.

To distinguish missing telemetry from zero, every Test Runner process that exports metrics
reports zero for each ``etos.outcome`` of the startup and result publication counters, and
for the standard execution results (``successful``/``passed``, ``successful``/``failed`` and
``failed``/``inconclusive``). A missing series therefore means that no metrics arrived.


Contribute
==========

- Issue Tracker: https://github.com/eiffel-community/etos/issues
- Source Code: https://github.com/eiffel-community/etos-test-runner


Support
=======

If you are having issues, please let us know.
There is a mailing list at: etos-maintainers@googlegroups.com or just write an Issue.
