import redis
import config
import errors
from types import HealthStatus


def create_vaul_client() -> redis.Redis:
    r = redis.Redis(host=config.REDIS_HOST, port=config.REDIS_PORT, db=config.REDIS_VAULT_DB , decode_responses=True , socket_connect_timeout=config.REDIS_SOCKET_TIMEOUT , socket_timeout=config.REDIS_SOCKET_TIMEOUT)

    if r.ping():
        print("Connected to Vault.")
    else:
        print("Failed to connect to Vault.")
        raise errors.VaultUnavailableError(config.REDIS_PORT)

    return redis

def create_inventory_client() -> redis.Redis:
    r = redis.Redis(host=config.REDIS_HOST, port=config.REDIS_PORT, db=config.REDIS_INVENTORY_DB, decode_responses=True,
                    socket_connect_timeout=config.REDIS_SOCKET_TIMEOUT, socket_timeout=config.REDIS_SOCKET_TIMEOUT)

    if r.ping():
        print("Connected to Inventory.")
    else:
        print("Failed to connect to Inventory.")
        raise errors.VaultUnavailableError(config.REDIS_PORT)

    return redis

def health_check(vault_client , inventory_client) -> HealthStatus:
    return HealthStatus(vault_client.ping() , inventory_client.ping() , vault_client.dbsize() , inventory_client.dbsize() , vault_client.info().get('uptime_in_seconds') + inventory_client.info().get('uptime_in_seconds'))


def close_all(vault_client , inventory_client) -> None:
    vault_client.close()
    inventory_client.close()
    print("Disconnected from Vault.")
    print("Disconnected from Inventory.")



