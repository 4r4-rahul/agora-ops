# Security

This document records the security toolchain, the hardening baseline, and the accepted-risk
register. It was established on **2026-06-17** during a dedicated hardening pass.

## Toolchain (continuous)

| Layer | Tool | Where it runs | Gate |
|---|---|---|---|
| Dependency CVEs | `pip-audit` | CI + weekly cron + pre-commit-adjacent | **blocking** |
| Static code security | `bandit` (CWE) | CI + pre-commit | **blocking (HIGH)** |
| Secret/credential leak | `detect-secrets` | CI + pre-commit | **blocking** |
| Tests | `pytest` | CI | **blocking** |
| Lint | `ruff` | CI + pre-commit | **blocking** (backlog zeroed 2026-06-17) |
| Types | `mypy` | CI + pre-commit | **blocking (incremental scope)** — see `pyproject [tool.mypy].files` |
| Format | `ruff format` | manual | not gated (separate reformat pass pending) |

Install + enable locally:

```bash
pip install -r requirements-dev.txt
pre-commit install
pre-commit run --all-files       # one-off full sweep
pip-audit --progress-spinner off # ad-hoc CVE check
bandit -c pyproject.toml -r agora --severity-level high
```

CI is defined in `.github/workflows/ci.yml` (push, PR to `main`, and a weekly Monday re-audit so
newly-disclosed CVEs in already-pinned deps are caught without a code change).

## Dependency hardening — 2026-06-17

`pip-audit` initially reported **30 known CVEs across 12 packages**. After validated upgrades
(full test suite green at every step — 844 tests), this dropped to **2**, both upstream-unpatched.

Patched (validated): `requests` 2.32.5→2.34.2, `urllib3` 2.6.3→2.7.0, `idna` 3.11→3.18,
`Pygments` 2.19.2→2.20.0, `starlette` 1.0.0→1.3.1 (web-facing — 6 CVEs; allowed by
`fastapi>=0.46.0` constraint), `python-multipart` 0.0.29→0.0.32, `cryptography` 48.0.0→49.0.0,
`PyJWT` 2.12.1→2.13.0, `pytest` 9.0.2→9.1.0, `pip` 26.0.1→26.1.2. Versions are locked in
`requirements.txt`.

### Accepted-risk register (no upstream fix available as of 2026-06-17)

| Package | Advisory | Why accepted | Re-evaluate |
|---|---|---|---|
| `chromadb` 1.5.9 | CVE-2026-45829 | No patched release published; transitive via `fastmcp`/vector memory; not network-exposed in this deployment. | When a fixed release ships (CI cron will surface it). |
| `torch` 2.12.0 | CVE-2025-3000 | No patched release; transitive via `sentence-transformers`; used only for local embedding, never on untrusted model inputs. | When a fixed release ships. |

Both are `--ignore-vuln`'d **by exact ID** in CI so the gate stays green *and* honest — the moment
either upstream patches, the ignore is removed and the bump applied. Do not add blanket ignores.

### Dependency-declaration drift — RESOLVED 2026-06-17

`requirements.txt` had drifted from the actually-installed/working environment on 6 pins, which made
`pip-audit -r requirements.txt` (clean-resolve mode) fail with `ResolutionImpossible` — the pinned
`pydantic==2.9.2` could not satisfy `fastmcp>=3.3.1`, which requires the newer pydantic the venv was
already running. Reconciled the declarations to the validated installed versions (suite green on
them): `pydantic` 2.9.2→2.13.4, `pydantic_core` 2.23.4→2.46.4, `curl_cffi` 0.13.0→0.15.0,
`protobuf` 7.34.0→6.33.6, `python-dotenv` 1.0.1→1.2.2, `yfinance` 1.2.0→1.3.0. `pip install
--dry-run -r requirements.txt` and `pip-audit -r requirements.txt` now both resolve cleanly.

## Code hardening — 2026-06-17

- **6× B324 (weak MD5, HIGH)** → resolved. All six were content-dedup hashes (`file_num`/`text`/
  `cluster_key` → a "seen" key), never security/crypto. Marked `usedforsecurity=False` — output is
  byte-identical (verified), so behavior is unchanged and the finding is correctly cleared.
- **38× B608 (SQL f-string, MEDIUM)** → reviewed, not injectable. Every flagged query interpolates a
  **hardcoded column-list literal** with all runtime values bound via `?` placeholders. Excluded
  from the gate (`pyproject.toml [tool.bandit] skips`) with this rationale; re-audit if any query
  begins interpolating a non-constant.
- **B101/B110/B112 (asserts, try/except pass)** → intentional invariants and fail-safe I/O guards;
  excluded from the gate.

Result: **0 HIGH bandit findings** in `agora/`.

## Secrets

- `.env` is git-ignored and **not tracked** (only `.env.example`). No live secrets in tracked files.
- `detect-secrets` baseline (`.secrets.baseline`) allowlists 4 obvious **test placeholders**
  (`sk-test`, `sk-fake`, `backtest-mock`, `sk-ant-test-key`). Any new real-looking secret fails CI.
- **History note:** a prior config dump printed the Anthropic API key + Discord bot token/webhook to
  the terminal/logs; those credentials were rotated. The `detect-secrets` + `detect-private-key`
  gates now guard against re-introduction.

## Reporting

This is a private project. Report security concerns directly to the maintainer.
