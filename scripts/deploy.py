#!/usr/bin/env python3
"""
Compile + deploy LiquidationBot using py-solc-x (no brownie required).

Usage:
    python scripts/deploy.py --network testnet
    python scripts/deploy.py --network mainnet

Steps performed automatically:
    1. Install solc 0.8.19 if not already present (py-solc-x downloads it)
    2. Compile contracts/LiquidationBot.sol
    3. Save ABI + bytecode to build/LiquidationBot.json
    4. Deploy to the selected network
    5. Write contract address to deployments.json and print .env snippet
"""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent

SOLC_VERSION = "0.8.19"

# ─── Network configs ──────────────────────────────────────────────────────────
NETWORKS = {
    "testnet": {
        "rpc_env":  "ALCHEMY_HTTP_URL_TESTNET",
        "rpc_fallback_env": "ALCHEMY_HTTP_URL",
        "chain_id": 421614,
        "pool":     "0xBfC91D59fdAA134A4ED45f7B584cAf96D7792Eff",
        "router":   "0xE592427A0AEce92De3Edee1F18E0157C05861564",
        "label":    "Arbitrum Sepolia",
    },
    "mainnet": {
        "rpc_env":  "ALCHEMY_HTTP_URL",
        "rpc_fallback_env": None,
        "chain_id": 42161,
        "pool":     "0x794a61358D6845594F94dc1DB02A252b5b4814aD",
        "router":   "0xE592427A0AEce92De3Edee1F18E0157C05861564",
        "label":    "Arbitrum One",
    },
}

# ─── Step 1: Compile ─────────────────────────────────────────────────────────

def compile_contract() -> tuple[list, str]:
    """
    Compile LiquidationBot.sol with py-solc-x.
    Returns (abi, bytecode_hex).
    Caches result in build/LiquidationBot.json.
    """
    build_path = ROOT / "build" / "LiquidationBot.json"

    # Try cache first (recompile if source is newer)
    source_path = ROOT / "contracts" / "LiquidationBot.sol"
    if (
        build_path.exists()
        and build_path.stat().st_mtime >= source_path.stat().st_mtime
    ):
        print("✓  Using cached build (source unchanged)")
        with open(build_path) as f:
            artifact = json.load(f)
        return artifact["abi"], artifact["bytecode"]

    print(f"Compiling {source_path.name} with solc {SOLC_VERSION}…")

    try:
        import solcx
    except ImportError:
        print("❌  py-solc-x not installed. Run: pip install py-solc-x")
        sys.exit(1)

    # Install the required compiler version if needed
    installed = [str(v) for v in solcx.get_installed_solc_versions()]
    if SOLC_VERSION not in installed:
        print(f"   Downloading solc {SOLC_VERSION} (one-time)…")
        solcx.install_solc(SOLC_VERSION)

    solcx.set_solc_version(SOLC_VERSION)

    source = source_path.read_text()

    result = solcx.compile_source(
        source,
        output_values=["abi", "bin"],
        solc_version=SOLC_VERSION,
        optimize=True,
        optimize_runs=200,
    )

    # py-solc-x keys the output as "<stdin>:ContractName"
    contract_key = next(k for k in result if "LiquidationBot" in k)
    abi      = result[contract_key]["abi"]
    bytecode = result[contract_key]["bin"]

    if not bytecode:
        print("❌  Compilation produced empty bytecode")
        sys.exit(1)

    # Prefix with 0x if missing
    if not bytecode.startswith("0x"):
        bytecode = "0x" + bytecode

    # Cache to build/
    build_path.parent.mkdir(parents=True, exist_ok=True)
    with open(build_path, "w") as f:
        json.dump({"abi": abi, "bytecode": bytecode}, f, indent=2)

    print(f"✓  Compiled → {build_path.relative_to(ROOT)}")
    return abi, bytecode


# ─── Step 2: Deploy ──────────────────────────────────────────────────────────

