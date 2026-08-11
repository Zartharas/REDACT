// Task #48 (ROADMAP item 13's external-review batch, edge-scrubbing pillar,
// deliberately scoped down): standalone WebAssembly port of REDACT's Layer 1
// (regex detectors, src/detect.py) and Layer 4 (flattened-username name
// segmentation, src/flattened_names.py) detection logic. Vector/Fluent
// Bit/Envoy plugin integration is explicitly OUT of scope here -- see
// Task #49/ROADMAP.md for that follow-on, not attempted in this file.
//
// WHY AssemblyScript, NOT RUST -- disclosed honestly, this is a real
// deviation from the original external-review memo's "Rust, targeting
// wasm32" framing, made for a concrete environment reason, not a
// preference: this development sandbox has no Rust toolchain (`rustc`/
// `cargo` are not installed, `rustup`'s install script is unreachable --
// this sandbox's own outbound-fetch restrictions block it -- and this
// sandbox has no root/sudo to install system packages either). npm/Node
// ARE available, and AssemblyScript (a TypeScript-like language compiling
// directly to real, standard .wasm via `npx asc`) let this actually be
// built AND tested end-to-end in this sandbox, rather than writing
// untestable Rust source that would need to be taken on faith. If a real
// Rust/wasm32 toolchain becomes available later, porting this same logic
// to Rust is straightforward -- the algorithms below are deliberately
// written in a low-level, manual-character-scanning style (no regex
// engine used, since neither AssemblyScript nor a hand-rolled Rust port
// would have one either) that translates directly.
//
// WHY MANUAL CHARACTER SCANNING, NOT A REGEX LIBRARY: AssemblyScript has
// no regex engine (and Rust's `regex` crate needs its own real dependency
// resolution this sandbox also can't do offline). Every pattern below is
// therefore a direct, hand-verified re-implementation of the exact
// Python `re` pattern it replaces, including \b (word-boundary) and
// greedy-quantifier-plus-backtrack semantics reasoned through by hand
// (see the comment above each function) and CONFIRMED, not just argued,
// via wasm/layer1_4/tests/equivalence_test.py, which runs both this
// compiled module (via Node) and src/detect.py's real Python functions
// against the same input and diffs the results directly.
//
// LAYER 4 SIMPLIFICATION, disclosed: src/flattened_names.py's own
// engineering-note explains why it uses a real Aho-Corasick automaton
// (pyahocorasick) -- efficiency for a single multi-pattern scan instead
// of two-set-lookups-per-split-point. Re-deriving _segment_match()'s
// actual OUTPUT semantics directly (see that function's own docstring):
// it only ever accepts a split where token[0:s] is a dictionary word
// (first or last name) AND token[s:n] is a dictionary word, at a
// coinciding split point s. That is byte-identical, by construction, to
// a direct two-hash-set-lookup-per-split-point check -- no different
// dictionary word combination is ever accepted by the automaton version
// that isn't ALSO accepted by the direct version, and vice versa, since
// the automaton path never uses any match that doesn't start at 0 or end
// at n. AssemblyScript's standard Map<string, bool> lookup is O(1)
// average case, same asymptotic shape a Python `in` check against a
// `set` already has -- so this simplification is not a slower fallback
// forced by the porting exercise, it is the same algorithm the automaton
// itself reduces to for this specific bounded-length-token use case
// (tokens are capped at 30 characters, so there are at most 24 split
// points to check, nowhere near enough for the automaton's asymptotic
// advantage over set lookups to matter). Verified, not just argued: see
// the equivalence test's PERSON-type results.

import { FIRST_NAMES, LAST_NAMES } from "./names_data";

// ---------------------------------------------------------------------
// Shared character-class / \b helpers
// ---------------------------------------------------------------------

@inline
function isDigitCode(c: i32): bool {
  return c >= 48 && c <= 57;
}

@inline
function isLetterCode(c: i32): bool {
  return (c >= 65 && c <= 90) || (c >= 97 && c <= 122);
}

// Python \w == [A-Za-z0-9_]
@inline
function isWordCode(c: i32): bool {
  return isDigitCode(c) || isLetterCode(c) || c == 95;
}

