"""Tiny URL path router. See TASK.md for the full contract."""


class NotFound(Exception):
    pass


class MethodNotAllowed(Exception):
    def __init__(self, allowed):
        super().__init__(f"method not allowed; allowed: {allowed}")
        self.allowed = allowed


class Router:
    def add(self, method, pattern, handler):
        raise NotImplementedError

    def match(self, method, path):
        raise NotImplementedError
