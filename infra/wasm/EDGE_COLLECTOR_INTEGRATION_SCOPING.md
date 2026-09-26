# Edge collector Wasm integration: scoping doc (Task #49)

Follow-on to Task #48's standalone `wasm/layer1_4/` module. This is a
scoping document only, per that task's own description — no
integration build was attempted here. Written specifically to check
Task #49's own stated starting assumption against current reality
before committing engineering time to it.

## The task's premise didn't hold up, so the recommendation changed

Task #49 was written recommending Vector "since it has the most
straightforward Wasm/VRL extension story." That's **not accurate as of
this writing (checked via web search, 2026-08-11, not assumed from
training-data familiarity)**:

- **Vector removed its `wasm` transform in v0.17.0 (October 2021)** and
  has not reintroduced it since — the deprecation announcement itself
  states the team considered the experiment worthwhile but decided
  against continuing investment, citing a real performance penalty and
  a lack of documentation/SDKs, given low observed usage. No search
  result from 2022 onward shows this reversed. VRL (Vector Remap
  Language) is itself *compiled to* Wasm for the browser-based VRL
  Playground — that's Wasm being used to ship Vector's own scripting
  language into a browser demo, not a mechanism for a third party to
  load a custom Wasm plugin into a running Vector pipeline. These are
  easy to conflate from search results alone; confirmed the distinction
  by reading the actual removal announcement, not just a summary of it.
- **Fluent Bit has real, current, functional Wasm plugin support** for
  input and filter plugins (confirmed via `docs.fluentbit.io`'s own
  developer docs, fetched directly rather than summarized secondhand)
  — documented as "under development but functional," present across
  multiple recent doc versions (2.0 through 3.2+), with a defined C-ABI
  function signature, supported toolchains (Rust `wasm32-unknown-unknown`,
  TinyGo `wasm32-wasi`, WASI SDK), and real example filters (Rust, C,
  Go) in Fluent Bit's own repo.
- **Envoy** has proxy-wasm (a mature, widely-used extension ABI — Istio
  and other service meshes build on it) but it operates at the
  network-proxy layer (HTTP/TCP filter chains), not on log files the
  way this project's pipeline does. It's a plausible target for a
  genuinely different future use case (scrubbing PII in HTTP request/
  response bodies as they transit a mesh, before they're ever logged)
  but is a mismatch for "edge-scrub a log file before Logstash/Fluent
  Bit ships it," which is this project's actual stated scope. Not
  investigated further here — worth its own separate scoping doc if
  that different use case is ever prioritized.

**Recommendation: Fluent Bit, not Vector.** This directly reverses
Task #49's own starting assumption — worth stating plainly, since
building against Vector's actual current state (no Wasm plugin
mechanism at all) would have been unbuildable, not just harder.

## What Fluent Bit's Wasm filter interface actually requires

Confirmed from `docs.fluentbit.io/manual/fluent-bit-for-developers/wasm-filter-plugins`
directly (fetched 2026-08-11), not inferred:

```c
// C ABI, called once per log record by Fluent Bit's WAMR
// (WebAssembly Micro Runtime) host.
char* c_filter(char* tag, int tag_len,
                uint32_t time_sec, uint32_t time_nsec,
                char* record, int record_len);
```

Configured via a `wasm` filter block:

```yaml
filters:
  - name: wasm
    match: '*'
    wasm_path: /path/to/built_filter.wasm
    function_name: redact_filter
    accessible_paths: /path/to/fluent-bit
```

Three concrete facts this changes about the integration, versus just
dropping in Task #48's existing module unmodified:

1. **Different ABI entirely.** Task #48's `wasm/layer1_4/assembly/index.ts`
   was compiled with `--bindings esm` — AssemblyScript's JS-friendly
   wrapper, which handles string marshalling via generated glue code
   assuming a JS/Node host. Fluent Bit's WAMR host calls a raw C-ABI
   function directly against Wasm linear memory with no such glue.
   AssemblyScript CAN target this (it has `--exportRuntime` and manual
   pointer/memory APIs for exactly this case), but the existing build
   needs a second, C-ABI-shaped export added — not a config flag
   flip, a real second entry point with manual memory management.
2. **Different contract shape.** Fluent Bit's filter receives and must
   return an entire *record* (Fluent Bit's internal msgpack-encoded
   representation, JSON-shaped in the examples), not a text string with
   returned hit-spans the way `scanRegexJson`/`scanFlattenedNamesJson`
   do today. The Wasm module would need to: parse the record's
   msgpack/JSON structure, walk its string-valued fields, run Layer 1 +
   Layer 4 detection against each, apply replacement (not just
   detection), and re-serialize the modified record — a materially
   larger scope than Task #48's "detect and report spans" module.
