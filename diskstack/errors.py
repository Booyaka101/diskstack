"""Errors that reach the user as a message rather than a traceback."""


class DiskStackError(Exception):
    """Anything the user can act on: bad input, unknown format, missing file."""
