# Pinned, not floating. A provider that resolves differently next month turns a
# reconciled plan into an unreconciled one without anything in this repository
# changing -- which is the drift this whole directory exists to remove.
terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 6.0"
    }
  }
}

# user_project_override + billing_project are required for billingbudgets.
# Without them the budget import fails with "The billingbudgets.googleapis.com
# API requires a quota project, which is not set by default" -- a message about
# the CALLER's credentials that reads like a problem with the resource. Setting
# it here rather than telling everyone to run `gcloud auth application-default
# set-quota-project` means the configuration works from a clean checkout instead
# of depending on a local credential tweak nobody wrote down.
provider "google" {
  project = var.project_id
  region  = var.region
}

# A SECOND, aliased google provider, used by exactly one resource.
#
# billingbudgets requires a quota project on the caller's credentials, and
# without it the budget import fails with a message about the caller that reads
# like a problem with the resource. The fix is user_project_override.
#
# It is aliased rather than set on the default provider because setting it
# globally routes EVERY request through the project's own quota -- including the
# google_project data source, which then needs cloudresourcemanager.googleapis.com
# enabled and fails with a different 403 entirely. One provider-wide flag, set to
# fix one resource, broke an unrelated lookup and cost an API enablement nobody
# asked for. Scoped instead.
provider "google" {
  alias                 = "billing"
  project               = var.project_id
  region                = var.region
  billing_project       = var.project_id
  user_project_override = true
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}
