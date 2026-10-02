# Security model

## Assets

* The ability to change a service (restart, scale, roll back).
* Approval challenges and execution capabilities.
* Secrets in configuration (API keys, database password, signing keys).
* Alert payloads and logs, which may contain credentials or personal data.

## Threats and controls

| Threat | Control | Verified by |
|---|---|---|
| Approving without a token, with an empty or wrong token | `TokenBody.token` is required and non-empty; HMAC comparison with `compare_digest` | `test_missing_approval_token_is_rejected`, `test_empty_approval_token_is_rejected`, `test_wrong_approval_token_is_rejected_without_execution` |
| Anonymous approval | HTTP Basic login on every decision, action, event, trace, and demo-control endpoint | `test_unauthenticated_user_cannot_approve`, `test_private_endpoints_require_login` |
| Token replay | Challenge nonce cleared on decision; capability `used_at` set atomically | `test_token_cannot_be_replayed`, `test_exact_approved_action_succeeds_once_then_replay_fails` |
| Double execution under concurrency | Optimistic locking on `actions.version` | `test_concurrent_approvals_execute_exactly_once` |
| Approval reused for a different action, tool, arguments, or target | Capability bound to action ID, tool, normalized-argument hash, service, scope | `tests/test_tools.py`, `test_changed_arguments_after_approval_fail` |
| Original approval reused for rollback | Separate `rollback` scope and fresh nonce | `test_original_execution_token_cannot_approve_rollback` |
| Stale approvals | Challenges and deferred capabilities expire; the worker marks them `expired` | `test_expired_approval_window_is_refused`, `test_expired_capability_fails` |
| Token leakage through the API, logs, or traces | `public_view` strips challenge data; the redactor removes sensitive keys and secret patterns; only capability hashes are stored | `test_action_endpoints_do_not_return_approval_token`, `test_logs_never_contain_approval_secrets`, `test_langfuse_receives_redacted_spans`, `test_only_capability_hash_is_stored` |
| Cross-site request forgery on approvals | State-changing endpoints require `application/json` (a cross-site form cannot send it without a CORS preflight, which is never granted), and the challenge token is only readable on the same origin | `test_post_without_json_content_type_is_refused` |
| Script injection through alert text | Jinja2 autoescaping, values passed to script via `data-` attributes, nonce-based Content Security Policy | `test_approval_page_escapes_untrusted_alert_text` |
| Prompt injection in alerts, logs, or runbooks | Untrusted text is fenced as data; the policy check rejects anything outside the allowed tools, target, replica rule, and retrieved citations | `test_prompt_injection_cannot_exceed_policy`, planner evaluation (injection cases) |
| Model invents a citation | Citation must be a retrieved chunk that mentions the proposed tool | `test_invented_citation_is_rejected_and_escalated` |
| Path traversal or odd service names | Strict service-name pattern at ingress, in tool schemas, and an allow-list | `test_invalid_service_names_are_rejected_at_ingress`, `test_path_traversal_service_is_rejected` |
| Arbitrary command execution | No shell tool; diagnostics are an allow-list of four argument-free names, simulated | `test_diagnostic_allowlist` |
| Forged alerts | HMAC-SHA256 webhook signature (required in production) | `test_webhook_signature_is_required_when_configured`, `test_tampered_body_fails_signature` |
| Flooding | Per-client rate limit (429) | `test_rate_limit_returns_429` |
| Deploying with development secrets | `AEGIS_ENV=production` refuses default secret key, missing webhook secret, missing approvers, or SQLite | `test_production_refuses_insecure_defaults` |
| Secrets sent to the model provider | Alert and log text redacted before the prompt; the API key is never in the prompt | `test_model_never_sees_approval_secrets_or_api_keys` |
| Vulnerable or tampered dependencies | Hash-locked requirements, `pip-audit` in CI | CI `security` job |
| Committed secrets | gitleaks scan of the full history in CI | CI `security` job |
| Container breakout impact | Non-root user (uid 10001), no Docker socket, no cloud credentials | CI `container` job checks the uid |

## Residual risks (accepted for a simulator demo)

* HTTP Basic authentication; production should use OIDC with roles and
  separation of duties (the approver should not be the alert author).
* The rate limiter is per process.
* Pattern-based redaction can miss unusual secret formats.
* Approval challenges are shown on the approval page to any authenticated
  approver; there is no per-environment authorization yet.

## Reporting

Please open a private security advisory on the repository rather than a public issue.
