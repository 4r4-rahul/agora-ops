# Setup Requirements

This document captures every external dependency, credential, and infrastructure element required to deploy the options agent as a live, latency-aware trading stack.

## Summary Matrix

| Service Domain | Primary Purpose | Recommended Providers (Free / Paid) | Est. Monthly Cost (USD) | Time-Efficiency Score* |
| --- | --- | --- | --- | --- |
| Real-Time Options Chain & Greeks | Signal generation, risk models | Polygon.io (free delayed / paid real-time), Interactive Brokers Market Data (paid) | 0 – 249 | 5 (IBKR), 3 (Polygon) |
| Historical + Reference Data | Backtesting, audit, analytics | Alpha Vantage (free), Quandl/Nasdaq Data Link (paid) | 0 – 100 | 3 |
| Broker / Execution API | Live order placement, fills, margin events | Interactive Brokers (paid), Tradier (paid), Tastytrade (paid) | 0 – 150 (market data + routing fees) | 5 (IBKR), 4 (Tradier) |
| Secure Secrets & Config | API keys, auth tokens | AWS Secrets Manager, HashiCorp Vault (enterprise) | 0.40 per secret / month | 4 |
| Persistence Layer | Plans, fills, audit logs, training corpus | AWS RDS (Postgres/Timescale), Redis Enterprise | 50 – 600 depending on throughput | 4 |
| Stream Processing / Event Bus | Real-time telemetry, retraining queue | Redpanda Cloud, Confluent Kafka, AWS MSK | 200+ | 4 |
| Observability | Metrics, tracing, alerting | Datadog, Grafana Cloud (free tier) | 0 – 360 | 4 |
| Compute + Hosting | FastAPI services, schedulers, workers | AWS ECS/Fargate, Kubernetes (EKS), Fly.io (prototype) | 30 – 1500 | 4 |
| GPU / LLM Infra (optional) | Local explainability/retraining | AWS g5.xlarge (NVIDIA A10G), Lambda Labs A100 rentals | 400 – 2500 | 2 |
| Compliance & Secure Networking | VPN, IP whitelisting, SOC logging | Tailscale, AWS VPC + Transit Gateway | 12 – 200 | 4 |

\*Time-Efficiency Score: 1 (slow/manual) → 5 (market-ready, sub-second latencies).

---

## 1. Market Data Providers

### Interactive Brokers (paid, required for production)
- **Why**: Native partnership with execution venue; greeks, IV, order book, best market depth. Enables deterministic latency between signal and trade.
- **Use**: `/market/options-chain` & `/market/greeks` endpoints pull real-time quotes; planning engine consumes cached greeks.
- **Cost**: Exchange-specific (e.g., OPRA $1.50 + exchange fees). Expect ~$120/month for US options with non-professional status.
- **Setup Path**:
  1. Fund an IBKR account and request *Live Market Data* subscriptions (OPRA, CBOE, ISE, etc.).
  2. Install TWS or IB Gateway on a hardened host; enable API connection, trusted IPs, and auto-restart scripts.
  3. Create an API-only user, set `IBKR_HOST/PORT/CLIENT_ID` in `.env` or Secrets Manager.
  4. Enforce heartbeat monitors; rotate client IDs per service to avoid pacing violations.
- **Auth / Rate Limits**: Socket-based; IB enforces pacing (max 50 requests / sec). Use `ib_insync` throttling.
- **Time-Efficiency**: 5 — direct feed from execution venue.

### Polygon.io (hybrid free/paid, augmentation)
- **Why**: Simple REST/WebSocket; fallback when IBKR session unavailable; historical snapshots for audit.
- **Use**: Secondary provider inside `data_providers/` to warm caches, feed analytics dashboards.
- **Cost**: Free (15-min delayed) → $249/mo (real-time options). Includes 4M API calls.
- **Setup Path**: Create org, generate API key, store as `POLYGON_API_KEY`; implement WebSocket streaming for `options_previous_close` & `snapshot/options/{symbol}`.
- **Time-Efficiency**: 3 — REST introduces ~100-200 ms latency.

### Human Infrastructure Needed
- Dedicated machine or VM to host IBKR Gateway with auto-login script (requires manual 2FA device).
- Compliance approval for data usage; record subscriber status (pro vs non-pro).

## 2. Broker & Execution

### Interactive Brokers
- **Why**: Already paired with market data; robust order types, low commissions (~$0.65/contract US options).
- **Use**: Replace `/options/place` mock with real FIX or TWS API calls, order status streaming, margin monitoring.
- **Prereqs**: Funded account, risk limits configured, real-time market data rights. Production requires separate credentials per environment (paper/live).
- **Auth**: TWS socket + client ID, or IB Gateway + FIX sessions (if approved). Recommend dedicated API user.
- **Latency**: 30-80 ms from co-located AWS us-east-1 to IB endpoints.

### Alternative Brokers
| Broker | Pros | Cons | Est. Cost | Time Score |
| --- | --- | --- | --- | --- |
| Tradier | REST API, easy onboarding, paper trading | Less depth, limited option leg support | $85 data bundle | 4 |
| Tastytrade | Broker-native Greeks, fast approvals | API in beta; limited docs | $0 data but platform fees | 3 |
| TD/Schwab API | Retail coverage | Rate-limited, complex OAuth | $0 | 2 |

