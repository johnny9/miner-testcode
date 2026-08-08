# miner-testcode

`miner-testcode` is a Python `unittest` runner for repeatable, end-to-end tests
against real Bitcoin mining devices. Tests describe capabilities and normalized
state instead of a particular ASIC or firmware API. Device adapters own the
hardware-specific behavior.

The ESP-Miner adapter targets both the board 1002 Bitaxe Bonanza and board 602
Bitaxe Gamma. The first test independently handshakes with Public Pool's
Stratum V1 server while the device is monitored over HTTP and its ESP USB
serial log is captured. The 602 profile uses only the ESP-Miner application and
AxeOS artifacts; it has no separate bridge firmware lifecycle.

## What is implemented

- Generic device, capability, clean-state, pool, state, and upgrade contracts.
- `BitaxeBonanzaDevice` identity checks for board `1002` and ASIC `BZM`.
- Concurrent API polling and serial-log capture while an async test runs.
- Serialized, bounded API operations with safe read retries and per-request
  JSONL traces; writes are never retried automatically.
- ESP USB serial resolution through stable `/dev/serial/by-id` paths.
- Shell-free USB flash commands with named `{port}`, `{factory}`, `{application}`,
  and `{web}` substitutions.
- Paced AxeOS/ESP-Miner OTA uploads (`www.bin` before `esp-miner.bin`) and
  post-reboot version verification.
- Per-test baseline capture and restoration of pool settings and pause state.
- Runner, test, serial, device API, normalized state, upgrade, and outcome logs.
- A publication privacy pass that redacts pool identities and removes host,
  device, configuration, and artifact absolute paths from text evidence.
- A generic Public Pool smoke test using `unittest.IsolatedAsyncioTestCase`.
- Bitaxe 602/BM1370 identity and standard ESP-Miner telemetry normalization.
- Configurable local HTML/JSON, GitHub Check Run, and Mining QA Status result
  publishers.

## Architecture

```text
TOML configuration
  -> custom unittest runner
    -> generic MinerTestCase lifecycle
      -> capability-selected test
      -> MiningDevice abstraction
        -> Bitaxe Bonanza adapter
          -> AxeOS HTTP API (state, settings, OTA, logs)
          -> AxeOS live WebSocket (2 Hz telemetry, REST fallback)
          -> ESP USB serial (capture, optional flash command)
      -> independent Stratum V1 probe
      -> per-test artifacts and guaranteed cleanup
    -> aggregate RunSummary
      -> local HTML and JSON
      -> GitHub Check Run
      -> Mining QA Status result and signed artifact uploads
```

`MiningDevice` is the extension point for another miner family. An adapter maps
its native API into `DeviceState`, advertises capabilities, snapshots only the
mutable settings that tests may touch, and restores them even after an assertion
or setup error. A test declares `required_capabilities`; it is skipped on devices
that do not provide them.

The normalized state currently includes online/identity status, the native
lifecycle when one is exposed, a portable `mining_active` signal, hashrate,
accepted/rejected shares, active/expected engines, pool address, work age,
uptime, and a fault code. `mining_active` requires positive observed hashrate
and rejects paused, faulted, overheated, maintenance, and safe-off states, so
tests do not depend on a device-specific lifecycle label. `DeviceStateStore`
publishes updates through an
`asyncio.Condition`, so tests wait on new observations without blocking the API,
WebSocket telemetry, or serial monitor. Every device adapter also owns a generic
`TelemetryCapture`. The Bonanza adapter maps native data to hashrate (GH/s),
board temperature (°C), applied frequency (MHz), and fan speed (RPM).

## Configuration

Copy the example and adjust only local coordinates:

```bash
cp configs/bitaxe-bonanza.example.toml config.local.toml
```

The important shape is intentionally generic:

