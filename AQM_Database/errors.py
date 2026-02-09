class AQMDatabaseError(Exception):
    super.__init__()

class VaultUnavailableError(AQMDatabaseError):
    def __init__(self , message):
        message = f"Vault_error  = {message}"
        super().__init__(message)

class KeyAlreadyExistsError(AQMDatabaseError):
    def __init__(self , key):
        self.key = key
        message = f"Key {key} already exists"
        super().__init__(message)

class InvalidCoinCategoryError(AQMDatabaseError):
    def __init__(self , category):
        self.category = category
        message = f"Invalid Coin Category {category}"
        super().__init__(message)

class KeyNotFoundError(AQMDatabaseError):
    def __init__(self , key):
        self.key = key
        message = f"Key {key} not found"
        super().__init__(message)

class KeyAlreadyBurnedError(AQMDatabaseError):
    def __init__(self , key):
        self.key = key
        message = f"Key {key} already burned"
        super().__init__(message)