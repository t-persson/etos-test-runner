#!/usr/bin/env python
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
# -*- coding: utf-8 -*-
"""ETOS test runner module."""

import importlib
import json
import logging
import os
import pkgutil
import signal
import sys
from collections import OrderedDict
from importlib.metadata import version
from pprint import pprint
from typing import Optional, Union

from etos_lib import ETOS
from etos_lib.kubernetes.schemas.v1beta1.environment import (
    EnvironmentSpec as V1Beta1EnvironmentSpec,
)
from etos_lib.lib.http import Http
from etos_lib.logging.logger import FORMAT_CONFIG
from etos_lib.messaging.events import Status
from etos_lib.messaging.types import ServiceHealth, ServiceStatus
from etos_lib.schemas.v0.environment import Suite as V0Suite
from jsontas.jsontas import JsonTas
from pydantic import ValidationError
from urllib3.util import Retry
from yaml import safe_load

from etos_test_runner.lib.custom_dataset import CustomDataset
from etos_test_runner.lib.decrypt import Decrypt, decrypt
from etos_test_runner.lib.events import EventPublisher
from etos_test_runner.lib.iut import Iut
from etos_test_runner.lib.metrics import ETRMetrics, ETRTerminated
from etos_test_runner.lib.testrunner import TestRunner

VERSION = version("etos_test_runner")

# Remove spam from pika.
logging.getLogger("pika").setLevel(logging.WARNING)

_LOGGER = logging.getLogger(__name__)


def graceful_shutdown(*_) -> None:
    """Catch sigterm."""
    raise ETRTerminated("ETR has been terminated.")