```toml
[[devices]]
name = "bonanza-lab-1"
type = "bitaxe_bonanza"

[devices.interfaces.api]
base_url = "http://bitaxe.local"

[devices.interfaces.websocket]
enabled = true
# url is derived as ws://bitaxe.local/api/ws/live when omitted
required = false

[devices.options]
read_only = false
publication_name = "Bitaxe Bonanza 1002"

[devices.interfaces.serial]
port = "/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_*-if00"

[devices.interfaces.upgrade]
enabled = false
method = "ota"

[tests.public_pool_smoke]
host = "public-pool.io"
port = 3333
username_env = "MINER_TEST_POOL_USER"
configure_device = true
```

An exact value such as `${MINER_TEST_POOL_PASSWORD}` is read from the environment
at runtime. Request and run metadata never serialize HTTP bodies or the resolved
configuration, so write-only pool passwords are not copied into artifacts.

When `username` is omitted, the smoke test reads `MINER_TEST_POOL_USER` (or the
variable named by `username_env`). The optional password follows the same rule
with `MINER_TEST_POOL_PASSWORD` and otherwise uses the conventional Stratum `x`
for the independent probe. It is not written to the device unless
`configure_device_password=true`. Because AxeOS does not reveal that write-only
value, changing it also requires `devices.options.baseline_stratum_password_env`
so cleanup can restore the original from process memory. Firmware upgrades are
opt-in. OTA needs `application` and may also provide `web`; USB flashing needs a
serial `flash_command` and whichever artifact names that command references.

Set `devices.options.read_only=true` for an observational run. This is enforced
inside the HTTP interface: PATCH, POST, and firmware uploads are rejected even if
a test or adapter accidentally requests one. The requested pool must already
match the miner in that mode. Pair it with `configure_device=false`; the Stratum
probe can then use an explicit public test identity while the runner only checks
the device's existing pool host and port.

The target firmware becomes the run baseline. It is not automatically rolled
back after each test; mutable device settings are. This avoids repeatedly
flashing hardware while still giving every test a clean configuration.

`devices.options.publication_name` is the stable device label used in reports,
artifact names, and remote payloads. The private configuration name remains
available for local device selection but is replaced before publication. MAC
addresses, private IP addresses, Wi-Fi identifiers, and pool identities are
also removed from published text evidence.

For a Bitaxe 602, use `type = "bitaxe_602"`, set the API URL to the device, and
configure OTA with only `application` and `web` artifacts. Do not configure a
bridge artifact; board 602 does not have a separate bridge firmware.

## Run

Python 3.11 or newer and the declared `websockets` dependency are required.

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
miner-test --config config.local.toml
```

Without installation:

```bash
PYTHONPATH=src python3 -m miner_testcode --config config.local.toml
```

Select one configured device or a narrower test filename with:

```bash
miner-test --config config.local.toml --device bonanza-lab-1
miner-test --config config.local.toml --pattern 'test_public_pool_smoke.py'
```

Framework unit tests remain normal `unittest` tests and do not touch hardware:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests/unit -v
```

Each run creates one timestamped directory below `artifacts/`. Every device/test
pair gets `test.log`, `device-state.jsonl`, `telemetry.jsonl`, `api.jsonl`,
`serial.log`, a baseline, and the downloaded device log. Cleanup failures are
test errors, never hidden.

### Chart markers

The runner registers a `CHART` log level between `INFO` and `WARNING`. A test can
annotate an important moment with the convenience method:

```python
self.chart("Healthy mining and fresh pool work observed", status="good")
```

Library-style tests can use the logging API directly with
`logger.log(miner_testcode.CHART_LEVEL, "label")` or
`miner_testcode.log_chart(logger, "label")`.

Use `status="good"` for successful milestones and `status="bad"` for failures;
informational markers are the default. The test runner also appends a green or
red final outcome marker automatically. The message remains in `test.log` and
becomes a labeled vertical line in both
the local and Mining QA Status telemetry charts. Device lifecycle, firmware
readiness, test-body start, and clean-state restoration are marked
automatically. Marker text is passed through the same privacy redaction as
published logs. The full stream remains in `telemetry.jsonl`; structured result
payloads retain at most 2,000 evenly spaced samples, including both endpoints.

## Result publishers

