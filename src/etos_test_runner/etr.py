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
import argparse
import sys
import logging
import os
import signal
import importlib
import pkgutil
from pprint import pprint
from collections import OrderedDict

from etos_lib import ETOS
from etos_lib.logging.logger import FORMAT_CONFIG
from jsontas.jsontas import JsonTas

from etos_test_runner import VERSION
from etos_test_runner.lib.testrunner import TestRunner
from etos_test_runner.lib.iut import Iut
from etos_test_runner.lib.custom_dataset import CustomDataset
from etos_test_runner.lib.decrypt import Decrypt, decrypt


# Remove spam from pika.
logging.getLogger("pika").setLevel(logging.WARNING)

_LOGGER = logging.getLogger(__name__)


def parse_args(args):
    """Parse command line parameters.

    :param args: command line parameters as list of strings
    :return: command line parameters as :obj:`airgparse.Namespace`
    """
    parser = argparse.ArgumentParser(description="ETOS test runner")
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"etos_test_runner {VERSION}",
    )
    return parser.parse_args(args)


class ETR:
    """ETOS Test Runner."""

    context = None

    def __init__(self):
        """Initialize ETOS library and start eiffel publisher."""
        self.etos = ETOS("ETOS Test Runner", os.getenv("HOSTNAME"), "ETOS Test Runner")
        if os.getenv("ETOS_ENCRYPTION_KEY"):
            os.environ["RABBITMQ_PASSWORD"] = decrypt(
                os.environ["RABBITMQ_PASSWORD"], os.getenv("ETOS_ENCRYPTION_KEY")
            )

        self.etos.config.rabbitmq_publisher_from_environment()
        self.etos.config.set("etos_rabbitmq_password", os.environ.get("ETOS_RABBITMQ_PASSWORD"))
        # ETR will print the entire environment just before executing.
        # Hide the password.
        os.environ["RABBITMQ_PASSWORD"] = "*********"
        os.environ["ETOS_RABBITMQ_PASSWORD"] = "*********"

        self.etos.start_publisher()
        self.environment_id = os.getenv("ENVIRONMENT_ID")

        signal.signal(signal.SIGTERM, self.graceful_shutdown)

    @staticmethod
    def graceful_shutdown(*args):
        """Catch sigterm."""
        raise Exception("ETR has been terminated.")  # pylint:disable=broad-exception-raised

    def download_and_load(self, sub_suite_url):
        """Download and load test json.

        :param sub_suite_url: URL to where the sub suite information exists.
        :type sub_suite_url: str
        """
        response = self.etos.http.get(sub_suite_url)
        json_config = response.json(object_pairs_hook=OrderedDict)
        dataset = CustomDataset()
        dataset.add("decrypt", Decrypt)
        config = JsonTas(dataset).run(json_config)

        # ETR will print the entire environment just before executing.
        # Hide the encryption key.
        if os.getenv("ETOS_ENCRYPTION_KEY"):
            os.environ["ETOS_ENCRYPTION_KEY"] = "*********"

        self.etos.config.set("test_config", config)
        self.etos.config.set("context", config.get("context"))
        self.etos.config.set("artifact", config.get("artifact"))
        self.etos.config.set("main_suite_id", config.get("test_suite_started_id"))
        self.etos.config.set("suite_id", config.get("suite_id"))

    def _run_tests(self):
        """Run tests in ETOS test runner.

        :return: Results of test runner execution.
        :rtype: bool
        """
        iut = Iut(self.etos.config.get("test_config").get("iut"))
        test_runner = TestRunner(iut, self.etos)
        return test_runner.execute()

    def load_plugins(self):
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

    def get_sub_suite_url(self, environment_id):
        """Get sub suite from ETOS environment defined event.

        :param environment_id: ID of th environment defined event.
        :type environment_id: str
        :return: URL for sub suite.
        :rtype: str
        """
        query = (
            """
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
        """
            % environment_id
        )
        wait_generator = self.etos.utils.wait(self.etos.graphql.execute, query=query)
        for response in wait_generator:
            if response:
                try:
                    _, environment_defined = next(
                        self.etos.graphql.search_for_nodes(response, "environmentDefined")
                    )
                except StopIteration:
                    return None
                return environment_defined["data"]["uri"]
        return None

    def run_etr(self):
        """Send activity events and run ETR.

        :return: Result of testrunner execution.
        :rtype: bool
        """
        _LOGGER.info("Starting ETR.")
        sub_suite_url = self.get_sub_suite_url(self.environment_id)
        if sub_suite_url is None:
            raise TimeoutError(
                f"Could not get sub suite environment event with id {self.environment_id!r}"
            )
        self.download_and_load(sub_suite_url)
        FORMAT_CONFIG.identifier = self.etos.config.get("suite_id")
        self.load_plugins()
        try:
            activity_name = self.etos.config.get("test_config").get("name")
            triggered = self.etos.events.send_activity_triggered(activity_name)
            self.etos.events.send_activity_started(triggered)
            result = self._run_tests()
        except Exception as exc:  # pylint:disable=broad-except
            self.etos.events.send_activity_finished(
                triggered, {"conclusion": "FAILED", "description": str(exc)}
            )
            raise
        self.etos.events.send_activity_finished(triggered, {"conclusion": "SUCCESSFUL"})
        _LOGGER.info("ETR finished.")
        return result


def main(args):
    """Start ETR."""
    args = parse_args(args)

    etr = ETR()
    result = etr.run_etr()
    if isinstance(result, dict):
        pprint(result)
    _LOGGER.info("Done. Exiting")
    _LOGGER.info(result)
    sys.exit(result)


def run():
    """Entry point to ETR."""
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    main(sys.argv[1:])


if __name__ == "__main__":
    run()
