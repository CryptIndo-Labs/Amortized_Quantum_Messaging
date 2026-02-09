import time
from dataclasses import asdict
import redis
import errors
from AQM_Database import types
import config

class SecureVault:
    def __init__(self, client: redis.Redis):
        self.db: redis.Redis = client
        self.VALID_COIN_CATEGORIES = config.VALID_COIN_CATEGORIES
        self.VAULT_KEY_TTL_SECONDS = config.VAULT_KEY_TTL_SECONDS

    def store_key(
        self,
        key_id:         str,
        coin_category:  str,
        encrypted_blob: bytes,
        encryption_iv:  bytes,
        auth_tag:       bytes,
        coin_version:   str = "kyber768_v1"
    ) -> bool :
        if coin_category not in self.VALID_COIN_CATEGORIES:
            raise errors.InvalidCoinCategoryError(coin_category)

        if self.db.exists(key_id):
            raise errors.KeyAlreadyExistsError(key_id)

        vault_entry = types.VaultEntry(
            key_id=key_id,
            coin_category=coin_category,
            encrypted_blob=encrypted_blob,
            encryption_iv=encryption_iv,
            auth_tag=auth_tag,
            coin_version=coin_version,
            status = "ACTIVE",
            created_at=int(time.time())
        )

        try :
            with self.db.pipeline(transaction=True) as pipe:
                pipe.hset(key_id , mapping = asdict(vault_entry))
                pipe.expire(key_id , self.VAULT_KEY_TTL_SECONDS)
                pipe.hincrby("stats:vault", f"count:{coin_category.lower()}", 1)
                pipe.execute()
            return True
        except redis.exceptions.ConnectionError:
            raise errors.VaultUnavailableError(coin_category)


    def burn_key(self , key_id : str) -> bool:
        try:
            current_status = self.db.hget(key_id , "status")
            if current_status is None:
                raise errors.KeyNotFoundError(key_id)
            if current_status.decode('utf-8') == "BURNED":
                raise errors.KeyAlreadyBurnedError(key_id)

            with self.dp.pipeline(transaction=True) as pipe:
                pipe.hset(key_id , "status" , "BURNED")
                pipe.expire(key_id, config.VAULT_BURN_GRACE_SECONDS)
                pipe.hincrby("stats:vault", "active_keys", -1)
                pipe.hincrby("stats:vault", "total_burned", 1)

                pipe.execute()

            return True
        except redis.exceptions.ConnectionError:
            raise errors.VaultUnavailableError(key_id)