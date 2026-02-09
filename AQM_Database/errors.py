class AQMDatabaseError(Exception):
    pass

class VaultUnavailableError(AQMDatabaseError):
    def __init__(self , port):
        self.port = port
        message = f"Cannot connect to Vault at port {port}"
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