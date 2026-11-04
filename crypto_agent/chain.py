"""Lecture seule de la blockchain Base (JSON-RPC) : soldes d'un wallet, frais d'un pool.

Aucune signature, aucune clé privée ici. L'exécution réelle des ordres est
volontairement absente de l'étape 1 (simulation).
"""
import re

import httpx

from .config import BASE_RPC_URL

ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
BALANCE_OF = "0x70a08231"  # balanceOf(address)
DECIMALS = "0x313ce567"    # decimals()
FEE = "0xddca3f43"         # fee() des pools à liquidité concentrée, en millionièmes


class RPCUnavailable(RuntimeError):
    """Le nœud RPC ne répond pas (réseau, statut HTTP, réponse illisible)."""


def check_address(address: str) -> str:
    if not isinstance(address, str) or not ADDRESS.match(address):
        raise ValueError("adresse invalide : attendu 0x suivi de 40 caractères hexadécimaux")
    return address


def _rpc(method: str, params: list, client: httpx.Client) -> str:
    # Les messages d'erreur ne citent jamais l'URL : une URL RPC privée contient sa clé.
    try:
        r = client.post(BASE_RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        r.raise_for_status()
        body = r.json()
    except httpx.HTTPStatusError as e:
        raise RPCUnavailable(f"RPC Base : statut {e.response.status_code}") from None
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as e:
        raise RPCUnavailable(f"RPC Base injoignable ou mal configuré ({type(e).__name__})") from None
    if not isinstance(body, dict):
        raise RPCUnavailable("RPC Base : réponse qui n'est pas un objet JSON")
    if "error" in body:
        err = body["error"]
        msg = err.get("message", "erreur") if isinstance(err, dict) else "erreur"
        raise RuntimeError(f"RPC Base : {str(msg)[:200]}")
    result = body.get("result")
    if not isinstance(result, str) or not result.startswith("0x"):
        raise RuntimeError("RPC Base : réponse inattendue")
    return result


def _int(hexstr: str) -> int:
    if hexstr in ("0x", ""):
        raise RuntimeError("RPC Base : résultat vide (pas un contrat compatible ?)")
    return int(hexstr, 16)


def eth_balance(wallet: str, client: httpx.Client) -> float:
    check_address(wallet)
    return _int(_rpc("eth_getBalance", [wallet, "latest"], client)) / 1e18


def erc20_balance(token: str, wallet: str, client: httpx.Client) -> float:
    check_address(token)
    check_address(wallet)
    data = BALANCE_OF + wallet.lower().removeprefix("0x").rjust(64, "0")
    raw = _int(_rpc("eth_call", [{"to": token, "data": data}, "latest"], client))
    dec = _int(_rpc("eth_call", [{"to": token, "data": DECIMALS}, "latest"], client))
    return raw / 10**dec


def block_number(client: httpx.Client) -> int:
    return _int(_rpc("eth_blockNumber", [], client))


def pool_fee(pool: str, client: httpx.Client) -> float:
    """Frais d'un pool à liquidité concentrée (Uniswap v3, PancakeSwap v3, Aerodrome Slipstream),
    en fraction : 3000 millionièmes -> 0,003. Échoue sur les autres types de pools."""
    check_address(pool)
    raw = _int(_rpc("eth_call", [{"to": pool, "data": FEE}, "latest"], client))
    if not 0 < raw <= 100_000:
        raise RuntimeError(f"frais de pool hors bornes ({raw})")
    return raw / 1_000_000
