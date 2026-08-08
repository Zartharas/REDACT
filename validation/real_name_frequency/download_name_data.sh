#!/bin/bash
# Downloads real US name-frequency source data for the still-open half of
# ROADMAP item 10: validation/non_us_name_test.py already confirmed the
# flattened-username dictionary collapses outside its own (Faker en_US)
# population using Faker's non-US locales as the substitute population;
# this is the originally-planned real-population version of that test.
#
# Both sources are official US government aggregate statistics, public
# domain, and each publisher explicitly states the data does not identify
# individuals -- same ethical bar that got `names-dataset` (PyPI, sourced
# from a 2021 Facebook breach) rejected for this project. See raw/README.md
# for the full source citations.
#
# Mirrors validation/real_data/download_loghub.sh's own pattern: raw
# source data is downloaded fresh here, not committed to the repo (see
# .gitignore -- validation/real_name_frequency/raw/*.zip is ignored, only
# this script and raw/README.md are tracked).
set -e
mkdir -p raw
echo "Fetching SSA given-name frequency data (national, 1880-present)..."
curl -sL "https://www.ssa.gov/oact/babynames/names.zip" -o "raw/ssa_given_names.zip"
echo "Fetching Census 2010 surname frequency data (162,253 surnames)..."
curl -sL "https://www2.census.gov/topics/genealogy/2010surnames/names.zip" -o "raw/census_surnames.zip"
echo "Done. Two zip files in raw/, read directly by build_real_name_test.py (not yet written) via Python's zipfile module -- no need to extract by hand."