### Human Infrastructure Needed
- Compliance officer to sign electronic trading agreements.
- Risk desk contact for kill-switch procedures.
- Banking rails for capital contributions and daily PnL sweeps.

## 3. Persistence & State

1. **Postgres/TimescaleDB (AWS RDS or Azure Database)**
   - Stores plans, greek snapshots, executions, audit trails, reinforcement signals.
   - `db.t3.large` multi-AZ (~$240/mo) recommended for 30-day retention.
   - Setup: Terraform RDS, enable encryption at rest, IAM auth, rotate credentials via Secrets Manager.

2. **Redis (ElastiCache / Upstash)**
   - Low-latency cache for scores, active orders, throttling tokens.
   - ~$75/mo for production-ready cluster.

3. **Object Storage (Amazon S3, GCS)**
   - Persist daily JSONL audit logs, LLM transcripts.
   - Lifecycle policies to Glacier for compliance.

## 4. Streaming & Job Routing

- **Kafka / Redpanda**: Topic per stage (scores, plans, executions, audit-ingest, retraining). Redpanda Cloud (~$200/mo at 10 MB/s) offers lowest operational overhead.
- **AWS EventBridge / SQS**: Lightweight alternative for scheduled audits and plan refresh tasks.
- **Setup**: Configure topic-level ACLs, schema registry for OptionPlan/Execution events, and consumer lag monitoring.

## 5. Observability & Security

- **Metrics & Tracing**: Datadog APM or Grafana Cloud; scrape FastAPI/Uvicorn metrics (latency, 5xx). Budget ~$23/host/mo for Datadog.
- **Logging**: Ship structured logs to OpenSearch or Loki, enforce retention and PII scrubbing.
- **Secrets**: AWS Secrets Manager (0.40/secret/month) storing `IBKR_*`, `POLYGON_API_KEY`, `OPENAI_API_KEY`.
- **Network**: Private subnets, NAT gateway, VPC endpoints for AWS APIs. Consider Tailscale mesh for developer access and IB Gateway bridging.

## 6. Compute & Deployment

- **FastAPI Backend**: Containerize (Dockerfile already present). Deploy via AWS ECS/Fargate (2 vCPU/4 GB ≈ $55/mo) or Kubernetes (EKS) for multi-service layout.
- **Frontend**: Host static bundle on CloudFront/S3 or Vercel. Add auth (Cognito/Auth0) before exposing publicly.
- **Workers**: Async service for scheduled audits, backtesting, and reinforcement signals. Use AWS Batch or ECS scheduled tasks.
- **CI/CD**: GitHub Actions with OIDC to AWS; run pytest + lint + container scans.

## 7. LLM / Explainability

- **OpenAI** (current) or self-hosted models (Llama 3.1, Mistral). For on-prem, provision GPU nodes (g5.xlarge ≈ $1.21/hr) and run vLLM/Inference server. Maintain inference queue via Redis.

## 8. Compliance & Governance

- FINRA/SEC requirements: maintain tamper-proof logs (WORM storage), daily EOD reports, supervision vs automation policy.
- Enable human approval workflow for large trades; integrate Slack/MS Teams alerts for overrides.

## 9. Setup Checklist

1. **Accounts & Credentials**
   - Open funded IBKR account, enable API + market data.
   - Register Polygon.io & AlphaVantage keys (store in secrets manager).
   - Provision OpenAI key or self-host LLM endpoint.
2. **Infrastructure**
   - Deploy RDS + Redis + S3 buckets.
   - Stand up Kafka/Redpanda cluster (or EventBridge bus).
   - Container registry (ECR/GHCR) and CI pipelines.
3. **Security**
   - Configure IAM roles, security groups, and secret rotation.
   - Set up VPN/Tailscale for IB Gateway host.
4. **Monitoring**
   - Instrument FastAPI with Prometheus exporters.
   - Configure Datadog alerts for API errors >1%, latency >200 ms.
5. **Operational Runbooks**
   - Document TWS restart automation, failover steps, and kill-switch command.
   - Define human escalation paths.

## 10. Time-Sensitive Infrastructure Ranking

1. **IBKR Market Data + Broker Combo** (Score 5): Direct venue connectivity; sub-100 ms path.
2. **Kafka/Redpanda Streaming** (Score 4.5): Ensures deterministic event flow and enables multi-agent orchestration.
3. **Redis Cache** (Score 4.5): Microsecond lookups for scores/greeks; prevents API thrash.
4. **Secrets Manager + CI/CD** (Score 4): Enables rapid, safe deployments.
5. **Datadog/Grafana Observability** (Score 4): Necessary to maintain SLA and catch latency regressions in real time.

> **Key Human Inputs**: Funded brokerage accounts, hardware tokens for IBKR, corporate compliance approvals, AWS/GCP organization ownership, SSL certificates, and SOC logging acceptance.

---

By fulfilling the above requirements, the project transitions from a deterministic simulator to a production-grade, latency-aware agent capable of real money deployment.
