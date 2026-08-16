# Aave V3 Liquidation Bot — Deployment Guide

## Prerequisites

- Ubuntu 22.04 server (1 CPU, 2 GB RAM minimum)
- Python 3.12
- An Alchemy account (for Arbitrum WebSocket + HTTP RPC)
- A dedicated Ethereum wallet (hot wallet, gas only — 0.01 ETH)
- A Telegram bot token (optional but strongly recommended)

---

## 1. Local Setup

```bash
git clone <your-repo> aave-liquidation-bot
cd aave-liquidation-bot

python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Copy and fill in your environment:
```bash
cp .env.example .env
# Edit .env with your keys
```

---

## 2. Compile the Smart Contract

Install brownie in a separate environment:
```bash
pip install eth-brownie
brownie compile
```

This produces `build/contracts/LiquidationBot.json`.

---

## 3. Deploy to Testnet (Arbitrum Sepolia)

Set `CHAIN_ID=421614` in `.env`, then:

```bash
# Using the standalone deploy script:
python scripts/deploy.py --network testnet

# Or via brownie:
brownie run scripts/deploy.py --network arbitrum-sepolia
```

Copy the deployed address into your `.env`:
```
BOT_CONTRACT_ADDRESS=0x...
```

---

## 4. Test on Testnet

Run the bot against testnet Aave:
```bash
python main.py
```

Monitor the Telegram alerts and `logs/bot.log`.

To manually trigger a test liquidation, you can use Aave's testnet frontend to
create an undercollateralised position, then verify the bot detects and executes
it.

---

## 5. Deploy to Mainnet

Once testnet works perfectly:

1. Switch `.env` to mainnet values:
   ```
   CHAIN_ID=42161
   ALCHEMY_WS_URL=wss://arb-mainnet.g.alchemy.com/v2/YOUR_KEY
   ALCHEMY_HTTP_URL=https://arb-mainnet.g.alchemy.com/v2/YOUR_KEY
   ```

2. Deploy the contract:
   ```bash
   python scripts/deploy.py --network mainnet
   # Update BOT_CONTRACT_ADDRESS in .env
   ```

3. Fund your wallet with 0.01 ETH on Arbitrum (for gas).

4. Verify the contract on Arbiscan (optional but good practice):
   ```bash
   brownie run scripts/verify.py --network arbitrum-main
   ```

---

## 6. Deploy as systemd Service (Ubuntu Server)

```bash
# Copy project to server
scp -r ./aave-liquidation-bot ubuntu@YOUR_SERVER_IP:/home/ubuntu/

# SSH in
ssh ubuntu@YOUR_SERVER_IP

cd /home/ubuntu/aave-liquidation-bot
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Create data and log directories
mkdir -p data logs

# Install systemd service
sudo cp liquidation-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable liquidation-bot
sudo systemctl start liquidation-bot
```

Check status:
```bash
sudo systemctl status liquidation-bot
journalctl -u liquidation-bot -f
```

---

## 7. Monitoring

### Telegram
The bot will send you:
- Notification on startup/shutdown
- Every successful liquidation with profit
- Every failed execution with reason
- Daily P&L summary at 00:00 UTC
- Alert if no blocks received for 5 minutes

### Logs
```bash
tail -f /home/ubuntu/aave-liquidation-bot/logs/bot.log
```

### Database queries
```bash
sqlite3 data/bot.db "SELECT * FROM executions ORDER BY timestamp DESC LIMIT 10;"
sqlite3 data/bot.db "SELECT SUM(profit_usd), SUM(gas_cost_usd) FROM executions;"
```

---

## 8. Withdraw Profits

Profits accumulate in the deployed contract as the debt asset (e.g., USDC, WETH).
To withdraw:

```python
# Quick withdrawal script
from web3 import Web3
import os, json
from dotenv import load_dotenv
load_dotenv()

w3 = Web3(Web3.HTTPProvider(os.environ["ALCHEMY_HTTP_URL"]))
with open("abis/liquidation_bot.json") as f:
    abi = json.load(f)

contract = w3.eth.contract(
    address=os.environ["BOT_CONTRACT_ADDRESS"],
    abi=abi
)
account = w3.eth.account.from_key(os.environ["PRIVATE_KEY"])

# Withdraw all USDC
usdc = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
tx = contract.functions.withdrawAll(usdc).build_transaction({
    "from": account.address,
    "nonce": w3.eth.get_transaction_count(account.address),
    "gasPrice": w3.eth.gas_price,
})
signed = account.sign_transaction(tx)
w3.eth.send_raw_transaction(signed.rawTransaction)
```

---

## Architecture Notes

- **Zero capital risk**: All liquidations funded by Aave flash loans. Only gas
  costs require upfront ETH (~0.01 ETH covers months of operation).
- **No mempool sniping**: Bot submits at 110% of current gas price — enough to
  be competitive without overpaying on Arbitrum's cheap fee market.
- **Atomic execution**: The entire liquidation, swap, and repayment happens in
  a single transaction. If profit check fails, the tx reverts and you lose only
  the gas (~$0.01 on Arbitrum).
- **SQLite**: Chosen for simplicity. For high-volume production, migrate to
  PostgreSQL and increase the position refresh parallelism.

---

## Security Checklist

- [ ] Private key stored only in `.env`, never in code or git
- [ ] `.env` has `chmod 600` permissions on server
- [ ] Bot wallet holds gas ETH only — no large balances
- [ ] `onlyOwner` modifier on all contract entry points
- [ ] Minimum profit check on-chain prevents sandwich attacks
- [ ] systemd `PrivateTmp` and `ProtectSystem` enabled
- [ ] Telegram alerts for all failures
