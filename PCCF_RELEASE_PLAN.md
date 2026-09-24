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
   - akdeniz27: MIT
   - savasy: **no licence**, so keep the tr row research-only or drop it
     from the public artefact
4. `Academic Documentation/` stays unpushed; it is already gitignored.
5. Decide whether `PUBLICATION_STRATEGY.md` stays private; it is untracked.
6. After the JPC path settles, avoid concurrent submission: the PCCF
   results should go to one venue at a time.
