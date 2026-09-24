# Demo path

Run `gmake generate`, then `gmake deploy`.

1. **Generate (`gmake generate`).** Twelve credit policies are already rendered from `corpus/` into `data/credit-policies/`. This recipe runs `uv run docgen generate application --all --local-only` and writes sample applications under `data/client-applications/`.

2. **Deploy (`gmake deploy`).** This recipe runs the hosted path in project `lab5-gemini-dev1`, in this order:
   1. Terraform applies in region `us-east1`. It creates the private document bucket `lab5-gemini-dev1-credit-docs`, the empty Agent Search data store `kb-credit-policies` in location `global`, the agent service account `credit-policy-agent`, and the Reasoning Engine `credit-officer`. The engine runs `agents/credit_officer` on Agent Runtime in `us-east1`. `DATA_STORE` is set on the engine. Output `REASONING_ENGINE` is `projects/lab5-gemini-dev1/locations/us-east1/reasoningEngines/{id}`, written to `infra/outputs.json`. It enables `aiplatform.googleapis.com`, `artifactregistry.googleapis.com`, `cloudbuild.googleapis.com`, `discoveryengine.googleapis.com`, `dns.googleapis.com`, `run.googleapis.com`, `siteverification.googleapis.com`, and `storage.googleapis.com`. State stays in the existing bucket `terraform-lab5-gemini-dev1`. `adk run` and `adk web` stay local.
   2. The recipe reads `DATA_STORE` from the Terraform output and writes `agents/credit_officer/.env`. The value is `projects/lab5-gemini-dev1/locations/global/collections/default_collection/dataStores/kb-credit-policies`.
   3. `docgen upload` stores both prefixes in the bucket.
   4. `docgen index --wait` imports `gs://lab5-gemini-dev1-credit-docs/credit-policies/` and `gs://lab5-gemini-dev1-credit-docs/client-applications/` into data store `kb-credit-policies`. It does not create the store. It then polls until indexed counts meet the floors: at least 12 policies and at least 3 applications.

3. **Google Chat.** The public handler is `chat/main.py` on Cloud Run service `chat` in `us-east1` at `https://credit-policy.ai.lab5.ca`. Terraform sets `custom_audiences` on that service to exactly `https://credit-policy.ai.lab5.ca` with no trailing slash. That URL is the Chat HTTP endpoint and the authentication audience. Terraform creates public Cloud DNS zone `ai.lab5.ca` and CNAME `credit-policy.ai.lab5.ca` to `ghs.googlehosted.com`. Output `AI_ZONE_NS` lists that zone's name servers. Parent name-server records for `ai.lab5.ca` stay in zone `lab5.ca`. This repository does not manage zone `lab5.ca`. Terraform verifies `INET_DOMAIN` `ai.lab5.ca` with a DNS TXT record in zone `ai-lab5-ca` before the domain mapping. The Site Verification web resource uses `deletion_policy` `ABANDON`. The domain mapping depends on that web resource. The verified owner is the account that applies Terraform. `gmake google-auth` refreshes that account's application-default credentials with scope `https://www.googleapis.com/auth/siteverification`. This repository does not verify `lab5.ca`. `gmake deploy` applies Terraform, which packages `chat/`, submits Cloud Build, and deploys Cloud Run service `chat`. There is no Make recipe for the handler and no local image build. The handler listens for a `MESSAGE`. It calls `reasoningEngines/{id}:streamQuery` with class method `async_stream_query`, and posts the model text in that thread. `user_id` is the Chat user. `session_id` is the space plus the thread. The handler holds no policy text.

```bash
gmake generate
gmake deploy
```
