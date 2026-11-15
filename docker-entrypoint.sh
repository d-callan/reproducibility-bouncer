#!/bin/sh
# Prepare the isolated orbit HOME (env-keyed provider — same shape the
# bionym eval used) then hand off to the reprobouncer CLI.
set -e

OH="${REPRO_ORBIT_HOME:-/orbit-home}"
mkdir -p "$OH/.orbit" "$OH/.pi/agent"

# Orbit reads baseUrl/model from ~/.orbit/config.json; the API key comes
# from ORBIT_ACTIVE_LLM_API_KEY, which orbit.py forwards from LLM_API_KEY
# when REPRO_ORBIT_HOME is set. Provider defaults mirror the eval
# (TACC Tejas / MiniMax-M3); override via ORBIT_LLM_* env.
cat > "$OH/.orbit/config.json" <<EOF
{
  "llm": {
    "active": "env",
    "providers": {
      "env": {
        "baseUrl": "${ORBIT_LLM_BASE_URL:-https://ai.tejas.tacc.utexas.edu/v1}",
        "api": "openai-completions",
        "model": "${ORBIT_LLM_MODEL:-MiniMax-M3}",
        "contextWindow": ${ORBIT_LLM_CONTEXT:-131072}
      }
    }
  },
  "updateCheck": false
}
EOF

exec reprobouncer "$@"
