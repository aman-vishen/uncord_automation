# Production Stage Log Collector Setup

## Where to run it

You can use either deployment model:

### One collector per machine

Install the collector on each stage PC. Give each installation a unique `STATION_ID`, and enable only the relevant section.

### One central collector

Run one collector on a production PC and configure UNC/network paths such as:

```ini
LOG_PATH = \\WIFI-PC\ProductionLogs\wifi_calibration.log
```

The Windows account running the collector must have read permission for the share.

## Parsing configuration

### REGEX

Use named groups where present:

```text
timestamp, mac, serial, gpon, part, router_ip, router_slot, result, detail
```

Example:

```ini
FORMAT = REGEX
LINE_REGEX = (?i)(?P<timestamp>...).*(?P<mac>...).*(?P<result>PASS|FAIL)
```

### CSV

```ini
FORMAT = CSV
CSV_COLUMNS = timestamp,mac,serial,gpon,part,result,detail
RESULT_FIELD = result
```

### JSONL

```ini
FORMAT = JSONL
MAC_FIELD = mac
SERIAL_FIELD = serial_number
RESULT_FIELD = result
```

## First-run position

- `START_POSITION = END` starts with new records only.
- `START_POSITION = BEGIN` imports the existing file from the beginning.

The initial position is stored in `collector.db`. To intentionally re-import a file, stop the collector, back up and remove `collector.db`, then use `BEGIN`. Duplicate IDs still protect the local server and cloud from duplicate inserts when the same file and byte offsets are unchanged.

## Result mapping

Configure machine-specific values:

```ini
PASS_VALUES = PASS,OK,SUCCESS
FAIL_VALUES = FAIL,NG
ERROR_VALUES = ERROR,ABORT
```

A line with no recognized result is ignored.

## Local server connection

```ini
[SERVER]
URL = http://192.168.20.10:8765
API_KEY = same-key-as-server
STATION_ID = WIFI-CAL-01
```

The collector sends batches to:

```text
POST /api/stage-log/batch
```

The local server stores the records in `stage_log_history`, queues cloud events, and synchronizes them to Render.

## Validation before production

For every stage, collect at least:

- one known PASS log record;
- one known FAIL log record;
- one malformed/incomplete record;
- a restarted application test;
- a rotated/truncated log test;
- a local server offline/reconnect test.

Confirm MAC, serial, result and timestamp are displayed correctly before enabling live production.
