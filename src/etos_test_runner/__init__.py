# Copyright 2020 Axis Communications AB.
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
"""ETOS test runner module."""
import os
import logging
from importlib.metadata import version, PackageNotFoundError
from etos_lib.logging.logger import setup_logging
from etos_test_runner.lib.decrypt import decrypt

try:
    VERSION = version("etos_test_runner")
except PackageNotFoundError:
    VERSION = "Unknown"

if os.getenv("ETOS_ENCRYPTION_KEY"):
    os.environ["ETOS_RABBITMQ_PASSWORD"] = decrypt(
        os.environ["ETOS_RABBITMQ_PASSWORD"], os.getenv("ETOS_ENCRYPTION_KEY")
    )

DEV = os.getenv("DEV", "false").lower() == "true"
ENVIRONMENT = "development" if DEV else "production"
setup_logging("ETOS Test Runner", VERSION, ENVIRONMENT)

# JSONTas would print all passwords as they are decrypted,
# which is not safe, so we disable propagation on the loggers.
# Propagation needs to be set to 0 instead of disabling the
# logger or setting the loglevel higher because of how the
# etos library sets up logging.
logging.getLogger("JSONTas").propagate = 0
logging.getLogger("Dataset").propagate = 0