def deploy(network: str, abi: list, bytecode: str):
    from web3 import Web3
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")

    cfg = NETWORKS[network]

    # Resolve RPC URL
    rpc = os.environ.get(cfg["rpc_env"])
    if not rpc and cfg["rpc_fallback_env"]:
        rpc = os.environ.get(cfg["rpc_fallback_env"])
    if not rpc:
        print(f"❌  {cfg['rpc_env']} not set in .env")
        sys.exit(1)

    private_key = os.environ.get("PRIVATE_KEY")
    if not private_key:
        print("❌  PRIVATE_KEY not set in .env")
        sys.exit(1)

    print(f"\nConnecting to {cfg['label']} ({cfg['chain_id']})…")
    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        print(f"❌  Could not connect to RPC: {rpc}")
        sys.exit(1)
    print(f"✓  Connected  (latest block: {w3.eth.block_number:,})")

    account = w3.eth.account.from_key(private_key)
    balance = w3.eth.get_balance(account.address)

    print(f"\nDeployer:  {account.address}")
    print(f"Balance:   {w3.from_wei(balance, 'ether'):.6f} ETH")
    print(f"Pool:      {cfg['pool']}")
    print(f"Router:    {cfg['router']}")

    if balance == 0:
        print("\n❌  Deployer has 0 ETH — fund it with testnet ETH first")
        print("   Arbitrum Sepolia faucet: https://faucet.quicknode.com/arbitrum/sepolia")
        sys.exit(1)

    # Build constructor tx
    factory   = w3.eth.contract(abi=abi, bytecode=bytecode)
    gas_price = w3.eth.gas_price
    nonce     = w3.eth.get_transaction_count(account.address)

    deploy_tx = factory.constructor(
        cfg["pool"], cfg["router"]
    ).build_transaction({
        "from":     account.address,
        "nonce":    nonce,
        "gasPrice": gas_price,
        "chainId":  cfg["chain_id"],
    })

    # Estimate gas with 20% buffer
    try:
        estimated = w3.eth.estimate_gas(deploy_tx)
    except Exception as exc:
        print(f"❌  Gas estimation failed: {exc}")
        sys.exit(1)

    deploy_tx["gas"] = int(estimated * 1.2)

    cost_eth = w3.from_wei(deploy_tx["gas"] * gas_price, "ether")
    print(f"\nGas price:    {w3.from_wei(gas_price, 'gwei'):.4f} gwei")
    print(f"Gas estimate: {estimated:,}  (with buffer: {deploy_tx['gas']:,})")
    print(f"Deploy cost:  ~{cost_eth:.6f} ETH")

    # Sign and send
    signed  = account.sign_transaction(deploy_tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

    print(f"\nTx submitted: {tx_hash.hex()}")
    print("Waiting for confirmation…", end="", flush=True)

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300, poll_latency=2)
    print(" done")

    if receipt["status"] != 1:
        print(f"\n❌  Transaction reverted: {tx_hash.hex()}")
        sys.exit(1)

    contract_address = receipt["contractAddress"]

    print(f"""
╔══════════════════════════════════════════════════════════╗
║  ✅  LiquidationBot deployed successfully!
║
║  Network:  {cfg['label']}
║  Address:  {contract_address}
║  Tx hash:  {tx_hash.hex()}
║  Block:    {receipt['blockNumber']:,}
║  Gas used: {receipt['gasUsed']:,}
╚══════════════════════════════════════════════════════════╝
""")

    # Write to deployments.json
    log_path = ROOT / "deployments.json"
    deployments = {}
    if log_path.exists():
        with open(log_path) as f:
            deployments = json.load(f)

    key = f"{network}_{cfg['chain_id']}"
    deployments[key] = {
        "address":   contract_address,
        "tx_hash":   tx_hash.hex(),
        "block":     receipt["blockNumber"],
        "chain_id":  cfg["chain_id"],
        "pool":      cfg["pool"],
        "router":    cfg["router"],
        "deployer":  account.address,
    }
    with open(log_path, "w") as f:
        json.dump(deployments, f, indent=2)

    # Patch .env automatically
    _patch_env(contract_address)

    return contract_address


def _patch_env(address: str):
    """Write BOT_CONTRACT_ADDRESS into .env (add or replace)."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        env_path.write_text(f"BOT_CONTRACT_ADDRESS={address}\n")
        print(f"Created .env with BOT_CONTRACT_ADDRESS={address}")
        return

    lines = env_path.read_text().splitlines(keepends=True)
    replaced = False
    for i, line in enumerate(lines):
        if line.startswith("BOT_CONTRACT_ADDRESS="):
            lines[i] = f"BOT_CONTRACT_ADDRESS={address}\n"
            replaced = True
            break

    if not replaced:
        lines.append(f"BOT_CONTRACT_ADDRESS={address}\n")

    env_path.write_text("".join(lines))
    print(f"✓  .env updated → BOT_CONTRACT_ADDRESS={address}")


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Compile and deploy LiquidationBot"
    )
    parser.add_argument(
        "--network",
        choices=["testnet", "mainnet"],
        default="testnet",
        help="testnet = Arbitrum Sepolia, mainnet = Arbitrum One (default: testnet)",
    )
    parser.add_argument(
        "--recompile",
        action="store_true",
        help="Force recompile even if build cache is fresh",
    )
    args = parser.parse_args()

    if args.network == "mainnet":
        confirm = input(
            "\n⚠️  You selected MAINNET (Arbitrum One).\n"
            "   Type 'deploy mainnet' to confirm: "
        ).strip()
        if confirm != "deploy mainnet":
            print("Aborted.")
            sys.exit(0)

    # Force cache invalidation if requested
    if args.recompile:
        build = ROOT / "build" / "LiquidationBot.json"
        if build.exists():
            build.unlink()
            print("Cache cleared — recompiling…")

    abi, bytecode = compile_contract()
    deploy(args.network, abi, bytecode)


if __name__ == "__main__":
    main()
