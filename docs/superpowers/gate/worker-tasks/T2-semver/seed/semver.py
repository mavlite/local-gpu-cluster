"""Semantic version parsing, comparison and bumping. See TASK.md for the full contract."""


def parse(text):
    raise NotImplementedError


def compare(a, b):
    raise NotImplementedError


def bump(text, part):
    raise NotImplementedError
