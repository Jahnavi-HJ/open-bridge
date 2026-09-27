#!/bin/bash
# Hermetic tests for the a2a skill: no network, no keychain.
set -euo pipefail
cd "$(dirname "$0")/../.."
exec python3 -m pytest skills/a2a/tests -q -rs "$@"
