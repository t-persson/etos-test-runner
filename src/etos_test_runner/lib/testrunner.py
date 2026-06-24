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
"""ETR test runner module."""

import json
import logging
import os
import time
from datetime import timedelta
from pprint import pprint
from typing import Union

from eiffellib.events import EiffelTestSuiteStartedEvent
from etos_lib.kubernetes.schemas.v1beta1.environment import EnvironmentSpec

from etos_test_runner.lib.executor import Executor
from etos_test_runner.lib.iut_monitoring import IutMonitoring
from etos_test_runner.lib.log_area import LogArea
from etos_test_runner.lib.verdict import CustomVerdictMatcher
from etos_test_runner.lib.workspace import Workspace


class TestRunner:
    """Test runner for ETOS."""

    # pylint: disable=too-many-instance-attributes

    def __init__(self, iut, etos):
        """Initialize.

        :param iut: IUT to execute tests on.
        :type iut: :obj:`etr.lib.iut.Iut`
        :param etos: ETOS library
        :type etos: :obj:`etos_lib.etos.ETOS`
        """
        self.etos = etos
        self.iut = iut
        self.suite = self.etos.config.get("suite")
        assert isinstance(self.suite, EnvironmentSpec), (
            "Suite config should be of type EnvironmentSpec"
        )
        self.logger = logging.getLogger(f"ETR - {self.suite.name}")

        self.log_area = LogArea(self.etos)
        self.iut_monitoring = IutMonitoring(self.iut, self.etos)
        self.issuer = {"name": "ETOS Test Runner"}
        self.etos.config.set("iut", self.iut)
        self.plugins = self.etos.config.get("plugins")
        self._result = True
        self._outcome = None

        verdict_rule_file = os.getenv("VERDICT_RULES_FILE")
        if verdict_rule_file is not None:
            with open(verdict_rule_file, "r", encoding="utf-8") as inp:
                rules = json.load(inp)
        else:
            rules = []

        self.verdict_matcher = CustomVerdictMatcher(rules)

    def test_suite_started(self):
        """Publish a test suite started event.

        :return: Reference to test suite started.
        :rtype: :obj:`eiffel.events.base_event.BaseEvent`
        """
        suite_name = self.suite.name
        categories = ["Regression test_suite", "Sub suite"]
        categories.append(self.iut.identity.name)
        livelogs = self.suite.logArea.get("livelogs")

        test_suite_started = EiffelTestSuiteStartedEvent()
        data = {
            "name": suite_name,
            "categories": categories,
            "types": ["FUNCTIONAL"],
            "liveLogs": [{"name": "console", "uri": livelogs}],
        }
        # TODO: Remove CONTEXT link here.
        links = {
            "CONTEXT": self.etos.config.get("context"),
            "CAUSE": self.etos.config.get("main_suite_id"),
        }
        test_suite_started.meta.event_id = self.suite.id
        return self.etos.events.send(test_suite_started, links, data)

    def environment(self, context):
        """Send out which environment we're executing within.

        :param context: Context where this environment is used.
        :type context: str
        """
        # TODO: Get this from prepare
        if os.getenv("HOSTNAME") is not None:
            self.etos.events.send_environment_defined(
                "ETR Hostname",
                links={"CONTEXT": context},
                host={"name": os.getenv("HOSTNAME"), "user": "etos"},
            )
        if os.getenv("EXECUTION_SPACE_URL") is not None:
            self.etos.events.send_environment_defined(
                "Execution Space URL",
                links={"CONTEXT": context},
                host={"name": os.getenv("EXECUTION_SPACE_URL"), "user": "etos"},
            )

    def run_tests(self, workspace: Workspace) -> tuple[bool, list[Union[int, None]]]:
        """Execute test recipes within a test executor.

        :param workspace: Which workspace to execute test suite within.
        :type workspace: :obj:`etr.lib.workspace.Workspace`
        :return: Result of test execution.
        :rtype: bool
        """
        result = True
        test_framework_exit_codes = []
        for num, test in enumerate(self.suite.testExecutions):
            self.logger.info("Executing test %s/%s", num + 1, len(self.suite.testExecutions))
            start = time.time()
            with Executor(test, self.iut, self.etos) as executor:
                self.logger.info("Starting test '%s'", executor.test_name, extra={"user_log": True})
                executor.execute(workspace)
                end = time.time()
                if not executor.result:
                    result = executor.result
                if executor.result is False or executor.returncode != 0:
                    self.logger.warning(
                        "Test '%s' failed. Result: %s. Test framework exit code: %d (%s)",
                        executor.test_name,
                        executor.result,
                        executor.returncode,
                        timedelta(seconds=end - start),
                        extra={"user_log": True},
                    )
                else:
                    self.logger.info(
                        "Test '%s' finished. Result: %s. Test framework exit code: %d (%s)",
                        executor.test_name,
                        executor.result,
                        executor.returncode,
                        timedelta(seconds=end - start),
                        extra={"user_log": True},
                    )
                test_framework_exit_codes.append(executor.returncode)
        return result, test_framework_exit_codes

    def outcome(
        self,
        result: bool,
        executed: bool,
        description: str,
        test_framework_exit_codes: list[Union[int, None]],
    ) -> dict:
        """Get outcome from test execution.

        :param result: Result of execution.
        :type result: bool
        :param executed: Whether or not tests have successfully executed.
        :type executed: bool
        :param description: Optional description.
        :type description: str
        :return: Outcome of test execution.
        :rtype: dict
        """
        test_framework_output = {
            "test_framework_exit_codes": test_framework_exit_codes,
        }
        custom_verdict = self.verdict_matcher.evaluate(test_framework_output)
        if custom_verdict is not None:
            conclusion = custom_verdict["conclusion"]
            verdict = custom_verdict["verdict"]
            description = custom_verdict["description"]
            self.logger.info("Verdict matches testrunner verdict rule: %s", custom_verdict)
        elif executed:
            conclusion = "SUCCESSFUL"
            verdict = "PASSED" if result else "FAILED"
            self.logger.info(
                "Tests executed successfully. Verdict set to '%s' due to result being '%s'",
                verdict,
                result,
            )
        else:
            conclusion = "FAILED"
            verdict = "INCONCLUSIVE"
            self.logger.info(
                "Tests did not execute successfully. Setting verdict to '%s'",
                verdict,
            )

        suite_name = self.suite.name
        if not description and not result:
            self.logger.info("No description but result is a failure. At least some tests failed.")
            description = f"At least some {suite_name} tests failed."
        elif not description and result:
            self.logger.info(
                "No description and result is a success. All tests executed successfully."
            )
            description = f"All {suite_name} tests completed successfully."
        else:
            self.logger.info("Description was set. Probably due to an exception.")
        return {
            "verdict": verdict,
            "description": description,
            "conclusion": conclusion,
        }

    def _test_suite_triggered(self, name):
        """Call on_test_suite_triggered for all ETR plugins.

        :param name: Name of test suite that triggered.
        :type name: str
        """
        for plugin in self.plugins:
            plugin.on_test_suite_triggered(name)

    def _test_suite_started(self, test_suite_started):
        """Call on_test_suite_started for all ETR plugins.

        :param test_suite_started: The test suite started event
        :type test_suite_started: :obj:`eiffellib.events.EiffelTestSuiteStartedEvent`
        """
        for plugin in self.plugins:
            plugin.on_test_suite_started(test_suite_started)

    def _test_suite_finished(self, name, outcome):
        """Call on_test_suite_finished for all ETR plugins.

        :param name: Name of test suite that finished.
        :type name: str
        :param outcome: Outcome of test suite execution.
        :type outcome: dict
        """
        for plugin in self.plugins:
            plugin.on_test_suite_finished(name, outcome)

    def _run_and_finalize(self, workspace: Workspace) -> None:
        """Run tests and finalize outcome before artifact upload.

        Must be called inside a Workspace context so that plugin handlers
        can create artifacts that get uploaded by Workspace.__exit__.

        Stores result and outcome on ``self`` so that they are available
        to the caller even when an exception propagates.

        :param workspace: Active workspace context.
        :type workspace: :obj:`etr.lib.workspace.Workspace`
        """
        self._result = True
        description = None
        executed = False
        test_framework_exit_codes = []
        try:
            self.logger.info("Start IUT monitoring.")
            self.iut_monitoring.start_monitoring()
            self.logger.info("Starting test executor.")
            self._result, test_framework_exit_codes = self.run_tests(workspace)
            executed = True
            self.logger.info("Stop IUT monitoring.")
            self.iut_monitoring.stop_monitoring()
        except Exception as exception:  # pylint:disable=broad-except
            self._result = False
            executed = False
            description = str(exception)
            raise
        finally:
            if self.iut_monitoring.monitoring:
                self.logger.info("Stop IUT monitoring.")
                self.iut_monitoring.stop_monitoring()
            self.logger.info("Figure out test outcome.")
            self._outcome = self.outcome(
                self._result, executed, description, test_framework_exit_codes
            )
            pprint(self._outcome)
            self.logger.debug("Call on_test_suite_finished plugin handlers.")
            self._test_suite_finished(self.suite.name, self._outcome)

    def wait_for_events(self):
        """Wait for the Eiffel publisher to deliver all events."""
        timeout = time.time() + 600  # 10 minutes
        self.logger.info("Waiting for eiffel publisher to deliver events (600s).")
        previous = 0
        # pylint:disable=protected-access
        current = len(self.etos.publisher._deliveries)
        while current:
            current = len(self.etos.publisher._deliveries)
            self.logger.info("Remaining events to send        : %d", current)
            self.logger.info("Events sent since last iteration: %d", previous - current)
            if time.time() > timeout:
                if current < previous:
                    self.logger.info(
                        "Timeout reached, but events are still being sent. Increase timeout by 10s."
                    )
                    timeout = time.time() + 10
                else:
                    raise TimeoutError("Eiffel publisher did not deliver all eiffel events.")
            previous = current
            time.sleep(1)

    def execute(self):
        """Execute all tests in test suite.

        :return: Result of execution. Linux exit code.
        :rtype: int
        """
        test_suite_started = None
        try:
            self._test_suite_triggered(self.suite.name)
            self.logger.info("Send test suite started event.")
            test_suite_started = self.test_suite_started()
            self._test_suite_started(test_suite_started)
            sub_suite_id = test_suite_started.meta.event_id

            self.logger.info("Send test environment events.")
            self.environment(sub_suite_id)
            self.etos.config.set("sub_suite_id", sub_suite_id)

            self._result = True
            self._outcome = None

            with Workspace(self.log_area) as workspace:
                self._run_and_finalize(workspace)
        finally:
            if test_suite_started is None:
                self.logger.warning(
                    "Test suite did not start successfully. "
                    "Sending test suite started event in order to finish properly.",
                )
                test_suite_started = self.test_suite_started()
                self._outcome = {
                    "verdict": "INCONCLUSIVE",
                    "conclusion": "FAILED",
                    "description": "Test suite did not start successfully.",
                }
            self.logger.info(
                "Log area upload finished. Total uploaded: %d (%d log(s), %d artifact(s))",
                len(self.log_area.logs) + len(self.log_area.artifacts),
                len(self.log_area.logs),
                len(self.log_area.artifacts),
                extra={"user_log": True},
            )
            if self._outcome is None:
                self.logger.info("Figure out test outcome.")
                self._outcome = self.outcome(self._result, False, None, [])
                pprint(self._outcome)
            self.logger.info("Send test suite finished event.")
            test_suite_finished = self.etos.events.send_test_suite_finished(
                test_suite_started,
                links={"CONTEXT": self.etos.config.get("context")},
                outcome=self._outcome,
                persistentLogs=self.log_area.persistent_logs,
            )
            self.logger.info(test_suite_finished.pretty)

        self.wait_for_events()
        self.logger.info("Tests finished executing.")
        return 0 if self._result else self._outcome
