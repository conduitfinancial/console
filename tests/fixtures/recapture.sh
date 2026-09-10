#!/usr/bin/env bash
# Recapture live requirements fixtures. Read-only GETs. Run from this directory.
# Env: CONDUIT_HOST (default staging), CONDUIT_API_KEY (or sources ../../..../.env fallback).
set -euo pipefail
cd "$(dirname "$0")"

if [[ -z "${CONDUIT_API_KEY:-}" ]] && [[ -f ../../../.env ]]; then
  set -a; source ../../../.env; set +a
  CONDUIT_API_KEY="${SANDBOX_API_KEY:?no key}"
  CONDUIT_HOST="${CONDUIT_HOST:-$SANDBOX_HOST}"
fi
B="${CONDUIT_HOST:-https://api.staging.conduit.financial}/v2"
H="x-api-key: ${CONDUIT_API_KEY:?set CONDUIT_API_KEY}"
echo "capturing from $B"

for c in BGR ITA BRA USA; do
  curl -sfL -H "$H" "$B/onboarding/requirements?country=$c" -o "onboarding_requirements_$c.json"
done
curl -sfL -H "$H" "$B/onboarding/policy-subjects?axis=INDUSTRY" -o policy_subjects_industry.json
curl -sfL -H "$H" "$B/onboarding/policy-subjects?axis=REGULATED_ACTIVITY" -o policy_subjects_regulated_activity.json
curl -sfL -H "$H" "$B/payouts/requirements?purpose=payment_for_goods_or_services&rail=fedwire&recipientType=business&destinationCountry=USA" -o payout_requirements_fedwire_business.json
curl -sfL -H "$H" "$B/payouts/requirements?purpose=intercompany&rail=fedwire&recipientType=business&destinationCountry=USA" -o payout_requirements_fedwire_intercompany.json
curl -sfL -H "$H" "$B/payouts/requirements?purpose=payment_for_goods_or_services&rail=swift&recipientType=business&destinationCountry=DEU" -o payout_requirements_swift_business.json
curl -sfL -H "$H" "$B/payouts/requirements?purpose=payment_for_goods_or_services&rail=ach&recipientType=individual&destinationCountry=USA" -o payout_requirements_ach_individual.json
curl -sfL -H "$H" "$B/payouts/requirements?purpose=payment_for_goods_or_services&rail=sepa&recipientType=business&destinationCountry=DEU" -o payout_requirements_sepa_business.json

CID=$(curl -sfL -H "$H" "$B/customers?limit=1" | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"][0]["id"])')
curl -sfL -H "$H" "$B/customers/$CID/features/requirements?type=virtual_account&asset=USD" -o feature_requirements_virtual_account_usd.json
# EUR: currently 422 NO_ELIGIBLE_PROVIDER on this org — captured deliberately as the problem-detail fixture (-f omitted)
curl -sL -H "$H" "$B/customers/$CID/features/requirements?type=virtual_account&asset=EUR" -o problem_detail_422_no_eligible_provider.json

for f in onboarding_requirements_*.json feature_requirements_*.json; do
  v=$(python3 -c "import json; print(json.load(open('$f'))['schemaVersion'])")
  [[ "$v" == "3" ]] || echo "WARNING: $f schemaVersion=$v (expected 3) — review FORM_ENGINE_SPEC.md before trusting"
done
echo "done: $(ls *.json | wc -l | tr -d ' ') fixtures"