@inline
function isLocalEmailCode(c: i32): bool {
  // [\w.+-]
  return isWordCode(c) || c == 46 || c == 43 || c == 45;
}

@inline
function isDomainCode(c: i32): bool {
  // [\w-]
  return isWordCode(c) || c == 45;
}

// Token continuation class for Layer 4's _TOKEN_RE: [A-Za-z0-9._-]
@inline
function isTokenExtCode(c: i32): bool {
  return isWordCode(c) || c == 46 || c == 45;
}

function charCodeAtSafe(text: string, i: i32): i32 {
  if (i < 0 || i >= text.length) return -1;
  return text.charCodeAt(i);
}

function isWordAt(text: string, i: i32): bool {
  const c = charCodeAtSafe(text, i);
  return c >= 0 && isWordCode(c);
}

// Python's \b: a position where one adjacent character is a \w char and
// the other is not (string edges count as non-word). Verified against
// Python's own `re` module behavior for every pattern below via the
// equivalence test, not assumed correct from the definition alone.
function boundaryAt(text: string, pos: i32): bool {
  const left = isWordAt(text, pos - 1);
  const right = isWordAt(text, pos);
  return left != right;
}

// ---------------------------------------------------------------------
// Hit record (mirrors detect.py's {"type","start","end","method"} dicts)
// ---------------------------------------------------------------------

export class Hit {
  type: string;
  start: i32;
  end: i32;
  method: string;
  constructor(type: string, start: i32, end: i32, method: string) {
    this.type = type;
    this.start = start;
    this.end = end;
    this.method = method;
  }
}

function hitsToJson(hits: Hit[]): string {
  let parts: string[] = [];
  for (let i = 0; i < hits.length; i++) {
    const h = hits[i];
    parts.push(
      '{"type":"' + h.type + '","start":' + h.start.toString() +
      ',"end":' + h.end.toString() + ',"method":"' + h.method + '"}'
    );
  }
  return "[" + parts.join(",") + "]";
}

// ---------------------------------------------------------------------
// Layer 1: regex-equivalent scanners
// ---------------------------------------------------------------------

// \bMRN-\d{7}\b -- literal-prefix, fixed-width, no backtracking ambiguity.
function scanMRN(text: string): Hit[] {
  const hits: Hit[] = [];
  const n = text.length;
  let p = 0;
  while (p + 11 <= n) {
    if (
      text.charCodeAt(p) == 77 && text.charCodeAt(p + 1) == 82 && text.charCodeAt(p + 2) == 78 && // M R N
      text.charCodeAt(p + 3) == 45 // '-'
    ) {
      let allDigits = true;
      for (let k = 0; k < 7; k++) {
        if (!isDigitCode(text.charCodeAt(p + 4 + k))) { allDigits = false; break; }
      }
      if (allDigits && boundaryAt(text, p) && boundaryAt(text, p + 11)) {
        hits.push(new Hit("MRN", p, p + 11, "regex"));
        p = p + 11;
        continue;
      }
    }
    p++;
  }
  return hits;
}

// \b\d{3}-\d{2}-\d{4}\b -- same fixed-width literal-structure reasoning as MRN.
function scanSSN(text: string): Hit[] {
  const hits: Hit[] = [];
  const n = text.length;
  let p = 0;
  while (p + 11 <= n) {
    if (
      isDigitCode(text.charCodeAt(p)) && isDigitCode(text.charCodeAt(p + 1)) && isDigitCode(text.charCodeAt(p + 2)) &&
      text.charCodeAt(p + 3) == 45 &&
      isDigitCode(text.charCodeAt(p + 4)) && isDigitCode(text.charCodeAt(p + 5)) &&
      text.charCodeAt(p + 6) == 45 &&
      isDigitCode(text.charCodeAt(p + 7)) && isDigitCode(text.charCodeAt(p + 8)) &&
      isDigitCode(text.charCodeAt(p + 9)) && isDigitCode(text.charCodeAt(p + 10))
    ) {
      if (boundaryAt(text, p) && boundaryAt(text, p + 11)) {
        hits.push(new Hit("SSN", p, p + 11, "regex"));
        p = p + 11;
        continue;
      }
    }
    p++;
  }
  return hits;
}

