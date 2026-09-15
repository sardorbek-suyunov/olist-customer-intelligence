# ---------------------------------------------------------------------------
# Olist Customer Intelligence -- the GCP resources, as they actually exist.
#
# WRITTEN FROM AN ENUMERATION, NOT FROM MEMORY.
#
# Every resource below was read out of the live project before a line of this
# file was written -- `gcloud iam service-accounts list`, the BigQuery client's
# own dataset and access-entry listing, `gcloud services list --enabled`, and
# `gcloud alpha services quota list`. Then each was imported into state and
# `terraform plan` was run until it reported no changes. That plan output is
# committed beside this file as PLAN.txt.
#
# The reason for that order is the same one the README's failure table is about:
# configuration describing resources nobody looked at is a document that reads as
# authoritative and was never checked. A .tf file is especially good at it,
# because it looks like infrastructure whether or not it matches any.
#
# TWO THINGS THE ENUMERATION FOUND, both recorded rather than papered over:
#
#   1. There is NO Streamlit service account and no dataViewer binding anywhere.
#      Project IAM holds exactly one member -- the owner. Every dataset carries
#      only the four default access entries. The README described that grant as
#      "the control that actually stops a DELETE"; it does not exist and never
#      did. Nothing is codified here for it, because fabricating it in Terraform
#      would make the claim true-looking in a second place.
#
#   2. The BigQuery DAILY QUERY quota is not set. Two consumer overrides do
#      exist, both at 10 GB/day, and both are on the wrong metric -- see
#      quotas.tf. They are imported here because they are real, not because they
#      are right.
#
# WHAT IS DELIBERATELY NOT MANAGED
#
#   The project itself. Referenced through var.project_id. Managing project
#   lifecycle here would let one stray destroy take the datasets, the billing
#   link and the enrichment output with it, and there is no upside: the project
#   already exists and is created once.
#
#   Project IAM. The single binding is roles/owner for the human who owns this.
#   Putting that under Terraform means a botched apply can remove the only
#   administrator of the project, which is a worse failure than any it prevents.
#
#   The `ais-gemini-key-...` service account. Google created it to back the
#   Gemini API key and manages its lifecycle; it holds one SYSTEM_MANAGED key
#   and no user-managed keys. Importing a Google-managed resource means
#   Terraform and Google both believe they own it.
# ---------------------------------------------------------------------------

# ------------------------------------------------------------------ datasets
#
# Two protections, and it is worth being exact about what each one buys,
# because the obvious third one does not exist.
#
#   prevent_destroy            Terraform refuses to even PLAN a destroy of these.
#                              This is the one that stops `terraform destroy`.
#   delete_contents_on_destroy Left false, so Terraform will not empty a dataset
#                              on the way out. BigQuery itself then refuses to
#                              drop a dataset that still has tables.
#
# There is NO `deletion_protection` argument on google_bigquery_dataset -- it
# exists on google_bigquery_table, not on datasets, and the provider rejects it
# outright. So there is no API-enforced flag at the dataset level to set, and
# the protection here is Terraform-side plus BigQuery's own refusal to drop a
# non-empty dataset. Someone with gcloud and intent can still delete these; the
# defence against that is that they are not the threat model. The threat model
# is an absent-minded `terraform destroy`.
#
# The reason any of it is on is money. olist_raw and olist_marts carry the Gemini
# enrichment output -- 35,616 labelled reviews and 35,616 embeddings that cost
# $3.19 and ~87 minutes, and which cannot be regenerated for free. A destroy
# that takes them does not lose a rebuildable artifact, it spends the budget
# again.

locals {
  # The names only. Descriptions are NOT set here, and that is a correction
  # rather than an omission: the first draft of this file gave each dataset a
  # helpful description, and `terraform plan` reported four in-place updates --
  # because the live datasets have no description at all. Config that would have
  # CHANGED the project on first apply, while claiming to describe it.
  #
  # So what each dataset is for stays a comment, where it cannot be mistaken for
  # deployed state:
  #
  #   olist          dbt profile dataset. dbt appends each model's custom schema,
  #                  so materialised models land in olist_staging / olist_marts.
  #   olist_raw      Ingestion target: replayed Parquet slices plus the committed
  #                  Gemini enrichment snapshot.
  #   olist_staging  dbt staging models -- views over olist_raw.
  #   olist_marts    dbt marts. The only dataset the NL->SQL agent is shown.
  #
  # Setting them for real is a one-line change and an apply. It was not made
  # here because "write down what exists" and "improve what exists" are two
  # different jobs and only the first one was asked for.
  dataset_ids = ["olist", "olist_raw", "olist_staging", "olist_marts"]
}

# The project NUMBER, read from the project rather than typed. The quota
# override resources key on the number, not the id; hardcoding it would put a
# second identifier in terraform.tfvars that has to agree with the first.
data "google_project" "this" {
  project_id = var.project_id
}

resource "google_bigquery_dataset" "this" {
  for_each = toset(local.dataset_ids)

  project    = var.project_id
  dataset_id = each.key
  location   = var.bq_location

  # BigQuery itself then refuses to delete a dataset that still has tables.
  delete_contents_on_destroy = false

  lifecycle {
    prevent_destroy = true

    # Access entries are the four GCP defaults -- projectOwners, projectWriters,
    # projectReaders and the owner. They are not declared above because they are
    # not a decision anyone made; they are what BigQuery creates. Declaring them
    # would mean this file asserts an access model it did not design, and the
    # first real grant would have to be added in two places.
    ignore_changes = [access]
  }
}

# ------------------------------------------------------------------ services
#
# Only the APIs this project actually depends on. The project has 23 enabled;
# most are GCP defaults that were never a decision and would be noise here.
#
# disable_on_destroy is false throughout. Disabling an API is not a tidy-up --
# it breaks every other thing in the project that happens to use it, including
# things Terraform does not know about.

resource "google_project_service" "required" {
  for_each = toset([
    "bigquery.googleapis.com",           # the warehouse
    "bigquerystorage.googleapis.com",    # the Storage Read API, used by the client
    "generativelanguage.googleapis.com", # Gemini: enrichment, embeddings, the agent
    "serviceusage.googleapis.com",       # required to read or set the quota overrides below
  ])

  project = var.project_id
  service = each.key

  disable_on_destroy         = false
  disable_dependent_services = false
}
