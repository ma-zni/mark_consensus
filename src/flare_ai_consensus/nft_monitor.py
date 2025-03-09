import time
import sys
import os
import re
import json
from typing import Any

from web3 import Web3

from flare_ai_consensus.consensus.aggregator import centralized_llm_aggregator
from flare_ai_consensus.settings import AggregatorConfig, ModelConfig
from flare_ai_consensus.router import OpenRouterProvider

import random
from itertools import permutations

# -----------------------------
# Configuration
# -----------------------------
COSTON2_RPC_URL = "https://coston2-api.flare.network/ext/bc/C/rpc"
POLL_INTERVAL = 3

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

                verifier_results_map = {}
                for v in on_chain_verifiers:
                    result_str = contract.functions.getVerifierResult(request_id, v).call()

                    # Attempt to parse as JSON
                    try:
                        parsed_json = json.loads(result_str)
                        print(f"Verifier {v} => JSON parsed successfully:\n{parsed_json}")
                        verifier_results_map[v] = parsed_json
                    except json.JSONDecodeError:
                        print(f"Verifier {v} => result not valid JSON. Using raw string.")
                        verifier_results_map[v] = result_str

                aggregated_score = aggregator_score_from_llm(verifier_results_map)
                aggregated_score_tx_hash=submit_aggregate_result(web3, contract, aggregator_acct, request_id, aggregated_score,verifier_results_map)
                receipt=web3.eth.wait_for_transaction_receipt(aggregated_score_tx_hash)

                print(f"Aggregated correctness score (0-100): {aggregated_score}. Was aggregated score submitted successfully: ",receipt.status==1,"HASH: ",aggregated_score_tx_hash.hex())      
                verifier_count = len(on_chain_verifiers)

                aggregator_balance_before = web3.eth.get_balance(AGGREGATOR_ADDRESS)

                withdraw_fees_tx_hash = withdraw_fees_from_contract(
                    web3, contract, aggregator_acct
                )
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
                shap_values_dict = shap_values(verifier_results_map)
                print("SHAP VALUES: ",shap_values_dict)
                address2reward = {}
                sum_of_shap_values = sum(shap_values_dict.values())
                for verifier in on_chain_verifiers:
                    address2reward[verifier] = actual_withdrawn_amount * shap_values_dict[verifier] / sum_of_shap_values

                if actual_withdrawn_amount <= 0:
                    print("    No new fees were withdrawn (or negative?). Aborting distribution.")
                    continue

                

                # B. Send each verifier their share
                for v in on_chain_verifiers:
                    # Build a transaction to send FLR from aggregator to the verifier
                    
                    tx = {
                        "chainId": 114,  # EIP-155 chain ID for Coston2
                        "from": AGGREGATOR_ADDRESS,
                        "to": v,
                        "value": int(address2reward[v]),  # portion for each verifier
                        "nonce": web3.eth.get_transaction_count(AGGREGATOR_ADDRESS),
                        "gas": 21000,  # For a simple FLR transfer
                        "gasPrice": web3.to_wei("30", "gwei"),  # Example 30 gwei
                    }
                    signed_tx = aggregator_acct.sign_transaction(tx)
                    send_tx_hash = web3.eth.send_raw_transaction(signed_tx.raw_transaction)
                    print(f"        Sending {int(address2reward[v])} wei to {v}. Tx: {send_tx_hash.hex()}")
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
    

def aggregator_score_from_llm(verifier_results_map: dict[str, Any]) -> int:


    def _build_summary(data: dict) -> dict:

        summary = {}

        # 1) Grab confirming/refuting arrays if present
        confirming = data.get("confirming", [])
        refuting = data.get("refuting", [])
        summary["confirming_count"] = len(confirming)
        summary["refuting_count"] = len(refuting)

        # 2) correctness_score (default 0 if missing)
        summary["correctness_score"] = data.get("correctness_score", 0)

        # 3) Replace [https://...] with [CITED IN ARTICLE] in the response
        raw_response = data.get("response", "")
        cleaned_response = re.sub(r"\[https?:\/\/[^\]]+\]", "[CITED IN ARTICLE]", raw_response)
        summary["response"] = cleaned_response
        print("\n\nSUMMARY\n", summary)
        return summary

    # ----------------------------------------------------------
    # 1) Create the provider & aggregator config inline
    # ----------------------------------------------------------
    api_key = os.environ.get("OPEN_ROUTER_API_KEY")

    provider = OpenRouterProvider(
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1"
    )

    aggregator_config = AggregatorConfig(
        model=ModelConfig(
            model_id="openai/gpt-3.5-turbo",
            max_tokens=1000,
            temperature=0.0,  # more deterministic
        ),
        approach="centralized-llm",
        context=[
            {
                "role": "system",
                "content": (
                    "You are given multiple short JSON summaries from verifiers, "
                    "each containing 'confirming_count', 'refuting_count', 'correctness_score', "
                    "and a 'response' (with replaced links marked with [CITED IN ARTICLE]). Your job is to produce "
                    "ONE integer from 0..100 that represents the overall correctness. "
                ),
            }
        ],
        prompt=[
            {
                "role": "system",
                "content": (
                    "Important: Return ONLY a single integer in the range [0..100]. "
                    "No words, no punctuation, no extra text—just the integer."
                ),
            },
        ],
    )

    # ----------------------------------------------------------
    # 2) Build cleaned summaries for aggregator
    # ----------------------------------------------------------
    aggregated_responses = {}

    for verifier, data in verifier_results_map.items():
        if isinstance(data, dict):
            # if there's a subfield 'response_json', use that
            if "response_json" in data and isinstance(data["response_json"], dict):
                summary_dict = _build_summary(data["response_json"])
            else:
                # or just treat the entire dict as the primary data
                summary_dict = _build_summary(data)
            # Convert final summary dict to JSON for the aggregator
            aggregated_responses[str(verifier)] = json.dumps(summary_dict)

        else:
            # If it's just a string, store as-is
            # (maybe you want to do further cleaning here, if desired)
            aggregated_responses[str(verifier)] = str(data)
    print("AGGREGRATED RESPONSES THAT GOES TO MODEL: ", aggregated_responses)
    # ----------------------------------------------------------
    # 3) Call the LLM-based aggregator
    # ----------------------------------------------------------
    aggregated_score_str = centralized_llm_aggregator(
        provider=provider,
        aggregator_config=aggregator_config,
        aggregated_responses=aggregated_responses,
    )
    # ----------------------------------------------------------
    # 4) Parse aggregator’s output as an integer [0..100]
    # ----------------------------------------------------------
    try:
        aggregated_score = int(aggregated_score_str.strip())
        if aggregated_score < 0:
            aggregated_score = 0
        if aggregated_score > 100:
            aggregated_score = 100
    except ValueError:
        # if the aggregator didn't return a valid integer, fallback to 50
        aggregated_score = 50
    print("\n\nAGGREGATED SCORE: ", aggregated_score)

    return aggregated_score