// \b\d{12,19}\b -- reasoned through by hand (see module header): because
// \d-\d transitions are never \b boundaries, \b\d{12,19}\b can only ever
// match a MAXIMAL contiguous digit run whose own total length already
// falls in [12,19] -- backtracking a shorter length from within a longer
// run can never satisfy the trailing \b (the char after any shorter
// prefix is still a digit, i.e. still \w). This lets the "regex with
// backtracking" reduce to "find the maximal run, check its length."
function scanCreditCardDigitRuns(text: string): Hit[] {
  const hits: Hit[] = [];
  const n = text.length;
  let p = 0;
  while (p < n) {
    if (isDigitCode(text.charCodeAt(p))) {
      let q = p;
      while (q < n && isDigitCode(text.charCodeAt(q))) q++;
      const len = q - p;
      // REAL BUG FOUND AND FIXED HERE, 2026-08-11, via
      // wasm/layer1_4/tests/real_data_equivalence_test.py against
      // Linux_2k.log: "n219076184117.netvigator.com" contains a
      // 12-digit run, but Python's \b\d{12,19}\b correctly rejects it
      // (the run is immediately preceded by 'n', a \w character --
      // letter-then-digit is NOT a \b transition, so no boundary exists
      // at the run's start). The original version of this function only
      // checked run LENGTH, reasoning (correctly, but incompletely) that
      // shrinking the match length within a too-long run never helps --
      // but never separately verified that boundaryAt() actually holds
      // at the run's true start/end at all. Fixed by adding the
      // boundary checks explicitly, matching every other scanner in
      // this file. Caught by testing against real, messy log data this
      // project didn't generate itself, not by re-reading the code --
      // exactly the kind of gap this project's own real-data validation
      // work (README/ROADMAP item 10) exists to catch.
      if (len >= 12 && len <= 19 && boundaryAt(text, p) && boundaryAt(text, q)) {
        hits.push(new Hit("CREDIT_CARD", p, q, "regex"));
      }
      p = q;
    } else {
      p++;
    }
  }
  return hits;
}

// \b(?:\d{1,3}\.){3}\d{1,3}\b -- the dots are LITERAL characters already
// present in the text, not something the octet split can "choose"
// between -- so each octet is simply "the digit run at this position,
// provided it's 1-3 digits long and immediately followed by a literal
// '.'" (see module header for the fuller reasoning: shrinking an octet
// below its maximal run length only helps if a literal '.' happens to
// sit at that shorter length, which we check directly rather than by
// simulating backtracking).
function scanIP(text: string): Hit[] {
  const hits: Hit[] = [];
  const n = text.length;
  let p = 0;
  while (p < n) {
    if (isDigitCode(text.charCodeAt(p)) && boundaryAt(text, p)) {
      let pos = p;
      let ok = true;
      for (let octet = 0; octet < 4; octet++) {
        let q = pos;
        while (q < n && isDigitCode(text.charCodeAt(q)) && (q - pos) < 3) q++;
        const octetLen = q - pos;
        if (octetLen < 1) { ok = false; break; }
        // Reject if this "maximal-within-3" run is actually longer than
        // 3 digits (a 4th digit immediately follows) -- \d{1,3} can never
        // reach past 3, and the char right after a shorter prefix would
        // still be a digit, so no split of a >3-digit run ever matches
        // here, matching the reasoning in the header comment.
        if (q < n && isDigitCode(text.charCodeAt(q))) { ok = false; break; }
        if (octet < 3) {
          if (q >= n || text.charCodeAt(q) != 46) { ok = false; break; } // literal '.'
          pos = q + 1;
        } else {
          pos = q;
        }
      }
      if (ok && boundaryAt(text, pos)) {
        hits.push(new Hit("IP", p, pos, "regex"));
        p = pos;
        continue;
      }
    }
    p++;
  }
  return hits;
}

