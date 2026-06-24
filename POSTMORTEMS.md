# Postmortems — Alpha Radar

## 1. Secrets must stay out of the repo
Symptom: a ClickHouse password and a keystore password were present as a default arg and in a usage comment in working copies.
Fix: passwords come from CLI args / env only; `config.yaml`, `keystores/`, `.env` are git-ignored; `config.example.yaml` ships placeholders. Verify with a grep for long hex / password literals before every push.

## 2. Eligible-token allowlist must match the competition list exactly
Symptom: trades outside the competition's fixed BEP-20 list do not count toward scoring.
Fix: `allowed_tokens` in config mirrors the official eligible list. Tokens with no liquid BSC contract are dropped (they cannot be traded on PancakeSwap anyway).

## 3. Execution is direct, not via TWAK
Symptom: confusion over whether swaps must route through Trust Wallet Agent Kit.
Fix: Track 1 main scoring is pure PnL and PancakeSwap is an accepted venue, so the default path is direct PancakeSwap V2. The TWAK path exists but is off by default and is only relevant to the separate Trust Wallet special prize, which requires a TWAK-created wallet.
