"""Read-only MST contract verification. Browser wallets remain the only signers."""
from datetime import datetime, timezone

from web3 import Web3

from .blockchain_config import MST_CHAIN_ID, MST_CHAIN_NAME, MST_CONTRACT_ADDRESS, MST_PRIVATE_KEY, MST_RPC_URL, public_config


class BlockchainVerificationError(RuntimeError):
    pass


class BlockchainClient:
    def __init__(self):
        if not MST_RPC_URL or not MST_CONTRACT_ADDRESS:
            raise BlockchainVerificationError("MST blockchain configuration is unavailable.")
        self.w3 = Web3(Web3.HTTPProvider(MST_RPC_URL, request_kwargs={"timeout": 12}))
        if not self.w3.is_connected():
            raise BlockchainVerificationError("MST RPC is unavailable. Try verification again shortly.")
        config = public_config()
        self.contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(MST_CONTRACT_ADDRESS), abi=config["abi"]
        )

    def verify_registration(self, event_id: str, evidence_hash: str, transaction_hash: str) -> dict:
        """Validate receipt log and contract state for one exact local event."""
        expected_event_id = Web3.keccak(text=event_id)
        try:
            receipt = self.w3.eth.get_transaction_receipt(transaction_hash)
        except Exception as error:
            raise BlockchainVerificationError("Transaction receipt is not available yet.") from error
        if receipt is None:
            raise BlockchainVerificationError("Transaction is still pending.")
        if receipt.status != 1:
            raise BlockchainVerificationError("Transaction reverted on MST Testnet.")
        if receipt.to is None or receipt.to.lower() != MST_CONTRACT_ADDRESS.lower():
            raise BlockchainVerificationError("Transaction was not sent to the SentinelEvidence contract.")

        try:
            logs = self.contract.events.EvidenceRegistered().process_receipt(receipt)
            matching_log = next(
                (
                    log for log in logs
                    if log["args"]["eventId"] == expected_event_id
                    and log["args"]["evidenceHash"].hex().lower() == evidence_hash.removeprefix("0x").lower()
                ),
                None,
            )
            on_chain_hash, timestamp, registered_by = self.contract.functions.getEvidence(expected_event_id).call()
        except Exception as error:
            raise BlockchainVerificationError("Could not read and validate the MST evidence record.") from error

        if matching_log is None or timestamp == 0 or on_chain_hash.hex().lower() != evidence_hash.removeprefix("0x").lower():
            raise BlockchainVerificationError("On-chain evidence does not match this Sentinel event.")

        return {
            "blockchain_event_id": expected_event_id.hex(),
            "registered_at": datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone().isoformat(timespec="seconds"),
            "registered_by": registered_by,
            "transaction_hash": transaction_hash,
        }

    def register_evidence(self, event_id: str, evidence_hash: str) -> dict:
        """Sign and submit one evidence proof using only the server environment key."""
        if not MST_PRIVATE_KEY:
            raise BlockchainVerificationError("Automatic signing is not configured. Set MST_PRIVATE_KEY on the server.")
        try:
            account = self.w3.eth.account.from_key(MST_PRIVATE_KEY)
            event_id_bytes = Web3.keccak(text=event_id)
            evidence_hash_bytes = bytes.fromhex(evidence_hash.removeprefix("0x"))
            transaction = self.contract.functions.registerEvidence(event_id_bytes, evidence_hash_bytes).build_transaction({
                "from": account.address,
                "nonce": self.w3.eth.get_transaction_count(account.address, "pending"),
                "chainId": MST_CHAIN_ID,
                "gasPrice": self.w3.eth.gas_price,
            })
            transaction["gas"] = self.w3.eth.estimate_gas(transaction)
            signed = account.sign_transaction(transaction)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction).hex()
            return {"transaction_hash": tx_hash, "signer": account.address}
        except Exception as error:
            raise BlockchainVerificationError("Automatic MST registration could not be submitted.") from error
