#!/usr/bin/env bash
#
# Create the read-only service account the NL->SQL agent runs as.
#
#   bash scripts/setup_agent_sa.sh olist-customer-intelligence olist_marts
#
# NOT RUN BY THIS REPOSITORY. It creates an identity and, optionally, a key, and
# neither is something a script should do on someone's project without them
# watching. Run it yourself, or run the gcloud lines by hand -- they are short
# enough to read.
#
# WHY IAM AND NOT AN ALLOWLIST
# ----------------------------
# analytics/sql_guard.py parses the agent's SQL and rejects anything that is not
# a single SELECT, and analytics/bq_safety.py caps the bytes. Both are real
# controls and neither is the boundary. They run inside the application, so they
# protect against the model behaving badly and not against the application
# behaving badly -- a bug in the guard, a prompt injection that reaches an
# unguarded code path, a future contributor adding a second query helper that
# forgets to call it.
#
# The boundary is that the identity executing the SQL cannot write, and cannot
# read anything outside the marts, no matter what SQL it is handed. That is a
# property of the grant rather than of the code, so it survives the code being
# wrong.
#
# SCOPE, PRECISELY
# ----------------
#   roles/bigquery.jobUser     PROJECT level. Required to run any query at all;
#                              it confers no data access on its own.
#   roles/bigquery.dataViewer  DATASET level, on the marts only. NOT project
#                              level -- that is the mistake this script exists
#                              to avoid, and it is one word's difference.
#
# Deliberately NOT granted: dataEditor, dataOwner, user, admin, and any grant on
# olist_raw or olist_staging. The agent has no reason to read the raw extract,
# and the raw dataset is where the personally-identifying columns live.

set -euo pipefail

PROJECT="${1:?usage: setup_agent_sa.sh PROJECT MARTS_DATASET}"
DATASET="${2:?usage: setup_agent_sa.sh PROJECT MARTS_DATASET}"
SA_NAME="olist-nl2sql-reader"
SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

echo "Project : ${PROJECT}"
echo "Dataset : ${DATASET}"
echo "Identity: ${SA_EMAIL}"
echo

gcloud iam service-accounts create "${SA_NAME}" \
  --project="${PROJECT}" \
  --display-name="Olist NL->SQL agent (read-only, marts only)" \
  --description="Runs agent-authored SELECTs. dataViewer on ${DATASET} only." \
  || echo "  (already exists)"

# Project-level: run jobs. No data access comes with this.
gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/bigquery.jobUser" \
  --condition=None

# Dataset-level: read the marts, and nothing else. Done through the dataset's
# access list rather than a project binding, because a project binding would
# grant the raw dataset too and would look almost identical in a review.
TMP="$(mktemp)"
bq show --project_id="${PROJECT}" --format=prettyjson "${PROJECT}:${DATASET}" > "${TMP}"
python - "${TMP}" "${SA_EMAIL}" <<'PY'
import json, sys
path, email = sys.argv[1], sys.argv[2]
with open(path) as fh:
    dataset = json.load(fh)
access = dataset.setdefault("access", [])
if not any(a.get("userByEmail") == email for a in access):
    access.append({"role": "READER", "userByEmail": email})
    with open(path, "w") as fh:
        json.dump(dataset, fh)
    print(f"  added {email} as READER")
else:
    print(f"  {email} already has READER")
PY
bq update --project_id="${PROJECT}" --source="${TMP}" "${PROJECT}:${DATASET}"
rm -f "${TMP}"

echo
echo "Done. Verify the scope is what you think it is:"
echo
echo "  gcloud projects get-iam-policy ${PROJECT} \\"
echo "    --flatten='bindings[].members' \\"
echo "    --filter='bindings.members:${SA_EMAIL}' --format='table(bindings.role)'"
echo
echo "Expect exactly roles/bigquery.jobUser. Anything else at project level is"
echo "broader than intended."
echo
echo "A key is NOT created here. Prefer workload identity federation or an"
echo "attached service account; if the deployment genuinely needs a JSON key,"
echo "create it yourself and keep it out of this repository -- .gitignore already"
echo "refuses service-account*.json, gcp-key*.json and *.json generally."
