"""INI parser with typed getters. See TASK.md for the full contract."""


def parse(text):
    raise NotImplementedError


def get_int(cfg, section, key, default=None):
    raise NotImplementedError


def get_bool(cfg, section, key, default=None):
    raise NotImplementedError
