"""Public MST configuration used by the browser wallet integration.

This module deliberately contains no signing code or wallet credentials.
"""
import json
import os
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
CONTRACT_ABI_PATH = Path(__file__).with_name("contract_abi.json")

MST_RPC_URL = os.getenv("MST_RPC_URL", "")
MST_CHAIN_ID = int(os.getenv("MST_CHAIN_ID", "91562037"))
MST_CHAIN_NAME = os.getenv("MST_CHAIN_NAME", "MST Testnet")
MST_CONTRACT_ADDRESS = os.getenv("MST_CONTRACT_ADDRESS", "")
MST_EXPLORER_URL = os.getenv("MST_EXPLORER_URL", "")
MST_PRIVATE_KEY = os.getenv("MST_PRIVATE_KEY", "")


def public_config() -> dict:
    """Return only data a browser needs to construct its contract call."""
    return {
        "rpcUrl": MST_RPC_URL,
        "chainId": MST_CHAIN_ID,
        "chainName": MST_CHAIN_NAME,
        "contractAddress": MST_CONTRACT_ADDRESS,
        "explorerUrl": MST_EXPLORER_URL,
        "abi": json.loads(CONTRACT_ABI_PATH.read_text(encoding="utf-8")),
        "walletRequired": True,
    }
