# VaultScan — Solana Vault/PDA Lockup Detection

**Status:** Pilot phase — no code written yet
**Paper target:** FC27 (Financial Cryptography 2027), working with Arthur Gervais (UCL) + Isaac David

---

## 1. Core Claim

Solana's mandatory rent-exemption model turns a rare, incidental EVM failure category
("locked or frozen asset" — already named in Gervais's own SoK, ~1% of 181 documented
incidents) into a **structural, recurring risk**. Every PDA/vault creation requires a
mandatory rent-exempt deposit, so the precondition for lockup exists on every transaction,
not just when something goes wrong.

**Irreversibility hinges on one concrete, checkable condition:** whether the program's
upgrade authority has been renounced. Authority renouncement is common practice on Solana
specifically to prove a protocol can't rug users — so the exact practice that builds trust
is what can convert a fixable oversight into permanent, unrecoverable loss.

### What makes a case a genuine positive (per Arthur's Jul feedback — do not skip this)
A case only counts if **all** of the following hold:
1. Value (lamports beyond rent-exempt minimum) is stranded in a vault/PDA
2. No reachable recovery path exists — not just "no `close` instruction," but also no
   generic reclaim/admin/sweep instruction that could move it out
3. No possible program upgrade — i.e. upgrade authority is `None` (renounced), not just
   "currently no close instruction but authority is still live" (that's a *different*,
   weaker finding — fixable, not permanent)

Do not conflate "unclosed PDA + renounced authority" alone with a verified positive.
Manual verification is required before counting anything as a real case.

---

## 2. What This Is NOT (keep scope tight)

- **Not** a general Slither-for-Solana static analyzer — too broad, competes directly
  with VRust/WACANA/Sec3 X-Ray/SseRex on their own turf
- **Not** the original 6-adversarial-hack taxonomy plan (Wormhole/Mango/Cashio/Crema/
  Nirvana/Solend) — that thread was dropped, Arthur found it insufficiently novel
- **Not** live intrusion detection (that's WatchTower/Hypernative/Range/Riverguard's
  territory — commercial, real-time, different research task)
- **Not** exploit generation (that's A1) or attack postmortem (that's TxRay, EVM-only,
  not yet accepted anywhere as of writing)
- **Not** phishing detection (SolPhishHunter) or rug-pull detection (SolRugDetector) —
  both already exist academically for Solana; don't claim "no academic Solana detection
  work exists" — the real gap is narrower: nobody studies irreversible account-lifecycle
  loss in immutable programs specifically

---

## 3. Related Work (confirmed, correctly attributed)