class ETR:
    """ETOS Test Runner."""

    context = None

    def __init__(self, metrics: Optional[ETRMetrics] = None) -> None:
        """Initialize ETOS library and configure the internal message bus.

        The startup measurement starts here and ends in `run_etr` when the Test Runner is
        ready to execute, so that failures while constructing the runner are recorded too.

        :param metrics: Metrics to record the Test Runner lifecycle with. No-op if None.
        """
        signal.signal(signal.SIGTERM, self.graceful_shutdown)
        self.metrics = metrics if metrics is not None else ETRMetrics()
        self.metrics.begin_startup()
        try:
            self.etos = ETOS("ETOS Test Runner", os.getenv("HOSTNAME"), "ETOS Test Runner")
            # The internal message bus (ETOS_RABBITMQ_*) is used already before the sub suite
            # has been downloaded, e.g. for status events, so it is configured from the
            # environment here. The password is decrypted on import in the package __init__.
            self.etos.config.set("etos_rabbitmq_password", os.environ.get("ETOS_RABBITMQ_PASSWORD"))
            self.etos.config.set("suite_id", os.getenv("SUITE_ID"))
            # ETR will print the entire environment just before executing.
            # Hide the password.
            os.environ["ETOS_RABBITMQ_PASSWORD"] = "*********"

            self.environment_id = os.getenv("ENVIRONMENT_ID")
            self.environment_url = os.getenv("ENVIRONMENT_URL")
        except BaseException as exception:
            self.metrics.end_startup(exception)
            raise

    graceful_shutdown = staticmethod(graceful_shutdown)

    @staticmethod
    def apply_sub_suite_environment(config: dict) -> None:
        """Apply environment variables defined in the sub suite executor instructions.

        The execution space provider only passes the instructions the ETR needs before
        the sub suite is downloaded. The remaining instructions are read from the sub
        suite executor instructions here. Variables already set in the process
        environment take precedence over the ones defined in the sub suite.

        :param config: The downloaded sub suite configuration.
        """
        instructions = config.get("executor", {}).get("instructions", {})
        for key, value in instructions.get("environment", {}).items():
            if value is None:
                continue
            os.environ.setdefault(key, str(value))

    def configure_eiffel_publisher(self) -> None:
        """Configure and start the Eiffel publisher.

        The Eiffel RabbitMQ connection details are provided either directly as environment
        variables by the execution space provider, or applied from the downloaded sub suite
        by `apply_sub_suite_environment`. Since the Eiffel publisher is only used while
        executing tests, i.e. after the sub suite has been downloaded, its configuration is
        deferred until that point.
        """
        if os.getenv("ETOS_ENCRYPTION_KEY") and os.getenv("RABBITMQ_PASSWORD"):
            os.environ["RABBITMQ_PASSWORD"] = decrypt(
                os.environ["RABBITMQ_PASSWORD"], os.getenv("ETOS_ENCRYPTION_KEY")
            )
        self.etos.config.rabbitmq_publisher_from_environment()
        # ETR will print the entire environment just before executing.
        # Hide the password and the encryption key.
        os.environ["RABBITMQ_PASSWORD"] = "*********"
        if os.getenv("ETOS_ENCRYPTION_KEY"):
            os.environ["ETOS_ENCRYPTION_KEY"] = "*********"
        self.etos.start_publisher()

    def download_and_load(self, sub_suite_url: str) -> V1Beta1EnvironmentSpec:
        """Download and load test json.

        :param sub_suite_url: URL to where the sub suite information exists.
        """
        # We also wait for 500 here even though it should not be retryable because of how the ETOS
        # API tends to work.
        codes = [*Retry.RETRY_AFTER_STATUS_CODES, 404, 500]
        retry = Retry(
            total=None,
            read=10,
            connect=10,  # With 1 as backoff_factor, will retry for 1023s
            status=10,  # With 1 as backoff_factor, will retry for 1023s
            backoff_factor=1,
            other=0,
            status_forcelist=codes,  # 413, 429, 503, 404, 500
        )
        http_client = Http(retry=retry)

        response = http_client.get(sub_suite_url)
        try:
            json_config = response.json(object_pairs_hook=OrderedDict)
        except json.JSONDecodeError:
            testrun = safe_load(response.content)
            json_config = OrderedDict(testrun)
        dataset = CustomDataset()
        dataset.add("decrypt", Decrypt)
        config = JsonTas(dataset).run(json_config)

        # The execution space provider only needs to pass the instructions required before
        # the sub suite is downloaded. The remaining instructions are read from the sub
        # suite itself, letting any variables already set in the environment take precedence.
        self.apply_sub_suite_environment(config)
        try:
            v0_suite = V0Suite(**config)
            suite = V1Beta1EnvironmentSpec.convert_from(v0_suite)
        except ValidationError:
            suite = V1Beta1EnvironmentSpec(**config)

        # test_config kept for backward compatibility with plugins, but should not be used by
        # ETR itself.
        self.etos.config.set("test_config", config)

        self.etos.config.set("suite", suite)
        self.etos.config.set("context", suite.context)
        self.etos.config.set("artifact", suite.artifact)
        self.etos.config.set("main_suite_id", suite.mainSuiteID)
        self.etos.config.set("suite_id", suite.testrunID)
        return suite

    def load_plugins(self) -> None:
        """Load plugins from environment using the name etr_."""
        disable_plugins = os.getenv("DISABLE_PLUGINS")
        disabled_plugins = []
        if disable_plugins:
            disabled_plugins = disable_plugins.split(",")

        discovered_plugins = {
            name: importlib.import_module(name)
            for _, name, _ in pkgutil.iter_modules()
            if name.startswith("etr_") and name not in disabled_plugins
        }
        plugins = []
        for name, module in discovered_plugins.items():
            _LOGGER.info("Loading plugin: %r", name)
            if not hasattr(module, "ETRPlugin"):
                raise AttributeError(f"{name} does not have an ETRPlugin class!")
            plugins.append(module.ETRPlugin(self.etos))
        self.etos.config.set("plugins", plugins)

    def get_sub_suite_url(self, environment_id: str) -> Optional[str]:
        """Get sub suite from ETOS environment defined event.

        :param environment_id: ID of th environment defined event.
        :return: URL for sub suite.
        """
        query = """
        {
          environmentDefined(search: "{'meta.id': '%s'}") {
            edges {
              node {
                data {
                  uri
                }
              }
            }
          }
        }
        """ % environment_id
        # Timeout can be configured using ETOS_DEFAULT_WAIT_TIMEOUT environment variable
        # Default timeout is 60s.
        wait_generator = self.etos.utils.wait(self.etos.graphql.execute, query=query)
        for response in wait_generator:
            if response:
                try:
                    _, environment_defined = next(
                        self.etos.graphql.search_for_nodes(response, "environmentDefined")
                    )
                except StopIteration:
                    continue
                return environment_defined["data"]["uri"]
        return None

    def run_etr(self) -> Union[int, dict]:
        """Send activity events and run ETR.

        :return: Result of testrunner execution.
        """
        # Startup ends when the Test Runner is ready to execute the test suite.
        with self.metrics.measure_startup():
            event_publisher = EventPublisher(self.etos)
            _LOGGER.info("Publishing status event: ETOS Test Runner is starting.")
            event_publisher.publish_v2(
                Status(
                    data=ServiceStatus(
                        name="test-runner",
                        instance=self.environment_id,
                        version=VERSION,
                        status=ServiceHealth.OK,
                        message="ETOS Test Runner is starting.",
                    )
                ),
            )
            try:
                _LOGGER.info("Starting ETR.")
                sub_suite_url = self.environment_url
                if sub_suite_url is None:
                    sub_suite_url = self.get_sub_suite_url(self.environment_id)
                    if sub_suite_url is None:
                        raise TimeoutError(
                            "Could not get sub suite environment event with id "
                            f"{self.environment_id!r}"
                        )
                suite = self.download_and_load(sub_suite_url)
                self.configure_eiffel_publisher()
                FORMAT_CONFIG.identifier = self.etos.config.get("suite_id")
                self.load_plugins()
                iut = Iut(suite.iut)
                test_runner = TestRunner(iut, self.etos, metrics=self.metrics)
            except Exception as exception:  # pylint:disable=broad-except
                _LOGGER.exception("ETR failed to start.")
                _LOGGER.info("Publishing status event: ETOS Test Runner failed to start.")
                event_publisher.publish_v2(
                    Status(
                        data=ServiceStatus(
                            name="test-runner",
                            instance=self.environment_id,
                            version=VERSION,
                            status=ServiceHealth.ERROR,
                            message=f"ETOS Test Runner failed to download sub suite: {exception}",
                        )
                    ),
                )
                raise
        # test_runner.execute() will publish TestSuiteStarted and TestSuiteFinished events to manage
        # if there are any failures, no need for status events after this point.
        result = test_runner.execute()
        _LOGGER.info("ETR finished.")
        return result


def main() -> None:
    """Start ETR."""
    # Install the SIGTERM handler before any potentially blocking initialization so that
    # a termination always unwinds through the metrics shutdown below.
    signal.signal(signal.SIGTERM, graceful_shutdown)
    metrics = ETRMetrics()
    try:
        metrics = ETRMetrics.from_environment()
        etr = ETR(metrics)
        result = etr.run_etr()
        if isinstance(result, dict):
            pprint(result)
        _LOGGER.info("Done. Exiting")
        _LOGGER.info(result)
        sys.exit(result)
    finally:
        # Export the final metric values on every exit path, including failures,
        # timeouts and SIGTERM, since this process is short lived.
        metrics.shutdown()


def run():
    """Entry point to ETR."""
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    main()


if __name__ == "__main__":
    run()
