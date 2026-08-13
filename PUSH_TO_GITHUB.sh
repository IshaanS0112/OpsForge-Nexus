#!/usr/bin/env bash
# One-shot: create the GitHub repo and push OpsForge Nexus.
# Prereq (once): install GitHub CLI and run `gh auth login`.
# Then, from inside the OpsForge/ folder:  ./PUSH_TO_GITHUB.sh
set -euo pipefail

REPO_NAME="opsforge-nexus"

git init -b main
git add .
git commit -m "OpsForge Nexus: blue-green deploys, z-score anomaly detection, structured RCA pipeline"

# --public (change to --private to keep it private). Creates the repo under your
# account, adds it as origin, and pushes main.
gh repo create "$REPO_NAME" --public --source=. --remote=origin --push \
  --description "AI-powered release, reliability & incident intelligence platform (FastAPI, React, PostgreSQL, n8n, Claude)"

echo "Done -> https://github.com/$(gh api user -q .login)/$REPO_NAME"

# --- No gh CLI? Create an empty repo named opsforge-nexus on github.com, then:
#   git init -b main && git add . && git commit -m "OpsForge Nexus"
#   git remote add origin https://github.com/<you>/opsforge-nexus.git
#   git push -u origin main
