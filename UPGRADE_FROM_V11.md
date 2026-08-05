# Upgrade from v11 to v12

1. Stop the local central server, Writer, Verifier and MES.
2. Make two backups of `server/mac_server.db`.
3. Preserve your current:
   - `server/server_config.txt`
   - `mac_writer/config.txt`
   - `quality_verifier/config.ini`
   - product profile changes
4. Copy the v12 application files over v11.
5. Add the new `CLOUD_MES_*` and `PLANT_ID` settings from the supplied server config to your preserved server config.
6. Put the original production `mac_server.db` back into `server/`.
7. Start `server/run_server.bat`.
8. Confirm the local Writer and Verifier still connect.
9. Deploy Render and then enable cloud sync.

The server automatically creates the cloud queue and backfills completed Writer and Verifier records. It does not reset or reuse MAC addresses.