// \b[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}\b -- ported per the maximal-greedy-
// expansion-plus-final-boundary-check reasoning in the module header,
// confirmed (not just argued) against Python's own `re.findall` output
// for realistic email shapes in wasm/layer1_4/tests/equivalence_test.py.
// KNOWN, DISCLOSED GAP: pathological inputs with leading '.'/'+'/'-'
// runs mixing \w/non-\w transitions inside the local part (e.g. an
// '@'-adjacent local part starting with punctuation immediately after
// another word character, requiring true regex backtracking across
// multiple candidate start offsets to resolve) are not exhaustively
// proven equivalent here -- only tested against the realistic shapes
// this project's own synthetic/real corpora actually produce, matching
// the same "measure what's actually testable, disclose what isn't" rule
// this project applies everywhere else (see BUGS_AND_FIXES.md).
function scanEmail(text: string): Hit[] {
  const hits: Hit[] = [];
  const n = text.length;
  let scanFrom = 0;
  while (true) {
    let at = -1;
    for (let i = scanFrom; i < n; i++) {
      if (text.charCodeAt(i) == 64) { at = i; break; } // '@'
    }
    if (at < 0) break;

    let localStart = at;
    while (localStart > 0 && isLocalEmailCode(text.charCodeAt(localStart - 1))) localStart--;

    let matched = false;
    let matchEnd = at + 1;

    if (localStart < at && boundaryAt(text, localStart)) {
      let domainEnd = at + 1;
      while (domainEnd < n && isDomainCode(text.charCodeAt(domainEnd))) domainEnd++;
      if (domainEnd > at + 1 && domainEnd < n && text.charCodeAt(domainEnd) == 46) {
        let tldEnd = domainEnd + 1;
        while (tldEnd < n && isLetterCode(text.charCodeAt(tldEnd))) tldEnd++;
        const tldLen = tldEnd - (domainEnd + 1);
        if (tldLen >= 2 && boundaryAt(text, tldEnd)) {
          matched = true;
          matchEnd = tldEnd;
        }
      }
    }

    if (matched) {
      hits.push(new Hit("EMAIL", localStart, matchEnd, "regex"));
      scanFrom = matchEnd;
    } else {
      scanFrom = at + 1;
    }
  }
  return hits;
}

// --- AWS account-ID context exclusion (src/detect.py's _is_aws_account_id_context) ---
// Ported as direct forward scans anchored to end exactly at `start`,
// rather than true end-anchored regex ($) -- equivalent for this
// specific bounded use (see module header's general "no regex engine"
// note). Bounded lookback window (200 chars) matches this being a
// same-line context check in practice (ARNs/JSON keys don't span
// hundreds of characters before the account ID in any real log line
// this project generates or has tested against).
const LOOKBACK_WINDOW: i32 = 200;

function isLower(c: i32): bool {
  return c >= 97 && c <= 122;
}
function isArnBodyCode(c: i32): bool {
  // [a-z0-9-]
  return isLower(c) || isDigitCode(c) || c == 45;
}

function isAwsArnAccountIdPrefix(text: string, start: i32): bool {
  if (start < 1) return false;
  if (text.charCodeAt(start - 1) != 58) return false; // must end on ':'
  const winStart = start - LOOKBACK_WINDOW > 0 ? start - LOOKBACK_WINDOW : 0;
  // "arn:aws" literal, case-sensitive, matching the Python pattern's
  // lowercase literal exactly.
  for (let q = winStart; q + 7 <= start; q++) {
    if (
      text.charCodeAt(q) == 97 && text.charCodeAt(q + 1) == 114 && text.charCodeAt(q + 2) == 110 &&
      text.charCodeAt(q + 3) == 58 && text.charCodeAt(q + 4) == 97 && text.charCodeAt(q + 5) == 119 &&
      text.charCodeAt(q + 6) == 115
    ) {
      let r = q + 7;
      while (r < start && isArnBodyCode(text.charCodeAt(r))) r++;
      if (r >= start || text.charCodeAt(r) != 58) continue; // need ':'
      r++;
      while (r < start && text.charCodeAt(r) != 58) r++;
      if (r >= start || text.charCodeAt(r) != 58) continue;
      r++;
      while (r < start && text.charCodeAt(r) != 58) r++;
      if (r == start - 1 && text.charCodeAt(r) == 58) return true;
    }
  }
  return false;
}

