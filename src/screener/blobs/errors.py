"""Errors common to all blob store backends."""


class BlobWriteFailed(RuntimeError):
    """A payload could not be written or read.

    Fatal for a run rather than for one security: `ingest_observation` asserts
    that `blob_path` exists, so a row must not be written when the object does
    not. A store that is failing is almost always failing systemically.
    """
