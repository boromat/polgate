---
name: pii-safety-review
description: Review a diff or module for PII leaks: raw values reaching the LLM provider, logs, audit rows, templates or unencrypted storage. Use before committing anything in polgate/llm, polgate/web, polgate/audit, polgate/vault or when asked to "check for leaks" or "security review".
argument-hint: [path or "staged"]
---

# PII safety review: $ARGUMENTS

The whole project exists for one guarantee: original values never leave the process except to the employee
who typed them. Review with that lens. Scope: `$ARGUMENTS` (default: `git diff --cached`).

## Trace every outbound path
1. **Provider payloads** (`polgate/llm/openai_compat.py`, `router.py`): every `messages[*].content` must come
   from `AnonymizationResult.anonymized`, `Message.content_sent`, `FileAsset.text_sent`, or `SYSTEM_PROMPT`.
   Confirm `assert_no_raw_values(messages, mapping)` runs before `stream_chat`. Filenames also pass the pipeline.
2. **Judge calls** (polanon's `polanon.judge`, built by `polgate/judge_factory.py`): input is stage-1
   masked text only. `RemoteJudge` sends masked text to the cloud; the `message_sent` audit row must
   record `judge=remote` (`_judge_detail` in `polgate/llm/router.py`), `polgate/judge_factory.py` must
   log its warning, and the docs must call it non-local. A leak inside polanon itself is fixed in the
   [polanon repository](https://github.com/boromat/polanon), not patched here.
3. **Logs**: `grep -rn "logger\.\|logging\." polgate` and check no call formats `text`, `original`,
   `value`, `mapping` or a decrypted field. Log lengths, counts, hashes, ids.
4. **Audit rows**: `AuditEvent.detail` and `entity_counts_json` hold counts/hashes only. `audit.log` should raise
   if `detail` contains any mapping value.
5. **Storage**: `Message.content_original_enc`, `FileAsset.text_original_enc`, `VaultEntry.value_enc` are written
   only through `vault.encrypt`. No new plaintext columns holding originals. Uploaded bytes live under
   `settings.upload_dir` with a uuid name, never served without the owner check.
6. **Templates**: original text is rendered only for `user.id == conversation.user_id` (or admin only if a policy
   explicitly allows it, and then audited). Everything is autoescaped; `|safe` only on `highlight_tokens` output.
7. **Errors and exceptions**: exception messages that bubble to logs/UI must not embed the offending text
   (e.g. `LLMError` may include status and provider body, which contains tokens only, never originals).
8. **Tests**: at least one test per outbound path asserts absence of every mapping value in the payload.

## Also check the browser side
Original text reaches one more place: the employee's own page. A leak there is still a leak.
9. **Injected script**: no inline `<script>`, `on*=` handler, `hx-on` or `style=` attribute in a template.
   They would force `unsafe-inline` into the Content-Security-Policy in `polgate/security.py`, and with it
   any injected markup could read the decrypted page. `tests/test_security.py` enforces this.
10. **Redirects**: any target built from user input goes through `safe_next`, or the login page becomes an
    open redirect. Never put text, a filename or a token in a query string; the flash whitelist in
    `polgate/rendering.py` exists for exactly that reason.
11. **State-changing GET**: mutations are POST, so the origin check and `SameSite=Lax` apply to them.

## Output
List findings as `file:line — risk — fix`. Severity: LEAK (blocks commit) / WEAK (fix soon) / NOTE.
If there are zero LEAK findings, say so explicitly.