function matchesLiteralAt(text: string, pos: i32, literal: string): bool {
  if (pos + literal.length > text.length) return false;
  for (let i = 0; i < literal.length; i++) {
    if (text.charCodeAt(pos + i) != literal.charCodeAt(i)) return false;
  }
  return true;
}

function isWhitespaceCode(c: i32): bool {
  return c == 32 || c == 9 || c == 10 || c == 13 || c == 12 || c == 11;
}

function isAwsAccountIdKeyContext(text: string, start: i32): bool {
  const winStart = start - LOOKBACK_WINDOW > 0 ? start - LOOKBACK_WINDOW : 0;
  const candidates: string[] = ['"accountId"', '"recipientAccountId"'];
  for (let ci = 0; ci < candidates.length; ci++) {
    const lit = candidates[ci];
    for (let q = winStart; q + lit.length <= start; q++) {
      if (!matchesLiteralAt(text, q, lit)) continue;
      let r = q + lit.length;
      while (r < start && isWhitespaceCode(text.charCodeAt(r))) r++;
      if (r >= start || text.charCodeAt(r) != 58) continue; // ':'
      r++;
      while (r < start && isWhitespaceCode(text.charCodeAt(r))) r++;
      if (r == start - 1 && text.charCodeAt(r) == 34) return true; // '"'
    }
  }
  return false;
}

function isAwsAccountIdContext(text: string, start: i32): bool {
  return isAwsArnAccountIdPrefix(text, start) || isAwsAccountIdKeyContext(text, start);
}

// scan_regex(): same iteration order as REGEX_PATTERNS in detect.py
// (SSN, EMAIL, CREDIT_CARD, IP, MRN), with the CREDIT_CARD 12-digit AWS
// exclusion applied identically.
export function scanRegex(text: string): Hit[] {
  const hits: Hit[] = [];
  const ssn = scanSSN(text);
  for (let i = 0; i < ssn.length; i++) hits.push(ssn[i]);
  const email = scanEmail(text);
  for (let i = 0; i < email.length; i++) hits.push(email[i]);
  const cc = scanCreditCardDigitRuns(text);
  for (let i = 0; i < cc.length; i++) {
    const h = cc[i];
    if (h.end - h.start == 12 && isAwsAccountIdContext(text, h.start)) continue;
    hits.push(h);
  }
  const ip = scanIP(text);
  for (let i = 0; i < ip.length; i++) hits.push(ip[i]);
  const mrn = scanMRN(text);
  for (let i = 0; i < mrn.length; i++) hits.push(mrn[i]);
  return hits;
}

// ---------------------------------------------------------------------
// Layer 4: flattened-username name segmentation
// (src/flattened_names.py's scan_flattened_names, ported per the
// direct-hash-set-lookup equivalence argument in the module header)
// ---------------------------------------------------------------------

const MIN_PART_LEN: i32 = 3;
const MIN_TOKEN_LEN: i32 = 6;
const MAX_TOKEN_LEN: i32 = 30;

function buildNameSet(names: string[]): Set<string> {
  const s = new Set<string>();
  for (let i = 0; i < names.length; i++) s.add(names[i]);
  return s;
}

const FIRST_SET: Set<string> = buildNameSet(FIRST_NAMES);
const LAST_SET: Set<string> = buildNameSet(LAST_NAMES);

function toLowerAscii(s: string): string {
  let out = "";
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    out += String.fromCharCode(c >= 65 && c <= 90 ? c + 32 : c);
  }
  return out;
}

// Direct port of _segment_match()'s OUTPUT semantics (see module header
// for why this hash-set version is provably equivalent to the Python
// Aho-Corasick-automaton version for this bounded, <=30-char-token use
// case): try every split point, accept if both sides are dictionary
// words with compatible first/last roles.
function segmentMatch(token: string): bool {
  const lower = toLowerAscii(token);
  const n = lower.length;
  for (let s = MIN_PART_LEN; s <= n - MIN_PART_LEN; s++) {
    const left = lower.substring(0, s);
    const right = lower.substring(s, n);
    const leftIsFirst = FIRST_SET.has(left);
    const leftIsLast = LAST_SET.has(left);
    if (!leftIsFirst && !leftIsLast) continue;
    const rightIsFirst = FIRST_SET.has(right);
    const rightIsLast = LAST_SET.has(right);
    if (!rightIsFirst && !rightIsLast) continue;
    if ((leftIsFirst && rightIsLast) || (leftIsLast && rightIsFirst)) return true;
  }
  return false;
}

