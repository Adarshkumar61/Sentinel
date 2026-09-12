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


# ---------------------------------------------------------------------------
# Transaction serialization
# ---------------------------------------------------------------------------
#
# Sentinel can detect multiple events close together.
# All of them use the same backend wallet.
#
# A single wallet must not have multiple threads racing to choose/send
# transactions with the same nonce.
#
# Therefore only ONE MST registration transaction is constructed/sent at a
# time. This keeps automatic registration safe while still being fully
# unattended.
#
_TRANSACTION_LOCK = threading.Lock()


class BlockchainClient:
    def __init__(self):
        if not MST_RPC_URL or not MST_CONTRACT_ADDRESS:
            raise BlockchainVerificationError(
                "MST blockchain configuration is unavailable."
            )

        # Connect to MST RPC.
        self.w3 = Web3(
            Web3.HTTPProvider(
                MST_RPC_URL,
                request_kwargs={"timeout": 20},
            )
        )

        # ------------------------------------------------------------------
        # MST Testnet uses a POA-style block header with extended extraData.
        #
        # Without this middleware Web3.py can throw:
        #
        # "The field extraData is 97 bytes, but should be 32"
        #
        # Web3.py requires ExtraDataToPOAMiddleware at layer 0.
        # ------------------------------------------------------------------
        self.w3.middleware_onion.inject(
            ExtraDataToPOAMiddleware,
            layer=0,
        )

        if not self.w3.is_connected():
            raise BlockchainVerificationError(
                "MST RPC is unavailable."
            )

        # Verify that the RPC is actually the configured MST chain.
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

    # ----------------------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------------------

    @staticmethod
    def _normalise_hash(value: str) -> str:
        """Return a clean lowercase 64-character SHA-256 hex string."""

        clean = value.removeprefix("0x").lower()

        if len(clean) != 64:
            raise BlockchainVerificationError(
                "Invalid SHA-256 evidence hash length: "
                f"{len(clean)}"
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
        """Convert a Web3 transaction hash into a normal 0x-prefixed string."""

        value = tx_hash.hex()

        if not value.startswith("0x"):
            value = "0x" + value

        return value

    # ----------------------------------------------------------------------
    # Automatic registration
    # ----------------------------------------------------------------------

    def register_evidence(
        self,
        event_id: str,
        evidence_hash: str,
    ) -> dict:
        """
        Sign and submit one evidence proof using only the server environment
        private key.

        This function is completely automatic. No BridgeKey/user approval is
        required for backend CCTV evidence registration.
        """

        if not MST_PRIVATE_KEY:
            raise BlockchainVerificationError(
                "Automatic signing key not configured "
                "(MST_PRIVATE_KEY missing)."
            )

        clean_hash = self._normalise_hash(evidence_hash)

        # ------------------------------------------------------------------
        # IMPORTANT:
        #
        # One backend wallet is signing all Sentinel registrations.
        # Serialize the complete nonce -> build -> sign -> send -> receipt
        # process so multiple detection threads cannot race each other.
        # ------------------------------------------------------------------
        with _TRANSACTION_LOCK:

            try:
                account = self.w3.eth.account.from_key(MST_PRIVATE_KEY)

                event_id_bytes = Web3.keccak(text=event_id)
                evidence_hash_bytes = bytes.fromhex(clean_hash)

                # ----------------------------------------------------------
                # Get the pending nonce.
                #
                # "pending" includes transactions that have already been
                # submitted but are not mined yet.
                # ----------------------------------------------------------
                nonce = self.w3.eth.get_transaction_count(
                    account.address,
                    "pending",
                )

                # ----------------------------------------------------------
                # Get current network gas price.
                # ----------------------------------------------------------
                gas_price = self.w3.eth.gas_price

                if not gas_price or gas_price <= 0:
                    raise BlockchainVerificationError(
                        "MST RPC returned an invalid gas price."
                    )

                # ----------------------------------------------------------
                # Build contract transaction.
                # ----------------------------------------------------------
                transaction = (
                    self.contract
                    .functions
                    .registerEvidence(
                        event_id_bytes,
                        evidence_hash_bytes,
                    )
                    .build_transaction(
                        {
                            "from": account.address,
                            "nonce": nonce,
                            "chainId": MST_CHAIN_ID,
                            "gasPrice": gas_price,
                        }
                    )
                )

                # ----------------------------------------------------------
                # Estimate gas.
                # ----------------------------------------------------------
                estimated_gas = self.w3.eth.estimate_gas(transaction)

                # Add a small safety margin.
                transaction["gas"] = max(
                    int(estimated_gas * 1.20),
                    estimated_gas + 10000,
                )

                # ----------------------------------------------------------
                # Sign locally with backend private key.
                # ----------------------------------------------------------
                signed_tx = account.sign_transaction(transaction)

                raw_tx = getattr(
                    signed_tx,
                    "raw_transaction",
                    None,
                )

                if raw_tx is None:
                    raw_tx = getattr(
                        signed_tx,
                        "rawTransaction",
                        None,
                    )

                if raw_tx is None:
                    raise BlockchainVerificationError(
                        "Could not extract signed transaction bytes."
                    )

                # ----------------------------------------------------------
                # Submit transaction.
                # ----------------------------------------------------------
                tx_hash_bytes = self.w3.eth.send_raw_transaction(raw_tx)

                tx_hash = self._normalise_tx_hash(tx_hash_bytes)

                # ----------------------------------------------------------
                # Wait for mining.
                #
                # Use a longer timeout than before because Testnet nodes can
                # occasionally take more than 30 seconds.
                # ----------------------------------------------------------
                receipt = self.w3.eth.wait_for_transaction_receipt(
                    tx_hash,
                    timeout=120,
                    poll_latency=2,
                )

                if receipt.status != 1:
                    raise BlockchainVerificationError(
                        "Transaction reverted on MST Testnet "
                        f"(tx: {tx_hash})."
                    )

                # ----------------------------------------------------------
                # Read the mined block timestamp.
                # ----------------------------------------------------------
                block = self.w3.eth.get_block(
                    receipt.blockNumber
                )

                timestamp = (
                    block.timestamp
                    if block
                    else int(
                        datetime.now(timezone.utc).timestamp()
                    )
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
                        datetime
                        .fromtimestamp(
                            timestamp,
                            tz=timezone.utc,
                        )
                        .astimezone()
                        .isoformat(
                            timespec="seconds"
                        )
                    ),
                }

            except BlockchainVerificationError:
                raise

            except Exception as error:
                err_msg = str(error)

                raise BlockchainVerificationError(
                    "Automatic MST registration failed: "
                    f"{err_msg}"
                ) from error

    # ----------------------------------------------------------------------
    # Verification
    # ----------------------------------------------------------------------

    def verify_registration(
        self,
        event_id: str,
        evidence_hash: str,
        transaction_hash: str,
    ) -> dict:
        """
        Validate receipt log and contract state for one exact local event.
        """

        expected_event_id = Web3.keccak(
            text=event_id
        )

        expected_hash_clean = self._normalise_hash(
            evidence_hash
        )

        try:
            receipt = self.w3.eth.get_transaction_receipt(
                transaction_hash
            )

        except Exception as error:
            raise BlockchainVerificationError(
                "Transaction receipt is not available yet."
            ) from error

        if receipt is None:
            raise BlockchainVerificationError(
                "Transaction is still pending."
            )

        if receipt.status != 1:
            raise BlockchainVerificationError(
                "Transaction reverted on MST Testnet."
            )

        if (
            receipt.to is None
            or receipt.to.lower()
            != MST_CONTRACT_ADDRESS.lower()
        ):
            raise BlockchainVerificationError(
                "Transaction was not sent to the SentinelEvidence contract."
            )

        try:
            # --------------------------------------------------------------
            # Decode EvidenceRegistered event from receipt.
            # --------------------------------------------------------------
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
                    if log["args"]["eventId"]
                    == expected_event_id
                    and log["args"]["evidenceHash"]
                    .hex()
                    .lower()
                    == expected_hash_clean
                ),
                None,
            )

            if matching_log is None:
                raise BlockchainVerificationError(
                    "EvidenceRegistered event does not match "
                    "this Sentinel event."
                )

            # --------------------------------------------------------------
            # Read actual contract storage.
            # --------------------------------------------------------------
            (
                on_chain_hash,
                timestamp,
                registered_by,
            ) = (
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

        # --------------------------------------------------------------
        # Verify hash + timestamp.
        # --------------------------------------------------------------
        if timestamp == 0:
            raise BlockchainVerificationError(
                "On-chain evidence record does not exist."
            )

        if (
            on_chain_hash.hex().lower()
            != expected_hash_clean
        ):
            raise BlockchainVerificationError(
                "On-chain evidence hash does not match "
                "this Sentinel event."
            )

        formatted_event_id = expected_event_id.hex()

        if not formatted_event_id.startswith("0x"):
            formatted_event_id = "0x" + formatted_event_id

        return {
            "blockchain_event_id": formatted_event_id,
            "registered_at": (
                datetime
                .fromtimestamp(
                    timestamp,
                    tz=timezone.utc,
                )
                .astimezone()
                .isoformat(
                    timespec="seconds"
                )
            ),
            "registered_by": registered_by,
            "transaction_hash": transaction_hash,
            "block_number": receipt.blockNumber,
            "contract_address": MST_CONTRACT_ADDRESS,
            "blockchain_network": MST_CHAIN_NAME,
        }