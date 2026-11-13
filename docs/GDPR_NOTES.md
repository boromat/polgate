# How the hub relates to GDPR concepts

**Not legal advice.** This is an engineering note describing which technical measures the hub provides
and which questions remain for the company and its counsel. Deploying this software does not make an
organisation compliant with anything.

## Pseudonymisation, not anonymisation

GDPR Article 4(5) defines pseudonymisation as processing personal data so it can no longer be attributed
to a person without additional information, kept separately and protected. That is exactly what the hub
does: the provider receives tokens, and the mapping back to the person stays in the encrypted vault on
the company's own machine.

Pseudonymised data is still personal data. The hub does not take the processing outside the scope of the
regulation. It reduces what a processor receives, which is a data-minimisation measure under Article
5(1)(c) and a security-of-processing measure under Article 32.

Anonymisation, by contrast, would be irreversible. The hub is deliberately reversible, because an answer
about `<OSOBA_1>` is not useful to the employee.

## Which technical measures map to which obligations

| Concern | What the hub provides |
|---|---|
| Data minimisation, Art. 5(1)(c) | Detected values leave the network only as tokens, enforced in code and tests; what detection misses is not covered (`docs/THREAT_MODEL.md`). The search in long attachments sends each turn only the passages that match the question rather than a fixed part of the file |
| Security of processing, Art. 32 | Fernet encryption at rest, per-account access, argon2id passwords |
| Storage limitation, Art. 5(1)(e) | Admin-set retention for both the vault mapping and the message and file history, applied by an hourly job that deletes rows and the stored bytes |
| Accountability, Art. 5(2) | Audit trail of what was masked, when, by whom, to which provider |
| Records of processing, Art. 30 | The audit log supplies the factual basis; the register itself is yours |

## Questions the software cannot answer for you

- **Legal basis.** Why is this personal data being processed at all, and does using an AI assistant fall
  within that basis and the original purpose?
- **The provider relationship.** A processor agreement under Article 28 is still needed, along with a
  transfer mechanism under Chapter V when the provider is outside the EEA. Pseudonymised data crossing
  the border is still a transfer.
- **The embeddings endpoint, when it is not local.** The search in long attachments sends masked
  passages of every long file and masked questions to `EMBEDDING_BASE_URL`, or to the chat provider
  when that is empty. A local endpoint on the hub's server is no new recipient. A remote one is a
  processor in its own right, with the same Article 28 agreement and Chapter V questions as the chat
  provider, and it receives the whole masked text of a long file rather than its first part.
- **DPIA.** Article 35 may require an assessment. The threat model in `docs/THREAT_MODEL.md` is useful
  input, not a substitute.
- **Data subject rights.** Erasure and access requests must reach the hub's history and vault as well as
  everything else. Retention deletes on a schedule; there is still no built-in per-subject export or
  deletion, so an individual request means finding the conversations by hand.
- **The audit trail is personal data too.** It records who did what and when, and a failed sign-in
  records the account it was aimed at, or, for a name that matches no account, only its SHA-256,
  which is necessary for the lockout and for investigating an intrusion. It holds no masked values,
  only types, counts and hashes, but it still needs a retention decision of its own; the purge job
  deliberately leaves it alone.
- **Retention values.** The defaults here are arbitrary. The right numbers come from your own policy.
- **Special categories.** Health, biometric and similar data under Article 9 need a stricter analysis
  than "the guards will mask most of it".

## EU AI Act

For most companies this hub is a deployer-side tool rather than a high-risk AI system. Transparency
duties under Article 50 apply to the chatbot experience, and the obligations that became applicable on
2 August 2026 are worth reviewing with counsel before rolling the hub out widely.
