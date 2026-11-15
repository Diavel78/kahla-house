#!/bin/bash
# Call a shared-secret site endpoint from a Claude sandbox through the site-curl workflow
# (the sandbox can't reach thekahlahouse.com; the Actions runner can and holds the key).
# usage: site_curl.sh <path> "<query, no leading &>" <out.json>   — writes the JSON reply to out.json
# Run calls ONE AT A TIME: it reads back the newest site-curl run.
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
gh workflow run site-curl.yml -R Diavel78/kahla-house --ref main -f path="$1" -f query="$2" -f full=1 >/dev/null 2>&1
sleep 5
id=$(gh run list -R Diavel78/kahla-house -w site-curl.yml -L 1 --json databaseId -q '.[0].databaseId')
until [ "$(gh run view "$id" -R Diavel78/kahla-house --json status -q .status)" = completed ]; do sleep 3; done
gh run view "$id" -R Diavel78/kahla-house --log > "$3.txt"
python3 - "$3.txt" "$3" <<'PY'
import json, re, sys
lines = open(sys.argv[1]).read().splitlines()
lines = [re.sub(r'^\S+Z ', '', re.sub(r'^[^\t]*\t[^\t]*\t', '', l)) for l in lines]
t = ''.join(lines)
i = t.find('{"')
d, _ = json.JSONDecoder(strict=False).raw_decode(t[i:])
json.dump(d, open(sys.argv[2], 'w'))
PY
