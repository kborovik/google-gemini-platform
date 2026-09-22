# Demo path

Four steps take this repository to a cited answer in the terminal.

1. **Google Cloud resources (`infra/`).** Terraform runs in `lab5-gemini-dev1` (`us-east1`). It creates a private document bucket, turns on the RAG Engine, and creates the agent service account. State goes in the existing bucket `terraform-lab5-gemini-dev1`.

2. **Documents (`data/`).** Twelve credit policies are rendered from `corpus/` into `data/credit-policies/`. Sample applications are generated into `data/client-applications/`. Both are uploaded to the bucket.

3. **Knowledge (`talos deploy`).** One RAG corpus, `kb-credit-policies`, imports both prefixes. The model retrieves passages from that corpus. It does not invent LTV, DTI, or committee names.

4. **Chat (`talos chat`).** The terminal is the credit-officer client. Ask a policy question, or name an application by number or customer. Google Chat is not part of this version.

```bash
gmake infra-create
uv run talos generate application --all --local-only
uv run talos deploy --wait
uv run talos chat "What is the max LTV for an owner-occupied residential mortgage?"
```