3. **Detection only covers two of REDACT's three/five layers.** Layer 2
   (Presidio NER) and Layer 3 (entropy fallback) are not, and cannot
   reasonably be, ported to Wasm here — Presidio's NER is a full spaCy
   model (tens of MB, GPU/CPU-optimized native code), completely outside
   what a Wasm sandbox can run. An edge Wasm filter built from this
   module would ONLY catch what Layer 1 (regex) and Layer 4 (flattened
   names) catch — this project's own measured numbers (README, Section
   4) already show Layer 2's NER is responsible for the large majority
   of PERSON-type recall on normally-formatted text ("Timothy Wong"
   shapes), which Layer 4 explicitly does NOT cover (Layer 4 exists
   specifically for flattened usernames NER structurally can't catch).
   **This means an edge Wasm filter is not a drop-in replacement for
   `redact-service` — it's a lightweight PRE-filter that catches
   structured PII (SSN/EMAIL/CREDIT_CARD/IP/MRN shapes) and flattened
   usernames cheaply at the edge, with everything still needing to pass
   through the full pipeline (including NER) downstream for anything
   resembling REDACT's currently-measured recall.** This must be
   disclosed to anyone deploying this, not discovered after the fact —
   exactly the standard this project holds every other claim to.

## Redaction, not just detection: the real added scope

Task #48's module exports detection hits (`{type, start, end, method}`).
A real Fluent Bit filter needs to REPLACE those spans in the record,
which pulls in a slice of `src/anonymize.py`'s responsibility, not just
`src/detect.py`'s:

- **Regex-type replacements** (SSN, EMAIL, CREDIT_CARD, IP, MRN) are the
  simplest case — `anonymize.py`'s existing behavior for these is
  format-preserving-ish placeholder substitution (see that file for the
  exact per-type placeholder shapes), portable to Wasm with no new
  external state needed.
- **PERSON replacements** (Layer 4 hits) are the hard case:
  `anonymize.py`'s pseudonymization is consistent-per-identity — the
  same name always maps to the same pseudonym, via a persisted
  `TokenStore` (`REDACT_TOKEN_STORE_PATH`, reversible, HMAC-keyed).
  Doing this correctly at the edge, across potentially many independent
  Fluent Bit instances each running their own Wasm filter, raises a
  real open question this scoping doc does NOT resolve: does each edge
  node need its own local token store (accessible via
  `accessible_paths`, WASI file access), risking the same name getting
  DIFFERENT pseudonyms on different edge nodes (breaking
  cross-source correlation, the exact property `TokenStore` exists to
  preserve) — or does the edge filter need to call out to a shared
  service (defeating much of the point of edge processing, and
  reintroducing the network-round-trip cost this whole effort is meant
  to avoid)? **This is the single largest open design question for a
  real build**, not a detail to gloss over — flagged here rather than
  answered, since answering it needs a real decision about REDACT's
  cross-source pseudonym consistency guarantee, which this project has
  always treated as a first-class property (see `anonymize.py`'s own
  documentation), not something to quietly weaken for edge convenience.

## Effort estimate

Broken into phases, each independently buildable/testable, matching
this project's own incremental-verification discipline:

1. **C-ABI export + record parsing (no redaction yet, detection-only,
   logs matches to stdout/a side field).** Rebuild
   `wasm/layer1_4/assembly/index.ts` with a second entry point matching
   Fluent Bit's exact `c_filter` signature, add JSON record parsing
   (AssemblyScript has no built-in JSON parser either — another
   hand-written component, smaller than the detection logic itself but
   real work), wire up a minimal Fluent Bit source build with
   `-DFLB_WAMRC=On` to actually load and call it. **Estimated: several
   days**, mostly ABI/build-tooling friction rather than algorithm work
   (the detection logic itself is already done and verified).
2. **Regex-type (SSN/EMAIL/CREDIT_CARD/IP/MRN) redaction, stateless.**
   Straightforward once phase 1's plumbing works — no persisted state
   needed for these types. **Estimated: 1-2 days.**
2. **PERSON/pseudonym redaction — blocked on the open design question
   above being resolved first**, not just an implementation task.
   Whichever answer is chosen (local-per-node token store vs. a shared
   backing service) has a real, different effort shape: local-store is
   more Wasm/WASI file-API work; shared-service reintroduces a network
   dependency and needs its own auth/latency design, arguably
   undermining part of the original motivation for doing this at the
   edge at all. **Not estimated numerically here** — this needs a
   decision, not just more engineering time, before a number is
   meaningful.
3. **Real Fluent Bit build + WAMR runtime in an actual test
   environment.** This sandbox has no Fluent Bit source build available
   (no confirmed LLVM/AOT toolchain, no root to install one, and
   compiling Fluent Bit from source is itself a real, separate build
   dependency this sandbox has not attempted or confirmed it can do) —
   **this needs the user's own machine or CI environment**, the same
   "hand off, disclose, don't fake it" pattern already used for every
   Docker-dependent piece of this project's engineering work (floci,
   the 5M-line load test, etc).

**Total, honestly: a multi-week effort for a working, tested Fluent Bit
Wasm filter covering regex-type redaction, and an open design decision
(not just more time) standing between that and full PERSON-type parity
with the rest of REDACT's pipeline.** This matches Task #49's own
framing ("a multi-week integration effort on its own") — confirmed by
scoping it in detail, not just repeated at face value.

## Recommendation

Build Phase 1 + Phase 2 (regex-type-only edge pre-filter) as the next
concrete step if this is prioritized — it's real, bounded, useful on
its own (catches SSN/EMAIL/CREDIT_CARD/IP/MRN at the edge cheaply,
reducing what has to reach `redact-service` for those types), and
doesn't require resolving the harder PERSON-pseudonym-consistency
question first. Treat PERSON-type edge redaction as a separate,
later decision this project should make deliberately, not default into
by building whichever path is technically easiest.
