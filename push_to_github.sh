#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
git remote remove origin 2>/dev/null || true
git remote add origin git@github.com:aman-vishen/uncord_automation.git
git branch -M main
git push -u origin main
