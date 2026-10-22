"""Mount the containing directory at /plugins; plugin='collector_plugin:Example'."""
from hub_collector_plugins import Batch, Observation


class Example:
    def __init__(self, options):
        self.options = options

    def collect(self, *, cursor, now):
        # Use the service's read-only SDK/API here. A cursor may be a JSON value.
        # Commit it only with the observations from that same batch.
        revision = self.options.get('revision', 1)
        if cursor == revision:
            return Batch([], revision)
        return Batch([Observation({'revision': revision}, key='example', kind='example.changed')], revision)
