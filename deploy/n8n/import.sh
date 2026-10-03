#!/bin/sh
# Import credentials + workflows and publish (activate) each workflow. Idempotent.
set -e
n8n import:credentials --input=/import/credentials.json
n8n import:workflow --separate --input=/import/workflows
for f in /import/workflows/*.json; do
  id=$(node -e "process.stdout.write(require('$f').id)")
  n8n publish:workflow --id="$id"
done
