# Setup and verification

Read README.md first. This edition uses the shared engine in ../zoom-meeting-agent.
Keep recordings, settings and login state in this directory. Do not copy the
engine to create a second, diverging implementation.

Use run.py doctor before a meeting. LLM is off by default; explain the text flow
to OpenAI before enabling --llm codex. Never put tokens or meeting content in
command arguments. Model prompts go through stdin. Do not enable coding tools,
MCP, plugins, apps, hooks or broad filesystem access for meeting extraction.

Test with synthetic data. Live model evaluation requires an explicit command;
offline tests must not call Zoom or a real model. Do not claim a real meeting,
Windows/macOS, or LLM semantic quality was tested when only mocks were run.

The output is a draft with source quotes. Check status.json and
minutes-status.json before calling a recording or protocol complete. Never
publish .env, storage_state.json, sessions, tokens or personal meeting data.
