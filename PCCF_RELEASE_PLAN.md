# PCCF push/release plan (for the author's decision; nothing has been pushed)

## Current state
- **Branch:** `pccf-research`, local only.
- **Base:** it branches from `main` at 60a4c22.
- **Remote:** `origin` = github.com/Zartharas/REDACT.
- **Licence:** the repo is MIT (© 2026 Aman Kumar Singh).
- **Kept out of git:** third-party datasets, caches, HF cache, logs, the
  restricted K-LegalDeID corpus, and `Academic Documentation/`, which holds
  the manuscript draft and outreach drafts.

## Options
| option | what it does | when |
|---|---|---|
| A. Keep local | nothing leaves the Mac | while the JPC decision is pending |
| B. Push the branch to a private remote/mirror | off-site backup, not public | **recommended now**, if `origin` is public |
| C. Merge to `main` and push | results public on GitHub | at submission/preprint time |
| D. Tag v0.2.0 and mint a Zenodo DOI (GitHub–Zenodo integration) | citable, versioned artefact | with C; cite it in the paper's data availability statement |

## Pre-push checklist
1. Confirm the visibility of `origin` (public or private) on GitHub.
2. Secret scan. The earlier scan found no HF token in any commit; re-run it:

   ```bash
   git log -p | grep -nE "hf_[A-Za-z0-9]{30,}"
   ```

3. Third-party licences are listed in the results docs:
   - ai4privacy: dataset terms
   - KLUE: CC BY-SA
   - KDPII: CC BY 4.0
   - WikiANN: CC BY-SA
   - GLiNER v2.1: Apache-2.0
   - akdeniz27 (tr_mit): MIT, confirmed via HF API 2026-09-26
   - savasy (tr): **no licence** -- internal-only as of 2026-09-26, never
     cited or published; drop the `tr` row from the public artefact
   - Enron (Minorthird), ANERcorp Kaggle mirror: **no licence stated** --
     do not bundle the raw files, ship only the fetchers (see item 7)
   - joonhok-exo-ai/korean_law_open_data_precedents: openrail
4. `Academic Documentation/` stays unpushed; it is already gitignored.
5. Decide whether `PUBLICATION_STRATEGY.md` stays private; it is untracked.
6. After the JPC path settles, avoid concurrent submission: the PCCF
   results should go to one venue at a time.
7. **Do not bundle raw third-party datasets in the release/Zenodo archive.**
   Ship the fetcher scripts and cite each dataset's own canonical source
   instead. This is a hard rule for Enron and the ANERcorp Kaggle mirror
   (no licence stated) and for K-LegalDeID if ever obtained (CC BY-NC-SA
   *and* an explicit no-redistribution promise made to the author in
   `Academic Documentation/Outreach/K-LegalDeID_follow_up.md`). CC BY /
   MIT / openrail sources (BTC, WNUT-17, GermEval, FactRuEval, KDPII,
   ai4privacy, ko_legal_precedents) may be mirrored later as a **separate**,
   clearly-labelled data deposit with a per-file license manifest -- not
   folded into the code archive.

## Metadata for the Zenodo/GitHub integration (drafted, needs your review)

`CITATION.cff` and `.zenodo.json` are now in the repo root. Both have
placeholders you must fill in before tagging -- affiliation and ORCID
were deliberately left blank rather than guessed:

**Correction (2026-09-26):** a bare `git push origin <tag>` does **not**
trigger Zenodo -- its GitHub integration listens for a GitHub **Release**
event, not a tag push. Step 4 below was wrong about this; fixed here.

**What actually happened with v0.3.0 (2026-09-26):** the Zenodo webhook
toggle (step 2) was on from the v0.2.0 release, so a `v0.3.0` tag got
pushed and a GitHub Release created from it *before* this plan's metadata
files were committed and *before* `pccf-research` was merged into `main`
-- `git checkout main` had failed on uncommitted changes, so the tag
silently landed on the unmerged research branch instead. Zenodo received
that release and minted `10.5281/zenodo.21829173` (same concept DOI as
v0.1.0/v0.2.0) with placeholder/fallback metadata (no ORCID, no
affiliation). That record can't be deleted, but it's harmless: `v0.3.1`
below supersedes it as a new version under the same concept DOI, with the
merged `main` branch and complete metadata -- normal for iterative
software releases; v0.3.0 just stays in the version history as a rougher
snapshot.

1. Edit both files: your ORCID (`CITATION.cff` wants the full URL,
   `.zenodo.json` wants just the ID), your affiliation, and confirm the
   version number. -- **Done 2026-09-26:** ORCID `0009-0008-9752-3743`,
   affiliation "Independent Researcher", version bumped to `0.3.1`.
2. One-time account step (only you can do this): go to
   https://zenodo.org/account/settings/github/, log in, and flip the
   toggle on for `Zartharas/REDACT`. This must happen *before* the tag
   below, or Zenodo won't see the release. -- **Done**, confirmed via the
   Zenodo GitHub settings page (was already on from the v0.2.0 release).
3. Merge `pccf-research` into `main` (Option C above) when ready --
   this is the actual "release" decision this plan has deferred; nothing
   here forces that timing.
4. Tag the *merged `main` commit* and push the tag, then create a GitHub
   **Release** from that tag -- the release event, not the tag push, is
   what Zenodo's integration actually listens for:
   ```bash
   git tag -a v0.3.1 -m "PCCF phases 1-11: real-document validation, H1-H43 (supersedes v0.3.0 metadata)"
   git push origin v0.3.1
   gh release create v0.3.1 --title "v0.3.1 -- PCCF phases 1-11" \
     --notes "Supersedes v0.3.0 (tagged from an unmerged branch with placeholder metadata). See PCCF_SUMMARY.md and PCCF_PHASE11_RESULTS.md for full results."
   ```
5. Zenodo mints a DOI for the new version automatically within a few
   minutes of the Release being created (not the tag push). Add the
   resulting DOI badge to `README.md` and cite it in the paper's
   data-availability statement.