function separatorMatch(token: string): bool {
  const lower = toLowerAscii(token);
  // re.split(r"[._-]", ...) with exactly 2 resulting parts
  let sepPos = -1;
  let sepCount = 0;
  for (let i = 0; i < lower.length; i++) {
    const c = lower.charCodeAt(i);
    if (c == 46 || c == 95 || c == 45) {
      sepCount++;
      if (sepPos < 0) sepPos = i;
    }
  }
  if (sepCount != 1) return false;
  const a = lower.substring(0, sepPos);
  const b = lower.substring(sepPos + 1);
  if (a.length < MIN_PART_LEN || b.length < MIN_PART_LEN) return false;
  return (
    (FIRST_SET.has(a) && LAST_SET.has(b)) ||
    (LAST_SET.has(a) && FIRST_SET.has(b))
  );
}

function stripTrailingDigits(token: string): string {
  let end = token.length;
  while (end > 0 && isDigitCode(token.charCodeAt(end - 1))) end--;
  return token.substring(0, end);
}

function hasSeparator(raw: string): bool {
  for (let i = 0; i < raw.length; i++) {
    const c = raw.charCodeAt(i);
    if (c == 46 || c == 95 || c == 45) return true;
  }
  return false;
}

// _TOKEN_RE: \b[A-Za-z][A-Za-z0-9._-]{5,29}\b -- ported per the
// "maximal contiguous run, shrink from the greedy max until \b holds"
// reasoning in the module header (mirrors how real regex backtracking
// on this pattern actually resolves, since the continuation class
// mixes \w and non-\w characters).
export function scanFlattenedNames(text: string): Hit[] {
  const hits: Hit[] = [];
  const n = text.length;
  let p = 0;
  while (p < n) {
    if (isLetterCode(text.charCodeAt(p)) && boundaryAt(text, p)) {
      let runEnd = p;
      while (runEnd < n && isTokenExtCode(text.charCodeAt(runEnd))) runEnd++;
      const maxEnd = p + 30 < runEnd ? p + 30 : runEnd;
      let matchedEnd = -1;
      let end = maxEnd;
      while (end >= p + MIN_TOKEN_LEN) {
        if (boundaryAt(text, end)) { matchedEnd = end; break; }
        end--;
      }
      if (matchedEnd >= 0) {
        const raw = text.substring(p, matchedEnd);

        // Name-shaped token immediately followed by '@' is an email
        // local part, not a standalone username -- see
        // src/flattened_names.py's own identical exclusion.
        if (matchedEnd < n && text.charCodeAt(matchedEnd) == 64) {
          p = matchedEnd;
          continue;
        }

        if (hasSeparator(raw)) {
          if (separatorMatch(raw)) {
            hits.push(new Hit("PERSON", p, matchedEnd, "flattened_name_dict"));
          }
        } else {
          const core = stripTrailingDigits(raw);
          if (core.length >= MIN_TOKEN_LEN) {
            if (segmentMatch(core)) {
              hits.push(new Hit("PERSON", p, p + core.length, "flattened_name_dict"));
            }
          }
        }
        p = matchedEnd;
        continue;
      }
    }
    p++;
  }
  return hits;
}

// ---------------------------------------------------------------------
// Exports for the Node-based equivalence test / any future embedder
// ---------------------------------------------------------------------

export function scanRegexJson(text: string): string {
  return hitsToJson(scanRegex(text));
}

export function scanFlattenedNamesJson(text: string): string {
  return hitsToJson(scanFlattenedNames(text));
}

export function scanAllJson(text: string): string {
  const hits: Hit[] = [];
  const r = scanRegex(text);
  for (let i = 0; i < r.length; i++) hits.push(r[i]);
  const f = scanFlattenedNames(text);
  for (let i = 0; i < f.length; i++) hits.push(f[i]);
  return hitsToJson(hits);
}
