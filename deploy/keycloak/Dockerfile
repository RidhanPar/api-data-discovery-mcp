# Keycloak with the Nordlys realm baked in (Azure). Secrets in the realm are placeholders
# resolved from environment variables at import time; in Azure those come from Key Vault.
FROM keycloak/keycloak:26.4
COPY nordlys-realm.json /opt/keycloak/data/import/nordlys-realm.json
