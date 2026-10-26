# Security and responsible disclosure

This repository is an engineering preview, not a production-certified research service.

## Safe local operation

- Do not commit API keys, `.env*` variants, database credentials, private key material, provider response bodies or proprietary research content.
- Use independent, migrated development databases for integration tests. Never enable database tests against production.
- External LLM / search provider calls may incur costs; keep the backends disabled unless intentionally configured.
- Treat research sources and retrieved webpages as untrusted input.
- Search candidates and metadata are **not** verified evidence.

## Reporting vulnerabilities

Please do not paste credentials or vulnerability exploitation details into a public issue. Contact the repository owner privately through an appropriate channel once the repository has been published and a contact method is designated.
