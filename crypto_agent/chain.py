"""Lecture seule de la blockchain Base (JSON-RPC) : soldes ETH et ERC-20 d'un wallet.

Aucune signature, aucune clé privée ici. L'exécution réelle des ordres est
volontairement absente de l'étape 1 (simulation).
"""
import httpx

from .config import BASE_RPC_URL

BALANCE_OF = "0x70a08231"  # balanceOf(address)
DECIMALS = "0x313ce567"    # decimals()


def _rpc(method: str, params: list, client: httpx.Client) -> str:
    r = client.post(BASE_RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(body["error"])
    return body["result"]


def eth_balance(wallet: str, client: httpx.Client) -> float:
    return int(_rpc("eth_getBalance", [wallet, "latest"], client), 16) / 1e18


def erc20_balance(token: str, wallet: str, client: httpx.Client) -> float:
    data = BALANCE_OF + wallet.lower().removeprefix("0x").rjust(64, "0")
    raw = int(_rpc("eth_call", [{"to": token, "data": data}, "latest"], client), 16)
    dec = int(_rpc("eth_call", [{"to": token, "data": DECIMALS}, "latest"], client), 16)
    return raw / 10**dec


def block_number(client: httpx.Client) -> int:
    return int(_rpc("eth_blockNumber", [], client), 16)
