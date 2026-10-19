"""PEP8-ish sample: 4 spaces, double quotes, two blank rows between defs."""


class Registry:
    """Holds named handlers."""

    DEFAULT_LIMIT = 32

    def __init__(self):
        self.handlers = {}
        self.created_at = 0

    def register(self, name, handler):
        self.handlers[name] = handler
        return handler

    def lookup(self, name):
        if name in self.handlers:
            return self.handlers[name]
        return None


def build_registry(items):
    registry = Registry()
    for key, value in items:
        registry.register(key, value)
    return registry


def summarise(registry):
    total = len(registry.handlers)
    return f'{registry.__class__.__name__}({total})'
