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
"""Helpers for collecting ETOS Test Runner metrics in tests."""

from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from etos_test_runner.lib.metrics import ETRMetrics

# Attribute keys that metrics are allowed to use. Anything else, such as identifiers or
# error messages, is a test failure.
ALLOWED_ATTRIBUTES = {"etos.outcome", "etos.conclusion", "etos.verdict"}


class CollectedMetrics:
    """Metrics recorded into an in-memory reader."""

    def __init__(self):
        """Create metrics backed by an in-memory reader."""
        self.reader = InMemoryMetricReader()
        self.metrics = ETRMetrics.with_readers([self.reader])
        self.data = None

    def collect(self) -> "CollectedMetrics":
        """Collect the current metric data from the reader."""
        self.data = self.reader.get_metrics_data()
        return self

    def metric(self, name: str):
        """Get a collected metric by name.

        :param name: Name of the metric.
        :return: The collected metric.
        """
        for resource_metrics in self.data.resource_metrics:
            for scope_metrics in resource_metrics.scope_metrics:
                for metric in scope_metrics.metrics:
                    if metric.name == name:
                        return metric
        raise KeyError(name)

    def points(self, name: str) -> dict:
        """Get the data points of a metric keyed by their attributes.

        :param name: Name of the metric.
        :return: Data points keyed by a sorted tuple of attribute items.
        """
        return {
            tuple(sorted(point.attributes.items())): point
            for point in self.metric(name).data.data_points
        }

    def value(self, name: str, **attributes) -> float:
        """Get the value of a counter data point.

        :param name: Name of the counter.
        :param attributes: Attributes of the data point.
        :return: Value of the data point.
        """
        return self.points(name)[tuple(sorted(attributes.items()))].value

    def nonzero(self, name: str) -> dict:
        """Get the non-zero counter values of a metric.

        :param name: Name of the counter.
        :return: Values keyed by a sorted tuple of attribute items.
        """
        return {key: point.value for key, point in self.points(name).items() if point.value}

    def histogram(self, name: str, **attributes):
        """Get a histogram data point.

        :param name: Name of the histogram.
        :param attributes: Attributes of the data point.
        :return: The histogram data point.
        """
        return self.points(name)[tuple(sorted(attributes.items()))]

    def attribute_keys(self) -> set:
        """Get all attribute keys used by any data point."""
        keys = set()
        for resource_metrics in self.data.resource_metrics:
            for scope_metrics in resource_metrics.scope_metrics:
                for metric in scope_metrics.metrics:
                    for point in metric.data.data_points:
                        keys.update(point.attributes.keys())
        return keys
