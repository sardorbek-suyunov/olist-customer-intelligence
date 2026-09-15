# ---------------------------------------------------------------------------
# Consumer quota overrides.
#
# THE FINDING THIS FILE EXISTS TO RECORD
#
# The intent was a daily cap on BigQuery query bytes -- a platform-enforced
# ceiling, which this project prefers over an application-enforced one for the
# same reason it prefers `maximum_bytes_billed` over a prompt asking the model
# to be careful: one is a control and the other is a hope.
#
# That cap is NOT SET. `bigquery.googleapis.com/quota/query/usage` reports
# `consumerOverride: null` on both its buckets -- per-project and per-user.
#
# What IS set is the two overrides below, both at 10,000,000,000 (10 GB/day),
# and neither of them constrains query scanning:
#
#   quota/extract/bytes
#     Bytes that EXTRACT jobs may write per day. Default 50 TiB, now 10 GB.
#     This project runs no extract jobs, so the cap has never been reached and
#     its presence has never been felt.
#
#   quota/query/alloydb_federated_query_cross_region_bytes
#     Cross-region bytes for federated queries against AlloyDB. Default 1 TiB,
#     now 10 GB. This project has no AlloyDB instance and no federated queries.
#     The override is completely inert.
#
# Both metric names contain "bytes" and both sort near the query metrics in the
# console's quota list. The most likely explanation is a filtered list and two
# adjacent rows.
#
# WHY THEY ARE IMPORTED RATHER THAN CORRECTED OR DELETED
#
# Because they exist. This directory's claim is that it describes the project as
# it is, proven by a plan with no changes -- and a configuration that omits two
# live resources because they are embarrassing is exactly as inaccurate as one
# that invents resources that are absent. Correcting them is a change to
# infrastructure and a separate decision, not a side effect of writing it down.
#
# WHAT THE PROJECT ACTUALLY RELIES ON IN THE MEANTIME
#
# Only application-level ceilings: `maximum_bytes_billed` set per job in
# transform/profiles.yml (2 GiB) and in analytics/bq_safety.py (1 GiB per query,
# 5 GiB per session). Those are real and they are tested -- BigQuery was observed
# rejecting a query with `bytesBilledLimitExceeded`. But every one of them is set
# by code in this repository, so all of them share a failure mode that a
# project-level quota would not: they protect against a query being too large,
# not against this repository being wrong about what it sets.
#
# To actually set the intended cap (10 GiB/day = 10737418240):
#
#   gcloud alpha services quota update \
#     --service=bigquery.googleapis.com \
#     --consumer=projects/<project-id> \
#     --metric=bigquery.googleapis.com/quota/query/usage \
#     --unit='1/d/{project}' \
#     --value=10737418240
#
# It is left undone here because changing a live ceiling is the owner's call.
# ---------------------------------------------------------------------------

locals {
  # Both overrides carry the same value, which is itself evidence they were set
  # in one sitting from one intent.
  misapplied_override_bytes = "10000000000"
}

resource "google_service_usage_consumer_quota_override" "extract_bytes" {
  provider = google-beta

  project        = data.google_project.this.number
  service        = "bigquery.googleapis.com"
  metric         = urlencode("bigquery.googleapis.com/quota/extract/bytes")
  limit          = urlencode("/d/project")
  override_value = local.misapplied_override_bytes

  # Without this, removing the resource from configuration would DELETE the
  # override rather than just stop managing it. That is the right default in
  # general and the wrong one here: whether these overrides should exist is an
  # open question, and `terraform destroy` is not how it should be answered.
  force = false

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_service_usage_consumer_quota_override" "alloydb_federated_cross_region_bytes" {
  provider = google-beta

  project        = data.google_project.this.number
  service        = "bigquery.googleapis.com"
  metric         = urlencode("bigquery.googleapis.com/quota/query/alloydb_federated_query_cross_region_bytes")
  limit          = urlencode("/d/project")
  override_value = local.misapplied_override_bytes

  force = false

  lifecycle {
    prevent_destroy = true
  }
}
