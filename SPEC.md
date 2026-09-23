# SPEC — Bank Credit Policy Agent Demo (Gemini)

## §G GOAL

Demo: credit officer in Google Chat asks an ADK Python agent on Agent Runtime. RetrievalAgent searches one Agent Search data store (12 Contoso Demo Bank Markdown policies + client applications, ingested from GCS). InteractiveAgent answers with citations and evaluates applications (`accepted`|`rejected`|`missing-data`) by `application_id` or `customer_name`, judgement grounded in named policy docs. Not a production origination system. Same use case as kborovik/azure-ai-foundry. Document generation unchanged: facts.yaml + Jinja policies, Gemini JSON filings from the same application prompts.

## §C CONSTRAINTS

- demo: no origination system-of-record; no real borrower PII; no real bank IP; no production credit decisioning
- stack: Google ADK Python on Agent Runtime; officer client = Google Chat; one Agent Search data store; GCS = ingest only
- Markdown-only corpus v1; no PDF
- two ADK agents (`InteractiveAgent` + `RetrievalAgent`); no write-back to core banking
- public Gemini API via Application Default Credentials; no API keys committed
- one Agent Search data store imports both prefixes; InteractiveAgent synthesizes; no separate answer-synthesis service
- CPython 3.14; `requires-python = ">=3.14"`; `[tool.uv] python-preference = "managed"`; no PEP 723 under `src/`
- one CLI `docgen` (generate, upload); no deploy; no chat; no data-store import; officer chat = Chat app; local dev = `adk run` / `adk web`; unit via `gmake test` → `uv run pytest`
- workload project `lab5-gemini-dev1`; region `us-east1`; state bucket `terraform-lab5-gemini-dev1` owned by gcp-lab5-org
- chat model `gemini-3.8-flash`; document embeddings owned by Agent Search
- no Application Integration / Dialogflow in v1 Terraform
- default CI = unit corpus/CLI tests; live Google Cloud behind pytest markers
- Chat handler holds no policy text; session key = Chat user + thread

## §I INTERFACES