| Paper/Tool | Authors | What it actually does | Relevance |
|---|---|---|---|
| SoK: Decentralized Finance (DeFi) Attacks | Zhou, Xiong, Ernstberger, Chaliasos, Wang, Wang, Qin, Wattenhofer, Song, Gervais (IEEE S&P 2023) | Names "locked or frozen asset" as an existing incident category (11 papers, 2 audit categories, ~1% of incidents) | Our claim must engage this directly — not present lockup as a brand-new category, only as structurally amplified on Solana |
| A1: AI Agent Smart Contract Exploit Generation | Gervais, Zhou (FC 2026, arXiv 2507.05558) | Agentic LLM system generates exploits given contract address; 62.96% success on VERITE benchmark | Required related work per Arthur. EVM/BSC only — offense-oriented, different task from ours |
| TxRay: Agentic Postmortem of Live Blockchain Attacks | Wang, Yu, Qin, Song, Gervais, Zhou (arXiv 2602.01317) | Agentic reconstruction of exploit lifecycle + PoC generation from a seed tx hash | **Not yet accepted anywhere** — cite as preprint only. EVM-only, postmortem task, different from ours |
| VRust | Cui, Zhao, Gao, Tavu, Huang (CCS 2022) | Static analysis on Rust MIR for Solana; found 3 critical bugs in official Solana Programming Library | Adversarial-exploit-oriented, source-required, high false-positive rate reported in follow-ups |
| FuzzDelSol | Smolka, Giesen, Winkler, Draissi, Davi, Karame, Pohl (CCS 2023) | Coverage-guided eBPF fuzzing on Solana binaries | Doesn't handle Anchor-based contracts (~43% of deployed programs) |
| WACANA | Wang et al. (2024) | Symbolic execution on WASM/bytecode | Academic prototype, no maintenance |
| Sec3 X-Ray (formerly Soteria) | Sec3 | Static analysis, 50+ vuln types | Commercial, no longer free/open |
| SseRex | (2026) | Symbolic execution, first to properly support Anchor | Brand new, still adversarial-exploit-oriented, not lockup-focused |
| SolPhishHunter | Li et al. (arXiv 2505.04094) | Detects 3 types of Solana phishing tx, 93.96% precision, released SolPhishDataset | Academic, Solana-specific — corrects any claim of "no academic Solana detection work" |
| SolRugDetector | (arXiv 2603.24625) | Detects rug-pull tokens from on-chain tx/state data, 117 confirmed cases | Same correction as above — rug-pull ≠ our failure mode |
| OptiFi incident | — | $661K permanently locked, Aug 2022, program accidentally closed via `solana program close` | **Motivation only, not evidence.** Accidental closure ≠ voluntary authority renouncement — different mechanism, don't conflate |
| Deployment buffer lockups | solana-labs/solana GitHub #35531 | Hardware-wallet signing constraint strands rent in buffer accounts | Real, recurring, multiple developers affected — supports "structural, not rare" framing but is a different sub-mechanism (deployment infra, not user-facing vaults) |
| WatchTower / Hypernative / Range / Riverguard | Sec3 / respective vendors | Commercial real-time monitoring/alerting | Adjacent gap (no open-methodology academic equivalent), not our task |

---

## 4. Detection Pipeline

### Stage 1 — IDL Parsing (static)
- Input: Anchor IDL (JSON), fetched via `anchor idl fetch <program_id> --provider.cluster mainnet` or pulled from published source repo
- Extract every account type created via `init` / `init_if_needed` constraints → vault/PDA candidates
- Extract every instruction with a `close = <target>` constraint → build "closeable" account type set
- **New, per Arthur's feedback:** also scan for generic reclaim/admin/sweep instructions that
  aren't named `close` but could still recover funds (e.g. `withdraw_all`, `admin_sweep`,
  `emergency_withdraw`) — false positives here are the most likely failure mode of the tool
- Output: list of account types with `has_close_path: true/false/ambiguous`

### Stage 2 — Authority Check (on-chain)
- `solana program show <PROGRAM_ID> -u mainnet-beta` (CLI) or direct RPC query of the
  `ProgramData` account (BPFLoaderUpgradeable), decode `upgrade_authority_address`
- `None` = renounced/immutable, `Some(pubkey)` = still upgradeable

### Stage 3 — Cross-Reference (the actual heuristic)
- Candidate flag = `has_close_path: false` AND `authority: renounced`
- `ambiguous` cases go to manual review, not auto-counted either way
- This two-signal + manual-review combination is the paper's core contribution

### Stage 4 — Ecosystem Sweep (scale/severity)
- For flagged account types: `getProgramAccounts` with memcmp filter on account discriminator
  (first 8 bytes) to enumerate all on-chain instances
- Sum lamport balances across instances → convert to SOL/$ 
- **New, per Arthur's feedback:** track balance/instance count **over time** (multiple
  snapshots), not just a single point-in-time read — growth trend is part of what he
  explicitly asked to see in the pilot table

---

## 5. Pilot Candidate List

### Bucket 1 — High-TVL / likely audited (real program IDs, verified)
| Protocol | Program | Program ID | Source |
|---|---|---|---|
| Raydium | AMM v4 | `675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8` | public |
| Raydium | CPMM | `CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C` | public |
| Raydium | CLMM | `CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK` | public |
| Jupiter | (pull from jup.ag docs — same pattern as Raydium) | TBD | TBD |
| Orca | Whirlpools (pull from orca.so docs) | TBD | TBD |
| Drift | (pull from drift.trade docs) | TBD | TBD |
| Kamino | Lend (pull from kamino docs) | TBD | TBD |
| MarginFi | (pull from marginfi docs) | TBD | TBD |
| Marinade | (pull from marinade docs) | TBD | TBD |
| Jito / Sanctum / Meteora | (same pattern) | TBD | TBD |

