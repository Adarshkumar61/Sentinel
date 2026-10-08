"""Automated MST contract signing and verification backend service."""

from datetime import datetime, timezone
import os
import threading
import time

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from .blockchain_config import (
    MST_CHAIN_ID,
    MST_CHAIN_NAME,
    MST_CONTRACT_ADDRESS,
    MST_PRIVATE_KEY,
    MST_RPC_URL,
    public_config,
)


class BlockchainVerificationError(RuntimeError):
    pass


_TRANSACTION_LOCK = threading.Lock()


class BlockchainClient:
    def __init__(self):
        if not MST_RPC_URL or not MST_CONTRACT_ADDRESS:
            raise BlockchainVerificationError(
                "MST blockchain configuration is unavailable."
            )

        self.w3 = Web3(
            Web3.HTTPProvider(
                MST_RPC_URL,
                request_kwargs={"timeout": 20},
            )
        )

        self.w3.middleware_onion.inject(
            ExtraDataToPOAMiddleware,
            layer=0,
        )

        if not self.w3.is_connected():
            raise BlockchainVerificationError("MST RPC is unavailable.")

        try:
            connected_chain_id = self.w3.eth.chain_id
        except Exception as error:
            raise BlockchainVerificationError(
                f"Could not read MST chain ID: {error}"
            ) from error

        if connected_chain_id != MST_CHAIN_ID:
            raise BlockchainVerificationError(
                "MST chain ID mismatch: "
                f"RPC returned {connected_chain_id}, "
                f"but configuration expects {MST_CHAIN_ID}."
            )

        config = public_config()
        self.contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(MST_CONTRACT_ADDRESS),
            abi=config["abi"],
        )

    @staticmethod
    def _normalise_hash(value: str) -> str:
        """Return a clean lowercase 64-character SHA-256 hex string."""
        clean = value.removeprefix("0x").lower()
        if len(clean) != 64:
            raise BlockchainVerificationError(
                f"Invalid SHA-256 evidence hash length: {len(clean)}"
            )
        try:
            bytes.fromhex(clean)
        except ValueError as error:
            raise BlockchainVerificationError(
                "Evidence hash is not valid hexadecimal."
            ) from error
        return clean

    @staticmethod
    def _normalise_tx_hash(tx_hash) -> str:
        value = tx_hash.hex()
        return value if value.startswith("0x") else "0x" + value

    def register_evidence(self, event_id: str, evidence_hash: str) -> dict:
        """Sign and submit one Sentinel evidence proof to the MST contract."""
        if not MST_PRIVATE_KEY:
            raise BlockchainVerificationError(
                "Automatic signing key not configured (MST_PRIVATE_KEY missing)."
            )

        clean_hash = self._normalise_hash(evidence_hash)

        with _TRANSACTION_LOCK:
            try:
                account = self.w3.eth.account.from_key(MST_PRIVATE_KEY)
                event_id_bytes = Web3.keccak(text=event_id)
                evidence_hash_bytes = bytes.fromhex(clean_hash)

                nonce = self.w3.eth.get_transaction_count(
                    account.address,
                    "pending",
                )
                gas_price = self.w3.eth.gas_price

                if not gas_price or gas_price <= 0:
                    raise BlockchainVerificationError(
                        "MST RPC returned an invalid gas price."
                    )

                transaction = (
                    self.contract
                    .functions
                    .registerEvidence(event_id_bytes, evidence_hash_bytes)
                    .build_transaction(
                        {
                            "from": account.address,
                            "nonce": nonce,
                            "chainId": MST_CHAIN_ID,
                            "gasPrice": gas_price,
                        }
                    )
                )

                estimated_gas = self.w3.eth.estimate_gas(transaction)
                transaction["gas"] = max(
                    int(estimated_gas * 1.20),
                    estimated_gas + 10000,
                )

                signed_tx = account.sign_transaction(transaction)
                raw_tx = getattr(signed_tx, "raw_transaction", None)
                if raw_tx is None:
                    raw_tx = getattr(signed_tx, "rawTransaction", None)
                if raw_tx is None:
                    raise BlockchainVerificationError(
                        "Could not extract signed transaction bytes."
                    )

                tx_hash_bytes = self.w3.eth.send_raw_transaction(raw_tx)
                tx_hash = self._normalise_tx_hash(tx_hash_bytes)

                receipt = self.w3.eth.wait_for_transaction_receipt(
                    tx_hash,
                    timeout=120,
                    poll_latency=2,
                )

                if receipt.status != 1:
                    raise BlockchainVerificationError(
                        f"Transaction reverted on MST Testnet (tx: {tx_hash})."
                    )

                block = self.w3.eth.get_block(receipt.blockNumber)
                timestamp = (
                    block.timestamp
                    if block
                    else int(datetime.now(timezone.utc).timestamp())
                )

                formatted_event_id = event_id_bytes.hex()
                if not formatted_event_id.startswith("0x"):
                    formatted_event_id = "0x" + formatted_event_id

                return {
                    "transaction_hash": tx_hash,
                    "blockchain_event_id": formatted_event_id,
                    "registered_by": account.address,
                    "block_number": receipt.blockNumber,
                    "contract_address": MST_CONTRACT_ADDRESS,
                    "blockchain_network": MST_CHAIN_NAME,
                    "registered_at": (
                        datetime.fromtimestamp(
                            timestamp,
                            tz=timezone.utc,
                        )
                        .astimezone()
                        .isoformat(timespec="seconds")
                    ),
                }

            except BlockchainVerificationError:
                raise
            except Exception as error:
                raise BlockchainVerificationError(
                    f"Automatic MST registration failed: {error}"
                ) from error

    def verify_registration(
        self,
        event_id: str,
        evidence_hash: str,
        transaction_hash: str,
    ) -> dict:
        """
        Validate the exact evidence hash from both the emitted event and
        contract storage. The returned on_chain_evidence_hash is the value
        actually read back from MST, so the UI can distinguish blockchain
        proof from the locally calculated hash.
        """
        expected_event_id = Web3.keccak(text=event_id)
        expected_hash_clean = self._normalise_hash(evidence_hash)

        try:
            receipt = self.w3.eth.get_transaction_receipt(transaction_hash)
        except Exception as error:
            raise BlockchainVerificationError(
                "Transaction receipt is not available yet."
            ) from error

        if receipt is None:
            raise BlockchainVerificationError("Transaction is still pending.")

        if receipt.status != 1:
            raise BlockchainVerificationError("Transaction reverted on MST Testnet.")

        if (
            receipt.to is None
            or receipt.to.lower() != MST_CONTRACT_ADDRESS.lower()
        ):
            raise BlockchainVerificationError(
                "Transaction was not sent to the SentinelEvidence contract."
            )

        try:
            logs = (
                self.contract
                .events
                .EvidenceRegistered()
                .process_receipt(receipt)
            )

            matching_log = next(
                (
                    log
                    for log in logs
                    if log["args"]["eventId"] == expected_event_id
                    and log["args"]["evidenceHash"].hex().lower()
                    == expected_hash_clean
                ),
                None,
            )

            if matching_log is None:
                raise BlockchainVerificationError(
                    "EvidenceRegistered event does not match this Sentinel event."
                )

            on_chain_hash, timestamp, registered_by = (
                self.contract
                .functions
                .getEvidence(expected_event_id)
                .call()
            )

        except BlockchainVerificationError:
            raise
        except Exception as error:
            raise BlockchainVerificationError(
                "Could not read and validate the MST evidence record."
            ) from error

        if timestamp == 0:
            raise BlockchainVerificationError(
                "On-chain evidence record does not exist."
            )

        on_chain_hash_clean = on_chain_hash.hex().lower()
        if on_chain_hash_clean != expected_hash_clean:
            raise BlockchainVerificationError(
                "On-chain evidence hash does not match this Sentinel event."
            )

        formatted_event_id = expected_event_id.hex()
        if not formatted_event_id.startswith("0x"):
            formatted_event_id = "0x" + formatted_event_id

        return {
            "blockchain_event_id": formatted_event_id,
            "registered_at": (
                datetime.fromtimestamp(
                    timestamp,
                    tz=timezone.utc,
                )
                .astimezone()
                .isoformat(timespec="seconds")
            ),
            "registered_by": registered_by,
            "transaction_hash": transaction_hash,
            "block_number": receipt.blockNumber,
            "contract_address": MST_CONTRACT_ADDRESS,
            "blockchain_network": MST_CHAIN_NAME,
            "on_chain_evidence_hash": "0x" + on_chain_hash_clean,
        }
