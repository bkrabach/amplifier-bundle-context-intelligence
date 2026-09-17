#!/usr/bin/env bash
#
# validate-full.sh — launch `validate-bundle-repo` with full-mode-capable private dependencies.
#
# WHY THIS EXISTS
# --------------
# The validator runs its Python checks through a bash `python3` heredoc. In a
# default Amplifier environment that `python3` is a minimal interpreter with no
# `pip` and no `amplifier_foundation` / `hatchling`, so the recipe self-downgrades
# to `validation_mode: structural_only` — it SKIPS the two checks that matter most
# for a behaviour split: BundleRegistry resolution of the layered includes, and the
# package build check.
#
# This script builds a private, throwaway uv venv with the validator's dependencies
# and the Amplifier CLI. It invokes that venv's CLI explicitly (rather than the host
# CLI), so both the CLI's fixed shebang and the recipe's `python3` resolve to the
# interpreter that can `import pip`, `amplifier_foundation`, and `hatchling`.
#
# This is a launch/dependency helper, not a verdict gate. It propagates the
# `amplifier tool invoke` exit status unchanged; a zero process exit is not a
# validation PASS. User/CI must inspect the recipe's published structured
# `validation_mode`, `overall_verdict`, and `build_tested` fields. Result parsing
# and any remaining recipe/full-gate behavior are outside this interpreter repair.
#
# (This is the uv-based equivalent of the recipe's own documented
#  `uvx --with hatchling --with amplifier-foundation amplifier tool invoke ...`
#  one-liner; the venv form is used because the recipe shells out to `python3`,
#  so the deps must live on the PATH `python3`, not just in a uvx tool env.)
#
# USAGE
# -----
#   scripts/validate-full.sh [REPO_PATH]
#       REPO_PATH defaults to this bundle's repo root.
#
# ENV
#   CI_VALIDATE_VENV   set a new venv location. The path must not already exist.
#                      By default, a unique throwaway venv is created beneath
#                      <REPO_PATH>/.amplifier/validation/ and removed on exit.
#
# Requires: uv, network access to install the pinned private CLI, and the
# amplifier-foundation bundle present in ~/.amplifier/cache (it ships the recipe).
#
set -euo pipefail

REPO_PATH="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
CLI_REF="4d168ed822314dced895c8cf7fdbb24233cbe31b"

if [[ -n "${CI_VALIDATE_VENV:-}" ]]; then
  VENV="$CI_VALIDATE_VENV"
  if [[ -e "$VENV" || -L "$VENV" ]]; then
    echo "!! CI_VALIDATE_VENV already exists; refusing to modify it: $VENV" >&2
    exit 1
  fi
  VENV_OWNED=false
else
  VENV_ROOT="$REPO_PATH/.amplifier/validation"
  mkdir -p "$VENV_ROOT"
  VENV="$(mktemp -d "$VENV_ROOT/full.XXXXXX")"
  VENV_OWNED=true
fi

if [[ "$VENV_OWNED" == true ]]; then
  trap 'rm -rf "$VENV"' EXIT
fi

echo ">> building deps venv: $VENV"
uv venv --python 3.11 --allow-existing "$VENV" >/dev/null
uv pip install --python "$VENV/bin/python" --quiet \
  pip hatchling pyyaml \
  "amplifier-core @ git+https://github.com/microsoft/amplifier-core@main" \
  "amplifier-foundation @ git+https://github.com/microsoft/amplifier-foundation@main" \
  "amplifier-app-cli @ git+https://github.com/microsoft/amplifier-app-cli@$CLI_REF"

if ! PYTHONNOUSERSITE=1 "$VENV/bin/python" -c 'import pip, hatchling, amplifier_foundation'; then
  echo "!! private validation Python is missing required imports" >&2
  exit 1
fi
if [[ ! -x "$VENV/bin/amplifier" ]]; then
  echo "!! private validation venv did not install an executable amplifier CLI" >&2
  exit 1
fi

# Locate the foundation validate-bundle-repo recipe in the Amplifier cache.
# (The bare `amplifier tool invoke` CLI does not resolve the `foundation:` recipe
#  namespace, so we pass the cached recipe by absolute path.)
RECIPE="$(ls -1 "${HOME}/.amplifier/cache/"amplifier-foundation-*/recipes/validate-bundle-repo.yaml 2>/dev/null | head -1 || true)"
if [[ -z "$RECIPE" ]]; then
  echo "!! validate-bundle-repo.yaml not found under ~/.amplifier/cache/amplifier-foundation-*/recipes/" >&2
  echo "   Ensure the amplifier-foundation bundle is installed/cached, then retry." >&2
  exit 1
fi

echo ">> recipe: $RECIPE"
echo ">> repo:   $REPO_PATH"
echo ">> launching validate-bundle-repo with full-mode-capable private dependencies ..."
echo ">> inspect the published recipe validation_mode, overall_verdict, and build_tested; exit 0 is not PASS"
CONTEXT="$("$VENV/bin/python" -c 'import json, sys; print(json.dumps({"repo_path": sys.argv[1]}))' "$REPO_PATH")"
PYTHONNOUSERSITE=1 \
PATH="$VENV/bin:$PATH" "$VENV/bin/amplifier" tool invoke recipes operation=execute \
  recipe_path="$RECIPE" \
  context="$CONTEXT"
