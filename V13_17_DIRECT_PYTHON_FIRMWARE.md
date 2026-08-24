# v13.17 direct Python firmware updater

Replaces the v13.16 mcupgrade child-process workflow with the supplied OpenWrt Python updater logic. Firmware remains server-selected and server-controlled ON/OFF. The writer downloads and SHA-256 verifies the image, then performs SSH/SFTP upload, `sysupgrade -T`, `sysupgrade`, reboot wait, SSH reconnect and post-upgrade health verification.

Production adaptation: SSH and TCP readiness checks bind to each DUT's `ROUTER_n_SOURCE_IP`, allowing eight routers that all use `192.168.2.1` to be upgraded independently.

Firmware still runs before identity reservation and before `prolinecmd clearall`; if firmware update fails, MAC/Serial/GPON are not written.
