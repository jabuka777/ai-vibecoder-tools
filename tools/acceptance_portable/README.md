# Portable installation acceptance

`.github/workflows/acceptance.yml` runs on separate standard GitHub-hosted
Linux, Windows and macOS machines, with Python 3.12. Trigger it manually on the
branch to test; pushes to `zoom-acceptance-*` also trigger it. No account secrets,
Zoom meetings or paid LLM calls are needed.

The first job builds a source-only ZIP from the exact clean workflow commit.
Every OS verifies its SHA256 and extracts that same ZIP into a new path containing
spaces and Cyrillic characters. The runner prepares ffmpeg and, on Linux,
Playwright's system libraries explicitly. This does not test automatic installation
of system packages by the product: the product intentionally does not do that.

The harness installs the skill into a separate client profile, runs its setup
with `--install --download-model`, and adds `--install-mcp` only on POSIX.
Python dependencies, Chromium and Whisper use separate empty caches. It then
checks actual browser/model startup, repeats setup with downloads blocked,
runs portable regression tests and exercises real ffmpeg and Whisper on the
embedded Russian speech fixture. Chromium capture tests use a synthetic oscillator
and the product's real HTTP/capture code. Browser capture and speech recognition
are separate component checks, not a simulated successful Zoom meeting.

Windows MCP remains unsupported. Its preflight refusal is checked without
creating the requested application directory. Standard Windows runners may be
Windows Server; the report records the actual system, release and architecture.
Their success must not be reported as Windows 11 acceptance. macOS evidence
likewise applies only to the recorded architecture and image.

Reports include command outcomes, elapsed time, package versions, model snapshot
and file hashes, test skips, synthetic transcript and recognition keyword results.
Only bounded evidence is uploaded, not virtual environments, caches or user homes.
The readonly plan checks application-home creation and separately reports changes
to source files, including Python bytecode. A completed CI run does not close
the original N24/N25 procedures requiring live Zoom, nor independent-human N26.

The fixture contains original text synthesized by the rule-based Russian eSpeak
NG voice. It contains no recording of a person and uses no neural Piper voice.
`speech.json` records generator version, settings, reference text, fixed expected
keywords, FLAC SHA256, and separate fixture/engine licensing. Regeneration requires
a local eSpeak NG shared library/data directory plus ffmpeg; CI only decodes the
already-generated fixture. The reference is fixed before recognition, and this
single robotic voice must not be used as evidence of general speech quality.
The installation gate requires a nonempty transcript and a clean STT drain;
the independent keyword check retains its actual pass/fail result. The initial
Linux pilot matched only one of three predeclared keywords. That failure was
retained; neither the reference text nor keywords were changed to match errors.

For a local run, first commit all intended source/fixture changes. Create a source
package with `tools/portable_acceptance.py pack --output <new-package-dir>
--revision <full-commit-sha>`, then execute its `portable_acceptance.py run` with
`--package <package-dir> --work <new-work-dir> --report <evidence-dir>/report.json
--revision <same-sha>`. The work directory must not exist. Explicit downloads
can take several minutes; setup does not contact Zoom or a cloud language model.
