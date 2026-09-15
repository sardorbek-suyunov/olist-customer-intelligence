# ---------------------------------------------------------------------------
# Consumer quota overrides.
#
# THE EPISODE THIS FILE RECORDS
#
# The intent was a daily ceiling on BigQuery query bytes -- platform-enforced
# rather than application-enforced, for the same reason this project prefers
# `maximum_bytes_billed` to asking a model nicely: one is a control and the
# other is a hope. The README claimed a 10 GiB/day ceiling.
#
# Enumerating the project to write this directory found that
# `bigquery.googleapis.com/quota/query/usage` had `consumerOverride: null`. The
# ceiling did not exist. What existed were two overrides, both at exactly
# 10,000,000,000, and neither constrained query scanning:
#
#   quota/extract/bytes                                     bytes EXTRACT jobs write
#   quota/query/alloydb_federated_query_cross_region_bytes  AlloyDB federated queries
#
# Both metric names contain "bytes" and both sit near the query metrics in a
# filtered console list. The most likely explanation is two adjacent rows.
#
# THE UNIT TRAP, which nearly produced a second silent failure
#
# This file's first draft said "10 GiB/day = 10737418240" and would have set
# that. It is wrong, and wrong in the dangerous direction.
#
# The Service Usage API expresses this metric in MEBIBYTES, not bytes. The
# documented default is 200 TiB/day and the API reports it as 209715200 -- and
# 209715200 MiB is exactly 200 TiB, which is what pins the unit down. Setting
# 10737418240 would therefore have set ~10 PiB/day: no meaningful limit at all,
# while reading in the config like a tight one, and reporting success.
#
#   10 GiB/day  =  10 * 1024 MiB  =  10240
#
# The value below is 10240 and was verified AFTER applying: the API now reports
# effectiveLimit 10240 on the per-project bucket. The console displays this
# metric in TiB, a third unit for the same number, which is worth knowing before
# anyone edits it by eye.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------- the intended one
#
# This is the ceiling the README describes. It now exists.
#
# It is a genuinely different kind of control from the ones in this
# repository's Python. `maximum_bytes_billed` in transform/profiles.yml and in
# analytics/bq_safety.py are set BY this codebase, so they share a failure mode:
# they protect against a query being too large, not against this codebase being
# wrong about what it sets. A project quota holds even when the code is wrong,
# which is the property the rest of the README is about.

resource "google_service_usage_consumer_quota_override" "query_usage_per_day" {
  provider = google-beta

  project        = data.google_project.this.number
  service        = "bigquery.googleapis.com"
  metric         = urlencode("bigquery.googleapis.com/quota/query/usage")
  limit          = urlencode("/d/project")
  override_value = "10240" # MiB. 10 GiB/day. See the unit trap above.

  # false to match what the API returns, so `plan` stays empty. `force` is a
  # Terraform-side flag that the API does not store and import cannot recover,
  # so declaring true here produces a permanent one-line diff and destroys the
  # zero-change proof this directory rests on. Creating this override from
  # scratch DOES need force = true, because the value is far below the default
  # and the API asks for confirmation -- set it, apply, set it back.
  force = false

  lifecycle {
    prevent_destroy = true
  }
}

# --------------------------------------------------------- the kept accident
#
# Set by mistake, and KEPT DELIBERATELY -- which is a different thing from
# tolerated. The reasoning, so that it survives being written down:
#
#   An extract job writes table data out to Cloud Storage. Nothing in this
#   project runs one, so the cap has never been felt. But an accidental or
#   hostile full-table export is a real egress path and this is the only
#   ceiling on it. The whole dataset is ~120 MB, so 10 GB is roughly 85x
#   headroom: it cannot bite a legitimate use, and it caps a bad one.
#
# Keeping an accident because it turned out useful is a bad habit. Converting it
# into a decision with a stated reason is not, and that distinction is decision 2
# in docs/DECISIONS.md applied to infrastructure rather than to a taxonomy.
#
# The AlloyDB override was DELETED rather than kept: it constrained federated
# queries against a product this project does not use and never will, so there
# was no reading under which it was a control.

resource "google_service_usage_consumer_quota_override" "extract_bytes" {
  provider = google-beta

  project        = data.google_project.this.number
  service        = "bigquery.googleapis.com"
  metric         = urlencode("bigquery.googleapis.com/quota/extract/bytes")
  limit          = urlencode("/d/project")
  override_value = "10000000000" # bytes. 10 GB/day of extract egress.

  force = false

  lifecycle {
    prevent_destroy = true
  }
}
