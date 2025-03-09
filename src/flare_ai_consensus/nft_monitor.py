import time
import sys
from web3 import Web3
import json

# -----------------------------
# Configuration
# -----------------------------
COSTON2_RPC_URL = "https://coston2-api.flare.network/ext/bc/C/rpc"
POLL_INTERVAL = 5

AGGREGATOR_ADDRESS = "0xb228A5DC5Db4eF7560637cd5dE745f12dD92DA1f"
PRIVATE_KEY = "0xa7f187f7bfdac71439e1579b69ce21794bf6ebdfa03fe950e232af56f48ad39a"

FLARE_FACT_CHECKER_ADDRESS = "0xBb242f415dd53e47b0a8c6E71f8D1A0A32ce4F90"

with open("/app/src/flare_ai_consensus/abi.json", "r") as abi_file:
    FLARE_FACT_CHECKER_ABI = json.load(abi_file)

def main():
    # -----------------------------
    # Setup Web3
    # -----------------------------
    web3 = Web3(Web3.HTTPProvider(COSTON2_RPC_URL))

    if not web3.is_connected():
        print("Error: Could not connect to Flare (Coston2) RPC.")
        sys.exit(1)
    print("Successfully connected to Flare Coston2 testnet.")

    # ----------------------------- 
    # Instantiate contract
    # -----------------------------
    contract = web3.eth.contract(
        address=FLARE_FACT_CHECKER_ADDRESS,
        abi=FLARE_FACT_CHECKER_ABI["abi"]
    )

    # -----------------------------
    # Create event filter
    # -----------------------------
    threshold_filter = contract.events.ThresholdReached.create_filter(from_block="latest")
    print("Filter created. Listening for ThresholdReached events...")

    # Create account object for aggregator
    aggregator_acct = web3.eth.account.from_key(PRIVATE_KEY)

    try:
        while True:
            new_events = threshold_filter.get_new_entries()
            for event in new_events:
                # Parse event data
                request_id = event["args"]["requestId"]
                on_chain_verifiers = event["args"]["verifiers"]

                print("\n[+] ThresholdReached Event Detected!")
                print(f"    - requestId: {request_id}")
                print(f"    - On-chain verifiers: {on_chain_verifiers}")

                # Equal-split distribution (placeholder)
                verifier_count = len(on_chain_verifiers)

                aggregator_balance_before = web3.eth.get_balance(AGGREGATOR_ADDRESS)

                withdraw_fees_tx_hash = withdraw_fees_from_contract(
                    web3, contract, aggregator_acct
                )
                # Wait for the withdrawal to complete
                if withdraw_fees_tx_hash:
                    receipt = web3.eth.wait_for_transaction_receipt(withdraw_fees_tx_hash)
                    if receipt.status == 1:
                        print(f"    Withdraw TX succeeded in block {receipt.blockNumber}")
                    else:
                        print("    Withdraw TX failed. Aborting distribution.")
                        continue
                else:
                    print("    Error building or sending withdrawFees transaction. Skipping.")
                    continue

                print("\n    --> Distributing withdrawn rewards to verifiers:")



                aggregator_balance_after = web3.eth.get_balance(AGGREGATOR_ADDRESS)
                actual_withdrawn_amount = aggregator_balance_after - aggregator_balance_before


                if actual_withdrawn_amount <= 0:
                    print("    No new fees were withdrawn (or negative?). Aborting distribution.")
                    continue

                share_per_verifier_wei = actual_withdrawn_amount // verifier_count  # integer division

                # B. Send each verifier their share
                for v in on_chain_verifiers:
                    # Build a transaction to send FLR from aggregator to the verifier
                    tx = {
                        "chainId": 114,  # EIP-155 chain ID for Coston2
                        "from": AGGREGATOR_ADDRESS,
                        "to": v,
                        "value": share_per_verifier_wei,  # portion for each verifier
                        "nonce": web3.eth.get_transaction_count(AGGREGATOR_ADDRESS),
                        "gas": 21000,  # For a simple FLR transfer
                        "gasPrice": web3.to_wei("30", "gwei"),  # Example 30 gwei
                    }
                    signed_tx = aggregator_acct.sign_transaction(tx)
                    send_tx_hash = web3.eth.send_raw_transaction(signed_tx.raw_transaction)
                    print(f"        Sending {share_per_verifier_wei} wei to {v}. Tx: {send_tx_hash.hex()}")
                    receipt = web3.eth.wait_for_transaction_receipt(send_tx_hash)
                    if receipt.status != 1:
                        print("Distribution transaction failed!")
                        break


            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\nAggregator script interrupted. Exiting gracefully...")

def withdraw_fees_from_contract(web3, contract, aggregator_acct):
    try:
        withdraw_tx = contract.functions.withdrawFees().build_transaction({
            "chainId": 114,  # EIP-155 chain ID for Coston2
            "from": aggregator_acct.address,
            "nonce": web3.eth.get_transaction_count(aggregator_acct.address),
            "gas": 100000,
            "gasPrice": web3.to_wei("30", "gwei"),
        })
        signed = aggregator_acct.sign_transaction(withdraw_tx)
        tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
        return tx_hash
    except Exception as e:
        print(f"    Error in withdraw_fees_from_contract: {e}")
        return None

if __name__ == "__main__":
    main()