- cmd: `uv run docgen generate` Click group; bare → help exit 2. `generate policy` renders facts+templates, writes `data/credit-policies/`, optional GCS upload; `--local-only` / `--gcs-only` mutex; `--dry-run` / `--force` / `--fail-if-missing-gcs` / `--no-terraform`. `generate application` calls Gemini JSON mode; `--type` or `--all`; `--count`; `--force --application-id`; `--local-only`; `--dry-run` prints serials and does not call the model. `docgen upload` hash-skips both prefixes to the bucket and does not import them. No `docgen deploy`. No `docgen chat`. `adk run` and `adk web` are local dev only. `adk deploy agent_engine --project --region --display_name` deploys the ADK package to Agent Runtime. `docgen --completion bash|zsh|fish|powershell` prints Click source. Missing required env → exit 2.
- index: `gmake index` ensures Agent Search data store `kb-credit-policies`, imports both GCS URIs. `gmake index wait=1` polls indexed counts to the floors. Not a `docgen` subcommand.
- env: `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, `GCS_BUCKET`, `GCS_URI`, `DATA_STORE`, `REASONING_ENGINE`. Flags > process env > `infra/outputs.json` (unwrap `.value`). No `.env` load.
- names: project `lab5-gemini-dev1`; region `us-east1`; bucket `lab5-gemini-dev1-credit-docs`; tfstate bucket `terraform-lab5-gemini-dev1` prefix `google-gemini-platform`; data store display name `kb-credit-policies`; data store location `global`; Agent Runtime region `us-east1`; prefixes `credit-policies` and `client-applications`; model `gemini-3.8-flash`
- file: `corpus/facts.yaml`; `corpus/templates/*.md.j2`; committed `data/credit-policies/*.md` + `manifest.json`; gitignored `data/client-applications/*.md` + `manifest.json`; `agents/credit_officer/credit-policy-agent.instructions.md`; `agents/credit_officer/` (`root_agent`); `tests/fixtures/golden_queries.yaml` (18 ids); `tests/fixtures/client-applications/`
- infra: `infra/*.tf`; `infra/lab5-gemini-dev1.tfvars`; variable blocks = `project`, `region` only; GCS backend on the org-factory bucket; `infra-init` checks that bucket; `gmake infra-create` apply then `terraform output -json` → `infra/outputs.json`; `infra-destroy` does not delete the state bucket; APIs `aiplatform.googleapis.com`, `discoveryengine.googleapis.com`, and `storage.googleapis.com`; no RAG Engine tier
- pytest: markers `unit` `ingestion` `retrieval` `agent` `teams`; `addopts = "-m unit"`; `teams` = Google Chat live path, not hard-skipped
- chat: Google Chat `MESSAGE` → HTTPS handler → `reasoningEngines/{id}:streamQuery` `class_method` `async_stream_query`; `user_id` = Chat user; `session_id` = space + thread; handler posts model text in that thread

## §V INVARIANTS

V1: grounded-only — factual claims come from Agent Search retrieval; thresholds from policy hits; application facts from application hits after id or name match; never infer outcome from `application_id`, filename, or `source_name`; empty policy retrieve → exact `That is not in the published policies.`
V2: policy-docs — policy MD opens `> Policy ID:`; no synthetic watermark; application MD opens `# Credit application {application_id}`; no disclaimer phrases; no YAML frontmatter
V3: citation — every factual claim cites a retrieved filename (`source_name`) or `gs://` URI; an application citation is not the sole source of a policy threshold
V4: stack — officer client = Google Chat; thin handler calls Agent Runtime; ADK Python `InteractiveAgent` holds credit-officer instructions and calls `RetrievalAgent` via `AgentTool`; `RetrievalAgent` tools = `VertexAiSearchTool` only, one Agent Search data store over both prefixes; session key = Chat user + thread; question path reads Agent Search only
V5: corpus-shape — 12 policy Markdown files from `corpus/facts.yaml`; each fact value appears verbatim; applications are gitignored and append-only; fixtures live under `tests/fixtures/client-applications/`
V6: hash-skip — object overwrite skip via metadata `content_sha256` lowercase hex SHA-256 of the UTF-8 bytes
V7: models — chat `gemini-3.8-flash`; Agent Search owns document embeddings; this repo does not set `text-embedding-005`
V8: region — workload project `lab5-gemini-dev1`; bucket and Agent Runtime in `us-east1`; Agent Search data store location `global`
V9: docgen-cli — one Click package `docgen`; subcommands generate and upload only; upload hash-skips both prefixes to the bucket; no deploy; no chat; no data-store import; missing required env → exit 2; bare `docgen generate` → help exit 2
V10: deploy-split — bucket, APIs, and service account via Terraform; object upload via `docgen`; data store ensure + import via `gmake index`; no RAG Engine tier
V11: env-contract — flags override process env; missing keys from `infra/outputs.json` unless `--no-terraform`; never spawn `terraform output` at runtime; never load `.env`
V12: secrets — Application Default Credentials; never commit keys
V13: wait-gate — `gmake index wait=1` requires local policy files ≥ 12 and local application files ≥ 3, then Agent Search indexed counts at the same floors (application floor = max(3, local size))
V14: application-generate — opaque `CA-{YYYYMMDD}-{unix_ms}`; intended outcome only in `manifest.json`; mixed product per type; validate + one retry; prompts do not inject disclaimer phrases
V15: readme-hiring-manager — README.md is for a hiring manager: accurate, short, not an engineer runbook
V16: tfvars — apply `-var-file=lab5-gemini-dev1.tfvars`; terraform variables are only `project` and `region`
V17: model-host — chat model per §V.7 runs inside Agent Runtime; this repo does not call generateContent on the workload-region host; no RAG corpus; no us-east5 remap

## §T TASKS

id|status|task|cites
T1|x|init talos Click CLI generate+deploy+chat, CPython 3.14, unit tests, GHA unit workflow|V9,I.cmd
T2|x|reuse policy document generation: facts.yaml, jinja, 12 MD + manifest|V2,V5,I.file
T3|x|reuse application document generation on Gemini JSON mode|V14,I.cmd
T4|x|terraform GCS bucket + RAG Engine tier + agent service account; canonical outputs|V8,V10,V16,I.infra
T5|x|talos deploy hash-skip upload + RAG import + --wait|V6,V10,V13,I.cmd
T6|x|talos chat grounded generateContent + REPL|V1,V3,V4,I.cmd
T7|x|sync chat model id to gemini-3.8-flash and publish generateContent on global for a single-region workload|V7,V17,I.cmd
T8|x|add ADK package InteractiveAgent + RetrievalAgent; RetrievalAgent tool = VertexAiSearchTool only; InteractiveAgent calls it via AgentTool; load credit-officer instructions|V1,V3,V4,V7,I.cmd
T9|x|swap terraform off RAG Engine; gmake index ensures one Agent Search data store and imports both GCS prefixes; --wait uses indexed counts|V8,V10,V13,V16,V17,I.infra,I.index
T10|x|add Google Chat handler: MESSAGE → streamQuery; session = Chat user + thread; reply in thread|V4,I.chat
T11|x|rename package and console script talos → docgen; docgen = generate + GCS upload only; drop deploy and chat|V6,V9,I.cmd
T12|.|sync README and docs/demo.md to the Chat + Agent Search path|V15,I.file
T13|.|drop text-embedding-005 from constants and tests; scope grep text-embedding-005|V7

## §B BUGS

id|date|cause|fix
B1|2026-09-22|generateContent used workload region; gemini-3.8-flash 404 off global, us, and eu|V17
B2|2026-09-22|new projects not allowlisted for RAG in us-central1, us-east1, us-east4; corpus create uses us-east5|V17
