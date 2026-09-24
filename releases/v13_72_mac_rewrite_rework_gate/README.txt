ETE SOLUTIONS INDIA - MAC REWRITE v13.72

PURPOSE
-------
Admin-only MAC Rewrite / rework station.

LOGIN
-----
Username: admin
Password: 9278456590

IMPORTANT PRODUCTION RULE
-------------------------
A DUT that already PASSed Final Verification is BLOCKED from running normal
Final Verification again.

The only exception is an explicit MAC Rewrite rework authorization:
1. Admin runs MAC Rewrite.
2. The same existing MAC / Serial / GPON identity is rewritten.
3. MAC pool PASS state is NOT changed and the identity is NOT reallocated.
4. When rewrite verification passes, the server creates an ACTIVE rework authorization.
5. Final Verification may run again.
6. If FV FAILs or ERRORs, authorization stays ACTIVE so the repaired DUT may retry.
7. When FV PASSes, authorization is CONSUMED.
8. A further normal FV run is blocked again.

INSTALLATION
------------
CENTRAL SERVER PC - once:
  Run PATCH_SERVER_ONCE.bat
  Select your current production server.py
  Restart the Central Server app.

MAC REWRITE PC:
  Edit mac_rewrite\config.txt and confirm:
    SERVER_URL
    SERVER_API_KEY
    ROUTER_n_SOURCE_IP values
  Then double-click RUN_MAC_REWRITE.bat

MAC REWRITE FLOW
----------------
Scan existing MAC / Serial / GPON / PCB
-> firmware update
-> firmware reboot verification
-> firstboot -y -r
-> wait for Telnet
-> prolinecmd clearall
-> prolinecmd factorymode set 1
-> rewrite SAME MAC
-> rewrite SAME Serial
-> rewrite SAME GPON
-> prolinecmd telnetEnable set 1
-> firstboot -y -r
-> wait for Telnet
-> verify MAC / Serial / GPON
-> authorize Final Verification rework
-> PASS

NOTE
----
The server patcher creates:
  server.py.before_v13_72_rework.bak

Do not delete your existing production database.