Publishers run after unittest and still run when tests fail. An enabled publisher
is required by default: if publishing fails, the command exits unsuccessfully in
addition to preserving the test result and local artifacts. Set `required=false`
for a best-effort destination.

Before either remote publisher runs, the runner resolves its own GitHub `origin`
and exact `HEAD`. Remote publication is refused if tracked harness code is dirty
or that commit is not present in a local `origin/*` ref. Results therefore carry
two distinct revisions: the configured firmware repository/commit under test and
the automatically discovered `miner-testcode` revision that executed it. Every
test result links to its exact GitHub blob and source line.

Published text artifacts receive a final privacy pass. Paths inside this
repository become relative, artifact paths become `<artifacts>/...`, unrelated
absolute host or device paths become `<local-path>`, and configured pool
identities and keyed secrets are redacted. Publisher metadata uses relative
report names rather than `file://` URLs.

### Local HTML

```toml
[publishers.local]
enabled = true
required = true
filename = "report.html"
json_filename = "result.json"
```

`report.html` summarizes the native unittest results, renders telemetry as four
aligned time-series rows with shared vertical event markers, and links to every
log and artifact in each test directory. `result.json` contains the same
aggregate data for other automation. Both are written inside the timestamped
run directory. The report header links to the exact test-harness commit, and
each test name links to the executed test method at that commit.

### GitHub Check Run

```toml
[publishers.github]
enabled = true
required = true
name = "miner-testcode / hardware-e2e"
token_env = "GITHUB_TOKEN"
repository_env = "GITHUB_REPOSITORY"
sha_env = "GITHUB_SHA"
```

Check Run writes require a GitHub App installation token. GitHub Actions'
`GITHUB_TOKEN` is such a token, but the workflow must grant the permission:

```yaml
permissions:
  contents: read
  checks: write

steps:
  - uses: actions/checkout@v4
  - name: Run hardware tests
    run: miner-test --config config.ci.toml
```

A normal personal access token cannot create a Check Run. For a local runner,
set the configured token variable to a GitHub App installation token. The check
is created directly in its terminal state and includes the test table. If Mining
QA Status also publishes successfully, its durable result page becomes the
check's details URL. The Check summary includes the same pinned test-code links.

### Mining QA Status

```toml
[publishers.mining_qa_status]
enabled = true
required = true
base_url = "https://mining-qa-status.vercel.app"
token_env = "MINING_QA_TOKEN"
repository_env = "GITHUB_REPOSITORY"
commit_sha_env = "GITHUB_SHA"
target_type = "bitaxe"
target_name = "Bitaxe Bonanza 1002"
suite = "miner-testcode"
upload_artifacts = true
```

The publisher posts the aggregate result to `/api/v1/results`, requests a signed
upload URL for each selected artifact, uploads directly to private Supabase
Storage, and completes each reservation. This avoids sending large logs through
the application server. `artifact_globs` controls which run files are uploaded;
the full example includes the HTML/JSON report, runner events, test logs, device
state, serial output, device logs, and the Stratum probe result.

For a local publication, provide repository metadata without putting the token
in a command argument or file:

```bash
read -rsp 'Mining QA publisher token: ' MINING_QA_TOKEN
export MINING_QA_TOKEN
export GITHUB_REPOSITORY='owner/repository'
export GITHUB_SHA="$(git rev-parse HEAD)"
miner-test --config config.local.toml
```

In GitHub Actions those repository and revision variables are detected
automatically. Reusing the same `GITHUB_RUN_ID` updates the existing Mining QA
record instead of creating a duplicate.

## Adding a device type

1. Implement `MiningDevice` in `src/miner_testcode/devices/`.
2. Normalize native telemetry into `DeviceState` and keep its monitor async.
3. Advertise only capabilities actually backed by configured interfaces.
4. Implement a bounded, idempotent upgrade and a verified clean-state restore.
5. Register the type in `devices/__init__.py`.

Existing generic tests then run unchanged if the adapter provides their required
capabilities. Device-only tests can still declare a more specific capability
without adding model checks to shared test logic.
