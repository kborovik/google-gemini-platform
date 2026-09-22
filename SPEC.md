# SPEC — Bank Credit Policy Agent Demo (Gemini)

## §G GOAL

Demo Gemini prompt (1) answers credit-policy questions from 12 Contoso Demo Bank Markdown policies in one RAG corpus w/ citations (2) evaluates local client applications (`accepted`|`rejected`|`missing-data`) in the terminal by `application_id` or `customer_name`, judgement grounded in named policy docs — not a production origination system. Same use case as kborovik/azure-ai-foundry. Document generation reused: facts.yaml + Jinja policies, Gemini JSON filings from the same application prompts.

## §C CONSTRAINTS

- demo: no origination system-of-record; no real borrower PII; no real bank IP; no production credit decisioning
- thin stack: Gemini generateContent + RAG Engine retrieval tool; terminal client; Google Chat = not v1
- Markdown-only corpus v1; no PDF
- no multi-agent, no write-back to core banking
- public Gemini API via Application Default Credentials; no API keys committed
- one RAG corpus imports both prefixes; model synthesizes; no separate answer-synthesis service
- CPython 3.14; `requires-python = ">=3.14"`; `[tool.uv] python-preference = "managed"`; no PEP 723 under `src/`
- one CLI `talos` (generate, deploy, chat); unit via `gmake test` → `uv run pytest`
- workload project `lab5-gemini-dev1`; region `us-east1`; state bucket `terraform-lab5-gemini-dev1` owned by gcp-lab5-org
- chat model `gemini-3.5-flash`; embedding `text-embedding-005`
- no Application Integration / Dialogflow in v1 Terraform
- default CI = unit corpus/CLI tests; live Google Cloud behind pytest markers

## §I INTERFACES

- cmd: `uv run talos generate` Click group; bare → help exit 2. `generate policy` renders facts+templates, writes `data/credit-policies/`, optional GCS upload; `--local-only` / `--gcs-only` mutex; `--dry-run` / `--force` / `--fail-if-missing-gcs` / `--no-terraform`. `generate application` calls Gemini JSON mode; `--type` or `--all`; `--count`; `--force --application-id`; `--local-only`; `--dry-run` prints serials and does not call the model. `talos deploy` syncs both prefixes (hash-skip), ensures RAG corpus `kb-credit-policies`, imports both URIs, `--wait` polls the import and file counts. `talos chat` one-shot, stdin, or TTY REPL. `talos --completion bash|zsh|fish|powershell` prints Click source. Missing required env → exit 2.
- env: `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, `GCS_BUCKET`, `GCS_URI`. Flags > process env > `infra/outputs.json` (unwrap `.value`). No `.env` load.
- names: project `lab5-gemini-dev1`; region `us-east1`; bucket `lab5-gemini-dev1-credit-docs`; tfstate bucket `terraform-lab5-gemini-dev1` prefix `google-gemini-platform`; corpus display name `kb-credit-policies`; prefixes `credit-policies` and `client-applications`; model `gemini-3.5-flash`; embedding `text-embedding-005`
- file: `corpus/facts.yaml`; `corpus/templates/*.md.j2`; committed `data/credit-policies/*.md` + `manifest.json`; gitignored `data/client-applications/*.md` + `manifest.json`; `agents/credit-policy-agent.instructions.md`; `tests/fixtures/golden_queries.yaml` (18 ids); `tests/fixtures/client-applications/`
- infra: `infra/*.tf`; `infra/lab5-gemini-dev1.tfvars`; variable blocks = `project`, `region` only; GCS backend on the org-factory bucket; `infra-init` checks that bucket; `gmake infra-create` apply then `terraform output -json` → `infra/outputs.json`; `infra-destroy` does not delete the state bucket
- pytest: markers `unit` `ingestion` `retrieval` `agent` `teams`; `addopts = "-m unit"`; `teams` always skipped (Google Chat not v1)

## §V INVARIANTS

V1: grounded-only — factual claims come from RAG retrieval; thresholds from policy hits; application facts from application hits after id or name match; never infer outcome from `application_id`, filename, or `source_name`; empty policy retrieve → exact `That is not in the published policies.`
V2: policy-docs — policy MD opens `> Policy ID:`; no synthetic watermark; application MD opens `# Credit application {application_id}`; no disclaimer phrases; no YAML frontmatter
V3: citation — every factual claim cites a retrieved filename (`source_name`) or `gs://` URI; an application citation is not the sole source of a policy threshold
V4: stack-thin — v1 = Gemini + one RAG corpus + terminal client; no Google Chat host; no second corpus
V5: corpus-shape — 12 policy Markdown files from `corpus/facts.yaml`; each fact value appears verbatim; applications are gitignored and append-only; fixtures live under `tests/fixtures/client-applications/`
V6: hash-skip — object overwrite skip via metadata `content_sha256` lowercase hex SHA-256 of the UTF-8 bytes
V7: models — chat `gemini-3.5-flash`; embedding `text-embedding-005` (`publishers/google/models/text-embedding-005`)
V8: region — workload project `lab5-gemini-dev1` in `us-east1`
V9: talos-cli — one Click package `talos`; missing required env → exit 2; bare `talos generate` → help exit 2; generate does not deploy
V10: deploy-split — bucket, APIs, RAG Engine tier, and service account via Terraform; object sync + corpus create + import via `talos deploy`
V11: env-contract — flags override process env; missing keys from `infra/outputs.json` unless `--no-terraform`; never spawn `terraform output` at runtime; never load `.env`
V12: secrets — Application Default Credentials; never commit keys
V13: wait-gate — `talos deploy --wait` requires local policy files ≥ 12 and local application files ≥ 3, then indexed counts at the same floors (application floor = max(3, local size))
V14: application-generate — opaque `CA-{YYYYMMDD}-{unix_ms}`; intended outcome only in `manifest.json`; mixed product per type; validate + one retry; prompts do not inject disclaimer phrases
V15: readme-hiring-manager — README.md is for a hiring manager: accurate, short, not an engineer runbook
V16: tfvars — apply `-var-file=lab5-gemini-dev1.tfvars`; terraform variables are only `project` and `region`

## §T TASKS

id|status|task|cites
T1|x|init talos Click CLI generate+deploy+chat, CPython 3.14, unit tests, GHA unit workflow|V9,I.cmd
T2|x|reuse policy document generation: facts.yaml, jinja, 12 MD + manifest|V2,V5,I.file
T3|x|reuse application document generation on Gemini JSON mode|V14,I.cmd
T4|x|terraform GCS bucket + RAG Engine tier + agent service account; canonical outputs|V8,V10,V16,I.infra
T5|x|talos deploy hash-skip upload + RAG import + --wait|V6,V10,V13,I.cmd
T6|x|talos chat grounded generateContent + REPL|V1,V3,V4,I.cmd

## §B BUGS

id|date|cause|fix
