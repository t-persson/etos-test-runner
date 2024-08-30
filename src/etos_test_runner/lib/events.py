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
"""ETOS internal message bus module."""
import os
from etos_lib import ETOS
from etos_lib.logging.log_publisher import RabbitMQLogPublisher


class EventPublisher:
    """EventPublisher helps in sending events to the internal ETOS message bus."""

    disabled = False

    def __init__(self, etos: ETOS):
        """Set up, but do not start, the RabbitMQ publisher."""
        if os.getenv("DISABLE_EVENT_PUBLISHING", "false").lower() == "true":
            self.disabled = True
        publisher = etos.config.get("event_publisher")
        if self.disabled is False and publisher is None:
            config = etos.config.etos_rabbitmq_publisher_data()
            # This password should already be decrypted when setting up the logging.
            config["password"] = etos.config.get("etos_rabbitmq_password")
            publisher = RabbitMQLogPublisher(**config, routing_key=None)
            etos.config.set("event_publisher", publisher)
        self.publisher = publisher
        self.identifier = etos.config.get("suite_id")

    def __del__(self):
        """Close the RabbitMQ publisher."""
        self.close()

    def close(self):
        """Close the RabbitMQ publisher if it is started."""
        if self.publisher is not None and self.publisher.is_alive():
            self.publisher.wait_for_unpublished_events()
            self.publisher.close()
            self.publisher.wait_close()

    def publish(self, event: dict):
        """Publish an event to the ETOS internal message bus."""
        if self.disabled:
            return
        if self.publisher is None:
            return
        if not self.publisher.running:
            self.publisher.start()
        routing_key = f"{self.identifier}.event.{event.get('event')}"
        self.publisher.send_event(event, routing_key=routing_key)
