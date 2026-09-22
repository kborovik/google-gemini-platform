ifeq ($(filter notintermediate,$(.FEATURES)),)
$(error GNU Make ≥ 4.4 required (this is $(MAKE_VERSION) from $(MAKE)). On macOS: brew install make && gmake <target>)
endif

.SILENT:

SHELL := /bin/sh
MAKEFLAGS += --no-builtin-rules --no-builtin-variables
export PATH := $(abspath .venv)/bin:$(PATH)

UV ?= uv
export PYTHONUNBUFFERED := 1

esc := $(shell printf '\033')
blue := $(esc)[34m
green := $(esc)[32m
yellow := $(esc)[33m
reset := $(esc)[0m

header = $(info $(blue)==> $1 <==$(reset))

dry-run = $(findstring n,$(firstword $(MAKEFLAGS)))

need-terraform = $(if $(dry-run),,$(if $(shell command -v terraform),,$(error terraform not on PATH)))
need-gcloud = $(if $(dry-run),,$(if $(shell command -v gcloud),,$(error gcloud CLI required)))
need-gcloud-auth = $(if $(dry-run),,$(shell gcloud auth list --filter=status:ACTIVE --format='value(account)' | grep -q .)$(if $(filter 0,$(.SHELLSTATUS)),,$(error gcloud not authenticated — run: gcloud auth login && gcloud auth application-default login)))
need-jq = $(if $(dry-run),,$(if $(shell command -v jq),,$(error jq required)))
need-gh = $(if $(dry-run),,$(if $(shell command -v gh),,$(error gh CLI required)))
need-gh-auth = $(if $(dry-run),,$(shell gh auth status >/dev/null 2>&1)$(if $(filter 0,$(.SHELLSTATUS)),,$(error gh not authenticated — run: gh auth login)))
need-clean = $(if $(dry-run),,$(if $(shell git status --porcelain),$(error working tree not clean — commit or stash first)))
need-part = $(if $(part),,$(error usage: gmake release major|minor|patch))

# Workload project provisioned by gcp-lab5-org. State bucket is
# terraform-$(PROJECT), created in that project by the org factory.
PROJECT ?= lab5-gemini-dev1
REGION ?= us-east1
TFSTATE_BUCKET := terraform-$(PROJECT)
TF_PREFIX := google-gemini-platform
TF_VAR_FILE := $(PROJECT).tfvars

rwildcard = $(strip \
	$(wildcard $(1)$(2)) \
	$(foreach d,$(wildcard $(1)*),$(if $(wildcard $(d)/.),$(call rwildcard,$(d)/,$(2)))))

default: help

.PHONY: help test check generate deploy infra preflight e2e clean
.PHONY: infra-create infra-plan infra-fmt infra-validate infra-show infra-status infra-destroy infra-init
.PHONY: infra-backend-show
.PHONY: release major minor patch
.PHONY: _release-pre _release-bump _release-tag _release-gh

##@ Local:
test: .venv ## Run unit tests
	$(call header,Running unit tests)
	$(UV) run pytest

check: .venv ## Check Python code
	$(call header,Checking)
	$(UV) run ruff format --check
	$(UV) run ruff check
	$(MAKE) test

generate: .venv ## Generate sample client applications locally
	$(call header,Generating client applications)
	$(UV) run talos generate application --all --local-only

deploy: .venv infra-create ## Upload corpora and import the RAG corpus
	$(call need-terraform)
	$(call header,Deploying credit-policy corpus)
	$(UV) run talos deploy --wait

preflight: .venv
	$(call need-gcloud)
	$(call need-terraform)
	$(call need-gcloud-auth)
	$(call header,Google Cloud preflight)
	gcloud config get-value project
	terraform version

e2e_target := $(if $(FILE),$(firstword $(wildcard $(FILE) tests/$(FILE) tests/$(FILE).py)),)
ifneq ($(filter e2e,$(MAKECMDGOALS)),)
$(if $(FILE),$(if $(e2e_target),,$(error no test file matches FILE=$(FILE))))
endif

e2e: check preflight infra-create generate deploy ## infra-create + generate + deploy --wait + live pytest
	$(call header,Live e2e)
	$(UV) run pytest -v -ra -s --durations=0 \
		-m "ingestion or retrieval or agent" --override-ini addopts= $(e2e_target)

clean: ## Remove caches and bytecode
	$(call header,Cleaning)
	rm -rf .ruff_cache .pytest_cache dist build src/talos.egg-info *.egg-info $(call rwildcard,,__pycache__)
	rm -f .release-notes $(call rwildcard,,*.pyc) $(call rwildcard,,.DS_Store)

##@ Infrastructure:
infra-backend-show:
	$(call need-gcloud)
	$(call need-gcloud-auth)
	gcloud storage buckets describe gs://$(TFSTATE_BUCKET) --project=$(PROJECT)

infra-fmt:
	$(call need-terraform)
	$(call header,Terraform fmt)
	terraform -chdir=infra fmt

infra-init: infra-fmt
	$(call need-gcloud)
	$(call need-terraform)
	$(call need-gcloud-auth)
	gcloud storage buckets describe gs://$(TFSTATE_BUCKET) --project=$(PROJECT) >/dev/null 2>&1 \
	  || { echo "backend bucket missing — gs://$(TFSTATE_BUCKET) is created by gcp-lab5-org" >&2; exit 1; }
	terraform -chdir=infra init -input=false -reconfigure \
		-backend-config="bucket=$(TFSTATE_BUCKET)" \
		-backend-config="prefix=$(TF_PREFIX)"

infra-validate: infra-init
	$(call header,Terraform validate $(PROJECT))
	terraform -chdir=infra validate

infra-plan: infra-validate ## terraform plan in $(PROJECT)
	$(call header,Terraform plan $(PROJECT))
	terraform -chdir=infra plan -input=false -var-file=$(TF_VAR_FILE)

infra-create: infra-validate ## terraform apply in $(PROJECT); write infra/outputs.json
	$(call header,Terraform apply $(PROJECT))
	terraform -chdir=infra apply -input=false -auto-approve -var-file=$(TF_VAR_FILE)
	terraform -chdir=infra output -json > infra/outputs.json

infra-show:
	terraform -chdir=infra show -no-color

infra-status: ## Concise live bucket and project status
	$(call need-gcloud)
	$(call need-gcloud-auth)
	$(call need-jq)
	$(call header,Infra status)
	test -f infra/outputs.json || { echo "missing infra/outputs.json — run: gmake infra-create" >&2; exit 1; }
	project=$$(jq -r '.GOOGLE_CLOUD_PROJECT.value' infra/outputs.json); \
	bucket=$$(jq -r '.GCS_BUCKET.value' infra/outputs.json); \
	echo "project $$project"; \
	gcloud storage buckets describe gs://$$bucket --project=$$project --format='value(name,location)'; \
	echo "credit-policies $$(gcloud storage objects list gs://$$bucket/credit-policies --format='value(name)' | wc -l | tr -d ' ') objects"; \
	echo "client-applications $$(gcloud storage objects list gs://$$bucket/client-applications --format='value(name)' | wc -l | tr -d ' ') objects"

infra-destroy: infra-init ## terraform destroy workload stack; drop infra/outputs.json
	$(call header,Terraform destroy $(PROJECT))
	terraform -chdir=infra destroy -input=false -auto-approve -var-file=$(TF_VAR_FILE)
	rm -f infra/outputs.json

##@ Release:
part := $(firstword $(filter major minor patch,$(MAKECMDGOALS)))
ifneq ($(filter release,$(MAKECMDGOALS)),)
$(if $(part),,$(error usage: gmake release major|minor|patch))
endif
VERSION = $(shell $(UV) version --short)

release: check _release-gh ## Bump version, promote CHANGELOG, tag, push, gh release

_release-pre: check
	$(call need-part)
	$(call need-clean)
	$(call need-gh)
	$(call need-gh-auth)
	./changelog check

_release-bump: _release-pre
	$(UV) version --bump $(part)

_release-tag: _release-bump
	./changelog promote "$(VERSION)"
	git add pyproject.toml uv.lock CHANGELOG.md
	git commit -m "chore: release v$(VERSION)"
	git tag "v$(VERSION)"

_release-gh: _release-tag
	git push
	git push --tags
	./changelog notes "$(VERSION)" > .release-notes
	gh release create "v$(VERSION)" --title "v$(VERSION)" --notes-file .release-notes --verify-tag
	rm -f .release-notes

major minor patch: ;

.venv: uv.lock
	$(UV) venv --clear
	$(UV) sync

uv.lock: pyproject.toml
	$(UV) lock
	touch $@

help:
	$(info $(blue)Usage: $(green)gmake [recipe]$(reset))
	$(info )
	$(info $(yellow)test$(reset)                 unit tests)
	$(info $(yellow)check$(reset)                ruff + unit tests)
	$(info $(yellow)generate$(reset)             sample applications, local only)
	$(info $(yellow)deploy$(reset)               terraform apply + talos deploy --wait)
	$(info $(yellow)e2e$(reset)                  check, apply, generate, deploy, live pytest)
	$(info $(yellow)infra-create$(reset)         terraform apply in $(PROJECT))
	$(info $(yellow)infra-plan$(reset)           terraform plan)
	$(info $(yellow)infra-destroy$(reset)        terraform destroy workload stack)
	$(info $(yellow)release$(reset)              gmake release major|minor|patch)
	:
