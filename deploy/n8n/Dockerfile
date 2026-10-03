# n8n with the Nordlys workflows and credentials for the import init container (Azure).
FROM n8nio/n8n:2.41.6
COPY --chown=node:node import.sh credentials.json /import/
COPY --chown=node:node workflows /import/workflows
