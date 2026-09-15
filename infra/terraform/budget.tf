# ---------------------------------------------------------------------------
# The budget alert.
#
# It exists. That sentence is the point of this file.
#
# Before this directory was written, the $5 alert was a thing a note in a
# handoff document said had been configured on 2026-09-11. It could not be
# checked, because `billingbudgets.googleapis.com` was not enabled on the
# project and listing budgets without it returns a permission error that reads
# like a missing grant rather than a missing API. So it sat in the same category
# as the IAM grant in main.tf: an asserted control that nothing had verified.
#
# The difference is the outcome. Enumerating found the IAM grant did not exist
# and the query quota was on the wrong metric. This one was exactly as described:
# $5 monthly, thresholds at 50/90/100/150 percent, all credits included. Two out
# of three is not a good ratio, and it is the argument for checking rather than
# for assuming the check is a formality.
#
# SCOPE, which is worth being precise about.
#
# `budget_filter` carries no `projects` entry, so this budget covers the WHOLE
# BILLING ACCOUNT, not just olist-customer-intelligence. For a billing account
# with one project those are the same number; they stop being the same number
# the moment a second project is created under it, and the alert would then be
# measuring something other than what its name suggests. Left as it is because
# that is what exists, and narrowing it is a change rather than a transcription.
#
# WHAT A BUDGET IS AND IS NOT
#
# It notifies. It does not cap. Nothing about crossing $5 stops a query, a
# Gemini call, or anything else -- which is precisely why the quota override in
# quotas.tf matters, and why the Gemini spend is tracked by a ledger in the
# application rather than by trusting this.
# ---------------------------------------------------------------------------

resource "google_billing_budget" "monthly_alert" {
  provider = google.billing

  billing_account = var.billing_account
  display_name    = "$5 Monthly Budget Alert"

  budget_filter {
    calendar_period        = "MONTH"
    credit_types_treatment = "INCLUDE_ALL_CREDITS"
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = "5"
    }
  }

  # 150% is included on purpose and is not a rounding artifact: the alert that
  # matters is not the one at the limit, it is the one that says the limit was
  # crossed and spending continued.
  threshold_rules {
    threshold_percent = 0.5
  }
  threshold_rules {
    threshold_percent = 0.9
  }
  threshold_rules {
    threshold_percent = 1.0
  }
  threshold_rules {
    threshold_percent = 1.5
  }

  lifecycle {
    prevent_destroy = true
  }
}