**Bulk IDL source:** `allenhark.com/solana-idl-library` — 70 IDLs, 32+ protocols, free JSON downloads.
**Verification method:** each protocol's own docs site has a "program addresses" reference page
(same structure as Raydium's) — pull from there, not from Solscan search.

### Bucket 2 — Open-source escrow/vault repos (for testing parser logic, mostly devnet)
- `ironaddicteddog/anchor-escrow`
- `solanakite/anchor-escrow-2025`
- `solanakite/anchor-escrow-2026`
- `kobby-pentangeli/solana-escrow`
- `ghabxph/escrow-anchor`
- `solana-foundation/program-examples`
- `quicknode/solana-program-examples`

**Caveat:** mostly devnet/tutorial deployments — useful for validating Stage 1/2 parsing logic,
not for real severity numbers.

### Bucket 3 — Long-tail / abandoned mainnet programs (the bucket that matters most, least sourced)
**No reliable search shortcut found.** Method:
1. Solscan verified-program directory, sort by oldest deploy date + low recent activity
2. Cross-reference candidate names against Solana Foundation hackathon archives
   (Colosseum, Grizzlython, Riptide winner lists) for plausible abandoned projects
3. Manually confirm each candidate's authority status via `solana program show`

---

## 6. Build Order

1. Tighten detection definition (Section 1 — done, encoded above)
2. Finalize candidate list — fill in Bucket 1 TBDs, source Bucket 3 manually (in progress)
3. Build IDL fetch step (Stage 1 input)
4. Build Stage 1 parser (account/close-instruction extraction + reclaim-pattern heuristic)
5. Build Stage 2 authority check (CLI wrapper or direct RPC)
6. Build Stage 3 cross-reference logic (tightened definition, ambiguous-case bucket)
7. **Manually verify every flagged candidate** — this is what makes an example "fully
   verified" per Arthur's bar, not optional
8. Build Stage 4 sweep (getProgramAccounts + balance summation)
9. Add time-series tracking for Stage 4 (balance/instance growth over time)
10. Compile pilot table: selection criteria, program name, account count, locked value,
    growth over time, authority state, close-path evidence, false-positive check notes
11. Get to 2-3 **fully verified** examples (OptiFi does not count — motivation only)
12. Send pilot table + scanner methodology + verified examples to Arthur

---

## 7. Output Format (per flagged case)

```
program_name, program_id, account_type, creating_instruction,
close_path_status (none/ambiguous/present), authority_status (renounced/live),
instance_count, total_lamports_at_risk, balance_snapshot_dates[], 
manual_verification_notes
```

---

## 8. Known Limitations (state upfront in the paper — reviewers will ask)

| Risk | Mitigation |
|---|---|
| False positive: account closed via non-standard-named instruction the parser misses | Manual verification pass on every flagged candidate before counting it |
| False positive: protocol intentionally keeps accounts open (e.g. historical records) | Cross-check balance vs. rent-exemption minimum + recent write activity |
| Closed-source programs (no IDL) | Out of scope for v1 — bytecode discriminator reconstruction is future work |
| Single-snapshot bias | Stage 4 now tracks balance over multiple time points, not one read |

---

## 9. Open Questions / Not Yet Resolved

- [ ] Bucket 1: fill in program IDs for Jupiter, Orca, Drift, Kamino, MarginFi, Marinade,
      Jito, Sanctum, Meteora (same doc-page pattern as Raydium)
- [ ] Bucket 3: no sourcing shortcut found yet — needs manual Solscan work
- [ ] Reply owed to Arthur's open channel question on Solana intrusion detection (answered
      informally in Slack — commercial tools exist, no academic open-methodology equivalent,
      but distinct from our task)
- [ ] Tool name "VaultScan" is a placeholder, not finalized
- [ ] No code written yet — this document is the starting point for that
