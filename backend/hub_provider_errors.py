"""Adapter-owned messages; never surface arbitrary remote errors or credentials."""


class ProviderFailure(RuntimeError):
    def __init__(self, safe_message):
        super().__init__(safe_message)
        self.safe_message = safe_message


class ProviderOutcomeUnknown(ProviderFailure):
    """A remote action may have started. It must not be replayed automatically."""


class ProviderNeedsUser(ProviderFailure):
    """The external runtime is awaiting human input, without a success claim."""
