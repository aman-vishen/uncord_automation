# Upgrade to v13

1. Stop the Central Server, MES, Writer, Verifier and any previous collectors.
2. Back up `server/mac_server.db`.
3. Replace the application files with v13 while preserving your production database and private configuration values.
4. Start the Central Server once. It automatically creates `stage_log_history`.
5. Deploy the updated `mes_dashboard` to Render. It automatically adds the new PostgreSQL columns.
6. Configure `stage_log_collector/config.ini` with the real log paths and parser rules.
7. Start the Collector and verify one PASS and one FAIL from each external stage.
8. Confirm Render shows all six stages in order.

The Writer and Verifier continue to use the local Central Server. The Stage Log Collector also sends to the local server, not directly to Render.
