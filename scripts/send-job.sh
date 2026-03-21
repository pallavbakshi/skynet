#!/usr/bin/env bash
# Send a job to the AGP control plane.
# Usage: bash scripts/send-job.sh "your task here"
#        bash scripts/send-job.sh  (uses default test task)
set -euo pipefail

SERVER_URL="${AGP_SERVER_URL:-http://127.0.0.1:7860}"
AGENT_ID="${AGP_AGENT_ID:-agt_local}"
TASK="${1:-Write a hello world Python script and save it to /tmp/hello.py}"

uv run python -c "
from agp.client import AgpClient, AgpProfile
import time

profile = AgpProfile(server_url='${SERVER_URL}')
with AgpClient(profile=profile) as client:
    result = client.send('agent', '${AGENT_ID}', '''${TASK}''',
        metadata={'kind': 'manual'},
        idempotency_key=f'job-{int(time.time())}')
    print(f'Job:    {result[\"job_id\"]}')
    print(f'Status: {result[\"status\"]}')
    print(f'Agent:  ${AGENT_ID}')
    print()
    print('Watch live:  tmux attach -t agp-${AGENT_ID}')
"
