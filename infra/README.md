# Infrastructure

Azure resources defined as code (Bicep or Terraform, TBD) so the whole setup can be recreated in DSU's Azure account at handoff.

Keep resource names, region, and subscription as parameters, never hardcoded.

Planned resources: App Service (API), Azure AI Search (index), a scheduled job for ingestion, Key Vault (secrets), and logging.
