#!/bin/bash
# Call a shared-secret site endpoint from a Claude sandbox through the site-curl workflow
# (the sandbox can't reach thekahlahouse.com; the Actions runner can and holds the key).
# usage: site_curl.sh <path> "<query, no leading &>" <out.json>   — writes the JSON reply to out.json
# Run calls ONE AT A TIME: it reads back the newest site-curl run.
# REST only (Nov 16 2026): `gh workflow run` / `gh run` go through GraphQL, which Claude
# sandboxes block — the dispatch, run list and job log here are all plain REST calls.
set -u
R=repos/Diavel78/kahla-house
prev=$(gh api "$R/actions/workflows/site-curl.yml/runs?per_page=1" --jq '.workflow_runs[0].id')
gh api -X POST "$R/actions/workflows/site-curl.yml/dispatches" -f ref=main \
  -f "inputs[path]=$1" -f "inputs[query]=$2" -f 'inputs[full]=1' >/dev/null
for i in $(seq 1 90); do
  r=$(gh api "$R/actions/workflows/site-curl.yml/runs?per_page=1" --jq '.workflow_runs[0] | "\(.id) \(.status)"')
  id=${r% *}; st=${r#* }
  [ "$id" != "$prev" ] && [ "$st" = completed ] && break
  sleep 4
done
j=$(gh api "$R/actions/runs/$id/jobs" --jq '.jobs[0].id')
gh api "$R/actions/jobs/$j/logs" > "$3.txt"
python3 - "$3.txt" "$3" <<'PY'
import json, re, sys
lines = open(sys.argv[1]).read().splitlines()
lines = [re.sub(r'^\S+Z ', '', re.sub(r'^[^\t]*\t[^\t]*\t', '', l)) for l in lines]
t = ''.join(lines)
i = t.find('{"')
d, _ = json.JSONDecoder(strict=False).raw_decode(t[i:])
json.dump(d, open(sys.argv[2], 'w'))
PY
