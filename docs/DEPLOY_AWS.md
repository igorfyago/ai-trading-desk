# AWS deployment — the desk, live, 24/7

Goal: the **real options-flow-analytics pipeline** (Rust collector → PostgreSQL → Node dashboard) computing dealer positioning around the clock on real delayed market data, and the **AI agents** (this repo) answering questions and taking voice calls against that same live database. Two public URLs: `gex.<domain>` (dashboard) and `desk.<domain>` (agents).

The plan is staged so every phase is a working system — and each phase adds a distinct set of AWS competencies.

## Phase 1 — one EC2, Docker Compose (~$20/mo, ship first)

The whole stack on a single instance with [deploy/docker-compose.aws.yml](../deploy/docker-compose.aws.yml): postgres, collector, gex-api, desk, and Caddy terminating TLS (WebRTC mic access requires HTTPS).

| Step | Service | What you do |
|---|---|---|
| Account hygiene | **IAM, Budgets** | create an admin IAM user (no root usage), enable MFA, set a $30 budget alarm |
| Instance | **EC2** | t3.small (2GB) Ubuntu 24.04, 20GB gp3; security group: 22 (your IP only), 80, 443 |
| Static IP | **Elastic IP** | allocate + associate, so DNS survives restarts |
| DNS | **Route 53** | hosted zone; A-records `desk.<domain>` and `gex.<domain>` → the EIP |
| Secrets | **SSM Parameter Store** | `OPENAI_API_KEY`, `LANGSMITH_API_KEY`, `POSTGRES_PASSWORD` as SecureStrings; instance role with `ssm:GetParameter` — no secrets in files or user-data |
| Logs & metrics | **CloudWatch** | CloudWatch agent shipping docker logs; alarm on CPU > 80% and disk > 85% |
| Backups | **S3** | nightly `pg_dump` cron to a versioned bucket with 30-day lifecycle |

```bash
# on the instance
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git awscli
mkdir ~/apps && cd ~/apps
git clone https://github.com/igorfyago/options-flow-analytics
git clone https://github.com/igorfyago/ai-trading-desk
cd ai-trading-desk/deploy
# pull secrets from SSM into .env (script this)
for p in OPENAI_API_KEY LANGSMITH_API_KEY POSTGRES_PASSWORD; do
  echo "$p=$(aws ssm get-parameter --name /desk/$p --with-decryption --query Parameter.Value --output text)" >> .env
done
echo "BASE_DOMAIN=<your-domain>" >> .env
docker compose -f docker-compose.aws.yml up -d --build
```

Done: collector snapshots SPY/QQQ every 30s from CBOE's free delayed feed, agents answer at `https://desk.<domain>`, dashboard at `https://gex.<domain>`.

## Phase 2 — Kubernetes on the same box (k3s, +$0)

Real Kubernetes running the same workloads, without paying for EKS control plane:

```bash
curl -sfL https://get.k3s.io | sh -                      # k3s: certified k8s, single binary
kubectl apply -f ~/apps/options-flow-analytics/k8s/      # namespace, postgres StatefulSet, collector (Recreate), api
kubectl apply -f ~/apps/ai-trading-desk/k8s/             # namespace, desk Deployment (2 replicas), service, ingress
kubectl -n ai-trading-desk create secret generic desk-secrets --from-literal=OPENAI_API_KEY=...
```

Both repos' manifests are kubeconform-validated. Talking points this phase earns: Deployments vs StatefulSets, single-writer `Recreate` strategy for the collector, readiness/liveness probes, resource requests/limits, Secrets, Ingress + cert-manager, horizontal scaling of the stateless web tier.

## Phase 3 — managed containers (the "at scale" answer)

When asked "and beyond one box?":

- **ECR** — push both images (`docker push <acct>.dkr.ecr.<region>.amazonaws.com/...`), lifecycle policy keeping last 10
- **ECS on Fargate** — task definitions per service, desk service ×2 tasks, collector ×1; secrets injected from SSM; logs to CloudWatch
- **ALB + ACM** — target groups for desk/gex-api, free public TLS certs, health checks on `/agents` and `/healthz`
- **RDS Postgres** — replaces the container; automated backups, minor-version patching
- or **EKS** — the same `k8s/` manifests apply nearly unchanged; that's the point of writing them first

CI/CD: a GitHub Actions job building images on push to `main`, pushing to ECR, then `aws ecs update-service --force-new-deployment` (OIDC role, no long-lived keys).

## Cost guardrails

t3.small + EIP + Route53 + S3 ≈ **$20–25/mo**. Fargate/RDS/EKS raise that to $50–150 — do Phase 3 as a documented dry-run, tear down after screenshots unless traffic justifies it. Budget alarm at $30 catches surprises. Realtime voice costs scale with recruiter usage: keep the OpenAI project spending cap + per-session rate limit.

## Security posture

No root account usage; instance role scoped to `ssm:GetParameter` on `/desk/*` + CloudWatch write; SSH restricted to your IP (or SSM Session Manager instead of SSH at all); secrets only in SSM SecureStrings; Postgres never exposed publicly (compose binds it to the docker network, k8s keeps it ClusterIP); the desk app's own guardrails (read-only SQL, ephemeral voice tokens) documented in the [main README](../README.md).
