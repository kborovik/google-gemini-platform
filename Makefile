ifeq ($(filter notintermediate,$(.FEATURES)),)
$(error GNU Make ≥ 4.4 required (this is $(MAKE_VERSION) from $(MAKE)). On macOS: brew install make && gmake <target>)
endif

.SILENT:

SHELL := /bin/sh
MAKEFLAGS += --no-builtin-rules --no-builtin-variables
export PATH := $(abspath .venv)/bin:$(PATH)

UV ?= uv
export PYTHONUNBUFFERED := 1
export PYTHONWARNINGS := ignore
export PYTEST_ADDOPTS := --disable-warnings -W ignore

esc := $(shell printf '\033')
blue := $(esc)[34m
green := $(esc)[32m
yellow := $(esc)[33m
reset := $(esc)[0m

header = $(info $(blue)==> $1 <==$(reset))

dry-run = $(findstring n,$(firstword $(MAKEFLAGS)))

need-terraform = $(if $(dry-run),,$(if $(shell command -v terraform),,$(error terraform not on PATH)))
need-gcloud = $(if $(dry-run),,$(if $(shell command -v gcloud),,$(error gcloud CLI required)))
need-gcloud-auth = $(if $(dry-run),,$(shell gcloud auth list --filter=status:ACTIVE --format='value(account)' | grep -q .)$(if $(filter 0,$(.SHELLSTATUS)),,$(error gcloud not authenticated — run: gmake google-auth)))
need-gh = $(if $(dry-run),,$(if $(shell command -v gh),,$(error gh CLI required)))
need-gh-auth = $(if $(dry-run),,$(shell gh auth status >/dev/null 2>&1)$(if $(filter 0,$(.SHELLSTATUS)),,$(error gh not authenticated — run: gh auth login)))
need-clean = $(if $(dry-run),,$(if $(shell git status --porcelain),$(error working tree not clean — commit or stash first)))
need-part = $(if $(part),,$(error usage: gmake release major|minor|patch))

# Workload project provisioned by gcp-lab5-org. State bucket is
# terraform-$(PROJECT), created in that project by the org factory.
PROJECT ?= lab5-gemini-dev1
REGION ?= us-east1
google_project := $(PROJECT)
google_region := $(REGION)
google_zone ?= $(google_region)-b
TFSTATE_BUCKET := terraform-$(PROJECT)
TF_PREFIX := google-gemini-platform
TF_VAR_FILE := $(PROJECT).tfvars

git_root := $(shell git rev-parse --show-toplevel)
terraform_dir := $(git_root)/infra
terraform_tfvars := $(terraform_dir)/$(TF_VAR_FILE)
terraform_bucket := $(TFSTATE_BUCKET)

rwildcard = $(strip \
	$(wildcard $(1)$(2)) \
	$(foreach d,$(wildcard $(1)*),$(if $(wildcard $(d)/.),$(call rwildcard,$(d)/,$(2)))))

default: help

.PHONY: help test check generate deploy index --wait preflight e2e clean
.PHONY: terraform terraform-config terraform-fmt terraform-init terraform-validate
.PHONY: terraform-plan terraform-apply terraform-destroy terraform-clean terraform-show terraform-list
.PHONY: terraform-state-recursive terraform-state-versions terraform-state-unlock prompt
.PHONY: google google-auth google-logout google-config
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
	$(UV) run docgen generate application --all --local-only

deploy: .venv terraform-apply ## Apply, set DATA_STORE, upload, index, deploy the agent
	$(call need-terraform)
	$(call header,Reading DATA_STORE)
	data_store=$$(terraform -chdir=$(terraform_dir) output -raw DATA_STORE) && \
	test -n "$$data_store" && \
	printf 'DATA_STORE=%s\n' "$$data_store" > $(git_root)/agents/credit_officer/.env && \
	printf '%s\n' "$$data_store"
	$(call header,Uploading credit-policy corpus)
	$(UV) run docgen upload
	$(MAKE) index wait=1
	$(call header,Deploying credit officer)
	$(UV) run adk deploy agent_engine \
		--project=$(PROJECT) \
		--region=$(REGION) \
		--display_name=credit-officer \
		agents/credit_officer

# GNU make rejects `gmake index --wait` because a dashed word is an option.
# `gmake index wait=1` and `gmake -- index --wait` poll indexed counts.
ifeq ($(filter --wait,$(MAKECMDGOALS)),--wait)
wait := 1
endif

--wait:
	@:

index: .venv ## Import both prefixes into data store kb-credit-policies
	$(call header,Indexing Agent Search data store)
	$(UV) run docgen index $(if $(wait),--wait,)

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

e2e: check preflight generate ## check, apply, generate, upload, index, live pytest
	$(call header,Uploading credit-policy corpus)
	$(UV) run docgen upload
	$(MAKE) index wait=1
	$(call header,Live e2e)
	$(UV) run pytest -v -ra -s --durations=0 \
		-m "ingestion or retrieval or agent" --override-ini addopts= $(e2e_target)

clean: ## Remove caches and bytecode
	$(call header,Cleaning)
	rm -rf .ruff_cache .pytest_cache dist build src/docgen.egg-info *.egg-info $(call rwildcard,,__pycache__)
	rm -f .release-notes $(call rwildcard,,*.pyc) $(call rwildcard,,.DS_Store)

##@ Terraform:

ifeq ($(wildcard $(terraform_tfvars)),)
$(warning ==> $(terraform_tfvars) not found <==)
endif

terraform: terraform-plan prompt terraform-apply ## Plan, confirm, then apply

terraform-config:
	$(call header,Configure Terraform)
	ln -fs $(terraform_tfvars) $(terraform_dir)/terraform.tfvars

terraform-fmt: terraform-config
	$(call need-terraform)
	$(call header,Check Terraform Code Format)
	terraform -chdir=$(terraform_dir) fmt -check -recursive

terraform-init: terraform-fmt
	$(call need-gcloud)
	$(call need-terraform)
	$(call need-gcloud-auth)
	$(call header,Initialize Terraform)
	gcloud storage buckets describe gs://$(terraform_bucket) --project=$(google_project) >/dev/null 2>&1 \
	  || { echo "backend bucket missing — gs://$(terraform_bucket) is created by gcp-lab5-org" >&2; exit 1; }
	terraform -chdir=$(terraform_dir) init -input=false -upgrade -reconfigure \
		-backend-config="bucket=$(terraform_bucket)" \
		-backend-config="prefix=$(TF_PREFIX)"

terraform-validate: terraform-init
	$(call header,Validate Terraform)
	terraform -chdir=$(terraform_dir) validate

terraform-plan: terraform-validate ## Plan in $(PROJECT)
	$(call header,Run Terraform Plan)
	terraform -chdir=$(terraform_dir) plan -input=false -refresh=true -var-file=$(TF_VAR_FILE)

terraform-apply: terraform-validate ## Apply in $(PROJECT); write infra/outputs.json
	$(call header,Run Terraform Apply)
	terraform -chdir=$(terraform_dir) apply -auto-approve -input=false -var-file=$(TF_VAR_FILE)
	terraform -chdir=$(terraform_dir) output -json > $(terraform_dir)/outputs.json

terraform-destroy: terraform-validate ## Destroy the workload stack
	$(call header,Terraform destroy $(PROJECT))
	terraform -chdir=$(terraform_dir) apply -destroy -input=false -refresh=true -var-file=$(TF_VAR_FILE)
	rm -f $(terraform_dir)/outputs.json

terraform-show:
	$(call need-terraform)
	terraform -chdir=$(terraform_dir) show -no-color | bat -l Terraform

terraform-list:
	$(call need-terraform)
	terraform -chdir=$(terraform_dir) state list

terraform-state-recursive:
	$(call need-gcloud)
	$(call need-gcloud-auth)
	gcloud storage ls -r "gs://$(terraform_bucket)/$(TF_PREFIX)/**"

terraform-state-versions:
	$(call need-gcloud)
	$(call need-gcloud-auth)
	gcloud storage ls -a "gs://$(terraform_bucket)/$(TF_PREFIX)/default.tfstate"

terraform-state-unlock:
	$(call need-gcloud)
	$(call need-gcloud-auth)
	$(call header,Remove Terraform state lock)
	gcloud storage rm "gs://$(terraform_bucket)/$(TF_PREFIX)/default.tflock"

terraform-clean:
	$(call header,Delete Terraform providers and local state)
	rm -rf $(terraform_dir)/.terraform

prompt:
	printf "$(yellow)Continue? (yes/no)$(reset): "
	read answer && [ "$$answer" = "yes" ] || exit 127

##@ Google Cloud:

google: google-config ## Point the Google CLI at $(PROJECT)

google-auth: ## Log in and refresh application-default credentials
	$(call need-gcloud)
	$(call header,Configure Google CLI)
	gcloud auth login --update-adc --no-launch-browser

google-logout: ## Revoke gcloud credentials
	$(call need-gcloud)
	$(call header,Logout Google CLI)
	gcloud auth revoke --all

google-config: ## Set quota project, project, region, and zone
	$(call need-gcloud)
	$(call need-gcloud-auth)
	gcloud auth application-default set-quota-project $(google_project)
	gcloud config set core/project $(google_project)
	gcloud config set compute/region $(google_region)
	gcloud config set compute/zone $(google_zone)
	gcloud config list

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
	$(info $(yellow)test$(reset)                unit tests)
	$(info $(yellow)check$(reset)               ruff + unit tests)
	$(info $(yellow)generate$(reset)            sample applications, local only)
	$(info $(yellow)deploy$(reset)              apply, DATA_STORE, upload, index --wait, adk deploy)
	$(info $(yellow)index$(reset)               import both prefixes into kb-credit-policies)
	$(info $(yellow)index wait=1$(reset)        poll indexed counts (also: gmake -- index --wait))
	$(info $(yellow)e2e$(reset)                 check, apply, generate, upload, index, live pytest)
	$(info $(yellow)terraform$(reset)           plan, confirm, then apply)
	$(info $(yellow)terraform-plan$(reset)      plan in $(PROJECT))
	$(info $(yellow)terraform-apply$(reset)     apply; write infra/outputs.json)
	$(info $(yellow)terraform-destroy$(reset)   destroy workload stack)
	$(info $(yellow)google-auth$(reset)         log in and refresh application-default credentials)
	$(info $(yellow)google-config$(reset)       set project, region, zone, and quota project)
	$(info $(yellow)google-logout$(reset)       revoke gcloud credentials)
	$(info $(yellow)release$(reset)             gmake release major|minor|patch)
	:
