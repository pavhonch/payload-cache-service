class CacheUnavailable(Exception):
    pass


class CacheIntegrityError(Exception):
    pass


class TransformerUnavailable(Exception):
    pass


class InvalidTransformation(Exception):
    pass


class PayloadTooLarge(Exception):
    pass


class PayloadNotFound(Exception):
    pass


class StorageError(Exception):
    pass


class PayloadIntegrityError(StorageError):
    pass
