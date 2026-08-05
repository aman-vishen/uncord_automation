# ETE Production Stage Log Collector

This Windows desktop application monitors production log files and sends new stage results to the local ETE Central Production Server. The central server stores each record locally and synchronizes it to the Render MES.

Supported production stages:

1. Wi-Fi Calibration
2. Label Printing
3. BOB Calibration
4. Wi-Fi Coupling & VoIP

MAC Write and Verification are already reported by their existing desktop applications.

## Deployment options

Run one collector on every stage PC with only that stage enabled, or run one collector on a central PC that can access all four log files through Windows network shares.

## Input formats

- `REGEX`: one completed test per line using named groups.
- `CSV`: one completed test per line with columns configured through `CSV_COLUMNS`.
- `JSONL`: one JSON object per line with configurable field names.

The required result maps to `PASS`, `FAIL`, or `ERROR`. Records without a recognized result are ignored.

## Safety and duplicate prevention

- File byte offsets are stored in `collector.db`.
- Parsed records enter a durable local queue before upload.
- Internet access is not required on stage PCs; only access to the local server is needed.
- Every record gets a deterministic event ID based on stage, file, byte offset and content.
- Local server and Render both reject duplicate event IDs.
- Log rotation/truncation is detected automatically.

## First setup

1. Edit `config.ini`.
2. Set the local `SERVER.URL`, API key and station ID.
3. Set each `LOG_PATH` and parser configuration.
4. Use `START_POSITION = END` for live production. Use `BEGIN` once when importing an existing file.
5. Run `run_collector.bat`.

The sample files under `sample_logs` demonstrate REGEX, CSV and JSONL formats. Parser patterns are examples and must be adjusted to the exact log format produced by each machine/software package.
