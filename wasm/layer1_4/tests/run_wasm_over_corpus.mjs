// Runs the compiled Wasm module's scanRegexJson/scanFlattenedNamesJson
// over every line of a given corpus file and writes one JSON record per
// line to stdout: {"line": <index>, "regex": [...], "flattened": [...]}.
// A separate Python script (equivalence_test.py) runs src/detect.py's
// real scan_regex() and src/flattened_names.py's real
// scan_flattened_names() over the identical corpus and diffs the two
// outputs directly -- this script's only job is to produce the Wasm
// side of that comparison, not to judge correctness itself.
//
// Usage: node run_wasm_over_corpus.mjs <corpus.jsonl-or-txt-one-log-per-line>
import { readFileSync } from "node:fs";
import { scanRegexJson, scanFlattenedNamesJson } from "../build/layer1_4.js";

const path = process.argv[2];
if (!path) {
  console.error("usage: node run_wasm_over_corpus.mjs <file, one raw log line per line>");
  process.exit(1);
}

const lines = readFileSync(path, "utf8").split("\n");
const out = [];
for (let i = 0; i < lines.length; i++) {
  const line = lines[i];
  if (line.length === 0) continue;
  const regexHits = JSON.parse(scanRegexJson(line));
  const flatHits = JSON.parse(scanFlattenedNamesJson(line));
  out.push({ line: i, regex: regexHits, flattened: flatHits });
}
console.log(JSON.stringify(out));
