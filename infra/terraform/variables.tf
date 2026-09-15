# Identifiers live in terraform.tfvars, which is gitignored, with a committed
# terraform.tfvars.example beside it. None of these are secrets -- a GCP project
# id is not a credential -- but they are deployment facts, and the rest of this
# repository keeps deployment facts in one declared place rather than scattered
# through files that each drift on their own schedule.

variable "project_id" {
  description = "Existing GCP project id. This configuration REFERENCES the project; it does not manage it."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be a valid GCP project id."
  }
}

variable "region" {
  description = "Default provider region. BigQuery datasets use `location` below, not this."
  type        = string
  default     = "us-central1"
}

variable "bq_location" {
  description = "BigQuery dataset location. Every dataset in this project is US; changing it forces replacement, which for these datasets means data loss."
  type        = string
  default     = "US"
}
