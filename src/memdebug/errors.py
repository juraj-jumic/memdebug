"""Errors this package raises on purpose. The CLI shows these without a traceback."""


class MemdebugError(Exception):
    """Base class."""


class LedgerError(MemdebugError):
    """The ledger file is unusable (wrong format, unreadable entry, unsafe path)."""


class LedgerConflictError(LedgerError):
    """A write collided with another writer. Nothing was written; the caller may retry."""


class SnapshotError(LedgerError):
    """A snapshot could not be saved, loaded or compared."""


class AdapterError(MemdebugError):
    """A backend could not be read, or returned something unexpected."""


class UnsupportedSchemaError(AdapterError):
    """The backend's storage does not look like a format this adapter understands."""


class RestoreError(MemdebugError):
    """A rollback could not be planned or carried out. The message says whether anything was changed."""