from collections import Counter
import json
import re
from typing import Any
from web3 import Web3

def submit_aggregate_result(
    web3: Web3,
    contract,
    aggregator_acct,
    request_id: int,
    aggregated_score: int,
    verifier_results_map: dict[str, Any],
):
    """
    Calls contract.submitAggregateResult(request_id, aggregate_json_string).
    The JSON string includes:
      - "aggregated_score": <int>,
      - "supporting_links": [top 5 most common 'confirming' links overall],
      - "opposing_links": [top 5 most common 'refuting' links overall].

    Example final JSON:
    {
      "aggregated_score": 96,
      "supporting_links": [
        "https://pubmed.ncbi.nlm.nih.gov/35005672",
        "https://pubmed.ncbi.nlm.nih.gov/35005211",
        ...
      ],
      "opposing_links": []
    }
    """

    print("VERIFIER RESULTS MAP:", verifier_results_map)

    # 1) Collect all confirming/refuting links from the verifiers
    all_confirming = []
    all_refuting = []

    for verifier, data in verifier_results_map.items():
        # data might have "response_json" or might be raw
        if isinstance(data, dict) and "response_json" in data:
            resp_json = data["response_json"]
        else:
            # If there's no 'response_json' subfield, maybe data itself is the dict
            # or it's a plain string. Adapt to your situation; here we'll treat
            # 'data' as the JSON if it has 'confirming' or 'refuting'
            resp_json = data

        if isinstance(resp_json, dict):
            confirming_links = resp_json.get("confirming", [])
            refuting_links   = resp_json.get("refuting", [])
            # Extend our global lists
            all_confirming.extend(confirming_links)
            all_refuting.extend(refuting_links)

    # 2) Get the top 5 most common links in confirming/refuting
    #    (Adjust "5" to as many as you like, or omit to keep them all)
    top_supporting = [link for link, _ in Counter(all_confirming).most_common(5)]
    top_opposing   = [link for link, _ in Counter(all_refuting).most_common(5)]

    # 3) Build the final JSON object to store on-chain
    aggregate_data = {
        "aggregated_score": aggregated_score,
        "supporting_links": top_supporting,
        "opposing_links":   top_opposing,
    }

    # Convert to string
    aggregate_str = json.dumps(aggregate_data)

    print("\n\n\nFinal JSON to store:\n", aggregate_str)

    # 4) Build the transaction to call submitAggregateResult(_requestId, aggregate_str)
    tx = contract.functions.submitAggregateResult(request_id, aggregate_str).build_transaction({
        "chainId": 114,  # EIP-155 chain ID for Coston2
        "from": aggregator_acct.address,
        "nonce": web3.eth.get_transaction_count(aggregator_acct.address),
        "gas": 2000000,  # adjust as needed
        "gasPrice": web3.to_wei("30", "gwei"),
    })

    # 5) Sign the transaction
    signed = aggregator_acct.sign_transaction(tx)

    # 6) Send
    tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
    return tx_hash



def shap_values(verifier_results_map):
    """
    EXACT Shapley contributions for aggregator_score_from_llm when there are exactly 3 verifiers.
    We enumerate all 3! = 6 permutations, so no random sampling is needed.
    """
    verifier_addresses = list(verifier_results_map.keys())
    if len(verifier_addresses) != 3:
        raise ValueError("This function is designed for exactly 3 verifiers.")

    # Initialize total contributions
    shap_contributions = {v: 0.0 for v in verifier_addresses}

    # Enumerate all permutations of the 3 verifiers
    all_permutations = list(permutations(verifier_addresses))

    # For each ordering, add verifiers one by one and measure marginal contribution
    for ordering in all_permutations:
        current_coalition = {}
        current_score = aggregator_score_from_llm(current_coalition)

        for verifier in ordering:
            prev_score = current_score
            current_coalition[verifier] = verifier_results_map[verifier]
            current_score = aggregator_score_from_llm(current_coalition)
            marginal_contribution = current_score - prev_score
            shap_contributions[verifier] += marginal_contribution

    # Average the contributions across the 6 permutations
    n_perm = float(len(all_permutations))  # should be 6 for p=3
    for v in verifier_addresses:
        shap_contributions[v] /= n_perm

    return shap_contributions


if __name__ == "__main__":
    main()