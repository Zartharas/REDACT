# The three-tier sensitivity taxonomy and its transformation logic

Manuscript revision support (minor suggestion: "provide a more detailed
explanation of the three-tier sensitivity taxonomy and associated
transformation logic"). This is a reference extracted directly from the
live, working code (`src/anonymize.py`, `config/policy.json`), not a
redescription of an abstract design — every mapping and behavior below is
what the shipped system actually does today, with the exact source line
for each claim. No new engineering; this exists so the manuscript can cite
the real implementation precisely rather than paraphrase it approximately.

## 1. The taxonomy, as it actually runs

The pipeline diagram (`README.md`) names the three tiers **Critical**,
**Sensitive**, and **Public/low-risk**, each routed to a different
transformation:

```
Taxonomy router (Critical / Sensitive / Public)
  Critical      -> Reversible tokenization
  Sensitive     -> Keyed pseudonymization
  Public/low-risk -> Redaction
```

The router itself is `anonymize.anonymize_by_policy()` (`src/anonymize.py`,
line 1281): every detected span is checked against three type sets —
`REDACT_TYPES`, `PSEUDONYMIZE_TYPES`, `TOKENIZE_TYPES` — and transformed
accordingly in a single right-to-left pass so span offsets stay valid
regardless of which action touches which span. **A span whose type matches
none of the three sets is left completely untouched** (`transform()`'s final
`return original` branch) — a deliberate fail-*open* choice at this single
point, distinct from the fail-*closed* check described in Section 4. This
matters: it means the router itself trusts the policy to be complete: if a
new canonical PII type were ever added to `detect.py` without a
corresponding policy-file entry, spans of that type would pass through
`anonymize_by_policy()` silently unredacted at *runtime*. `load_policy()`
(Section 4) is what prevents that specific scenario — but it prevents it by
refusing to *start* the service with an incomplete policy, not by anything
in the router itself. Both mechanisms are needed; neither alone is
sufficient, and that's worth stating plainly rather than implying the
runtime check alone is the safety net.

## 2. Current type-to-tier mapping (configurable, not hardcoded)

As of `config/policy.json` (the live default, externalized in Phase 1 of the
detection/policy decoupling work — see `DETECTION_POLICY_DECOUPLING_SCOPING.md`):

| Type | Tier | Action | Reversible? |
|---|---|---|---|
| EMAIL | Critical | Tokenize | Yes, via TokenStore |
| MRN | Critical | Tokenize | Yes, via TokenStore |
| CREDIT_CARD | Critical | Tokenize | Yes, via TokenStore |
| SSN | Critical | Tokenize | Yes, via TokenStore |
| IP | Sensitive | Pseudonymize | No (one-way HMAC) |
| PERSON | Sensitive | Pseudonymize | No (one-way HMAC) |
| *(none currently)* | Public/low-risk | Redact | N/A, value destroyed |

**This mapping is a deployment-time configuration choice, not a fixed
property of the code** — `anonymize.load_policy()` (Section 4) loads it from
an external JSON file, hot-reloadable at runtime (Phase 2) via an
authenticated admin API, separately from any code change or redeploy. The
table above describes the shipped default, not an architectural constant.
`REDACT_TYPES` is empty in the shipped default because none of the six
ground-truth types this corpus exercises warrant outright destruction — but
the mechanism is fully live and documented for fields that would (the
codebase's own example: free-text password-reset email bodies, where no
downstream investigative value justifies keeping even a pseudonymized
trace).

**Fail-closed validation, not just a default file:** `load_policy()`
requires every one of `CANONICAL_TYPES` (`{PERSON, EMAIL, IP, SSN,
CREDIT_CARD, MRN}` — the exact six types `detect.py`'s ensemble claims) to
appear in exactly one of the three sets. A type missing from all three, or
present in more than one, raises `PolicyConfigError` and stops the process
at startup — deliberately chosen over silently running with an incomplete
policy, since an unenforced control missing no visible error is exactly
what a GDPR Article 32 audit would flag (comment directly in
`config/policy.json`). A genuinely *missing* policy file is handled
differently and intentionally: it fails *open* to the built-in in-code
defaults above (not a regression, since those defaults are themselves a
complete, valid policy) rather than refusing to start — the distinction
being invalid-and-present (fail closed, trust nothing) versus
absent-and-therefore-using-a-known-good-default (fail open, trust the
shipped default).

## 3. What each transformation actually does, mechanically

**Redact** (`redact()`, `src/anonymize.py`): replaces the matched span with
a fixed placeholder, `"[REDACTED]"`. Irreversible by construction — nothing
of the original value survives in the output in any form, and (measured
directly, see `validation/investigative_utility/`) every value of a given
type collapses to the *same* placeholder, meaning two different real
entities become indistinguishable from each other downstream, not merely
unlinkable — an active, not passive, loss of information.

**Pseudonymize** (`pseudonymize()` / `anonymize_by_policy()`'s inline
transform): `hmac.new(key, original, sha256).hexdigest()`, truncated and
prefixed with the entity type (e.g. `pers_a1b2c3...`). Deterministic per
key — the same real value always produces the same output, so cross-record
correlation is preserved (measured: 0% false-linkage rate across all six
entity types, `validation/investigative_utility/`) — but genuinely one-way:
no reverse map is ever stored anywhere, by design. **This is not the same
guarantee as anonymization under GDPR.** The EDPB's Guidelines 01/2025 on
Pseudonymisation (superseding reliance on the 2014 Article 29 WP opinion
alone) is explicit that pseudonymized data remains personal data in full
GDPR scope: for a low-entropy input space (an IP address, a name drawn from
a finite population), an attacker or investigator holding the key doesn't
need to invert the hash — brute-forcing candidates against a fast keyed
hash is tractable. `validation/investigative_utility/` measured this
directly as a real capability, not a theoretical risk: given a short
candidate list and the key, an investigator correctly identified the true
value against decoys in 30/30 trials (100%) — a materially different, much
more realistic capability than blind reversal, and the reason pseudonymized
IP/PERSON fields stay classified as personal data rather than anonymized
data.

**Tokenize** (`tokenize()` + `TokenStore`): `TokenStore.get_or_create_token()`
mints a token (`tok_<type>_<keyed-hash>`) and stores the forward
(original→token) and reverse (token→original) mapping in a
`StorageProvider` — `FileStorageProvider` (default, WAL-based incremental
writes after a real measured O(n)-per-call bottleneck was found and fixed —
see `DETECTION_PERFORMANCE_TRADEOFFS.md` Section 7), `RedisStorageProvider`
(shared backend across multiple service replicas, verified for both
single-process and real multi-process concurrent-write safety), or a
`HashiCorpVaultStorageProvider` (interface-ready, logic-verified against a
mocked client, not yet run against a live Vault server — disclosed as such,
not oversold). Reversal is genuine and direct: `TokenStore.resolve(token)`
or the standalone `detokenize()` function, given store access — measured at
100% direct reversal in `validation/investigative_utility/`, the one
transformation of the three that actually delivers investigator-recoverable
values, not just investigator-linkable ones.

## 4. A naming collision worth disambiguating, found while assembling this reference

**`drift.py`'s `CRITICAL_TYPES`/`SENSITIVE_TYPES` are not the same tiers as
the anonymization taxonomy's Critical/Sensitive, despite using identical
words.** `drift.py` (line 22) defines `CRITICAL_TYPES = {"PERSON", "EMAIL",
"SSN", "CREDIT_CARD", "MRN"}` and `SENSITIVE_TYPES = {"IP"}` — but these
control drift-*alert* severity weighting (how urgently a taxonomy-drift
finding should be treated), a completely different axis from
`anonymize_by_policy()`'s Critical→tokenize / Sensitive→pseudonymize
routing. Concretely: **PERSON is "Critical" for drift-alerting purposes but
"Sensitive" (not "Critical") for anonymization-routing purposes** — the
same word, two independently-designed classification systems, in the same
codebase, that happen to overlap on some types (EMAIL/SSN/CREDIT_CARD/MRN
land in "Critical" under both systems) and diverge on PERSON specifically.
This is a real finding, not a defect requiring a fix before the deadline —
both systems work correctly on their own terms — but it is a genuine
disambiguation the manuscript should make explicitly if it discusses both
the anonymization taxonomy and drift detection in the same section, since a
reader who assumes one shared "Critical" tier across both would draw an
incorrect conclusion about PERSON's classification.

## 5. Worked example, end to end

Input (windows_event, from `data/synthetic_logs.jsonl`):
```
EventID=4625 LogonType=3 TargetUserName=Timothy Wong FailureReason=%%2313 IpAddress=142.14.44.201 Status=0xC000006D
```

Detected spans: `PERSON` ("Timothy Wong"), `IP` ("142.14.44.201") — both
route to **Sensitive → Pseudonymize** under the current policy. Output
below is the real, computed result of calling `anonymize.pseudonymize()`
directly against this exact line (not an illustrative/invented string):

```
EventID=4625 LogonType=3 TargetUserName=pers_57ffa3c98253255d865bf82a16d6e2ef FailureReason=%%2313 IpAddress=ip_52e38cc05a39e6b4d0f0f8bdd52476ac Status=0xC000006D
```

Both fields are now: (a) correlatable — the same `Timothy Wong` or
`142.14.44.201` anywhere else in the corpus produces the identical token,
confirmed at 100% correlation recall; (b) not directly reversible by
anyone reading this output alone; (c) still classified as personal data
under GDPR per Section 3's EDPB citation, requiring the same handling as
the original. Had either field instead been an SSN or CREDIT_CARD (Critical
→ Tokenize under the current policy), the equivalent output would be
recoverable via `TokenStore.resolve()` given authorized store access — the
concrete difference Section 3 and `validation/investigative_utility/`
quantify.

## Empirical grounding — this taxonomy's design choices are measured, not asserted

- **Why IP and internal-vs-external ranges matter to the taxonomy design**:
  `DETECTION_PERFORMANCE_TRADEOFFS.md` Section 2 — all 2,626 IP false
  positives on the main corpus trace to one hardcoded internal address,
  a direct, measured demonstration of why internal-infrastructure and
  external/customer IPs arguably warrant different sensitivity tiers,
  not just a plausible-sounding taxonomy detail.
- **Why the Tokenize vs. Pseudonymize distinction is not merely
  mechanical**: `validation/investigative_utility/README.md` — the
  correlation/reversibility experiment measuring exactly what each tier's
  transformation costs and preserves, referenced throughout Sections 3-5
  above.
