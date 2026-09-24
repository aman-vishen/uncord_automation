from __future__ import annotations

from pathlib import Path
import shutil
import sys

TARGET = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("label_printing/printer/app.py")

if not TARGET.exists():
    raise SystemExit(f"Label printer app not found: {TARGET}")

text = TARGET.read_text(encoding="utf-8")
original = text

text = text.replace(
    'APP_VERSION = "13.66-ETE-LABEL-ADMIN-REPRINT-MAC-OR-PCB"',
    'APP_VERSION = "13.68-ETE-LABEL-RECOVERY-CLEAR-PCB-SCAN"',
)

old_fail = '''    def _pcb_link_fail(self,msg):
        self.busy=False
        self.pcb_entry.configure(state="normal")
        self.pcb_entry.focus_set(); self.pcb_entry.selection_range(0,"end")
        self.set_status("PCB LINK ERROR",False)
        messagebox.showerror("PCB Serial Linking",msg)
'''

new_fail = '''    def _pcb_link_fail(self,msg):
        # Always clear the scanned PCB text after a failed link attempt.
        self.busy=False
        self.pcb_scan_var.set("")

        upper=(msg or "").upper()
        conflict=any(token in upper for token in (
            "ALREADY LINKED",
            "DIFFERENT PCB",
            "PCB_SERIAL_ALREADY_LINKED",
            "MAC_ALREADY_LINKED",
            "PCB SERIAL NUMBER IS ALREADY LINKED",
        ))

        if conflict:
            # Match the practical behavior of restarting the printer app:
            # abandon the local reservation view, but leave the server-side PENDING
            # label job untouched so /api/label/next skips this identity and moves
            # to the next available MAC on the next operator action.
            self.current=None
            self.mac_var.set("—")
            self.serial_var.set("—")
            self.gpon_var.set("—")
            self.pcb_var.set("—")
            self.preview.configure(text="")
            self.pcb_entry.configure(state="disabled")
            self.btn_print.configure(state="normal")
            self.btn_reprint.configure(state="disabled")
            self.set_status("PCB CONFLICT — PRESS NEXT LABEL",False)
        else:
            # Validation/network error: keep the same identity so the operator can
            # rescan the correct PCB without losing the reservation.
            self.pcb_entry.configure(state="normal")
            self.pcb_entry.focus_set(); self.pcb_entry.selection_range(0,"end")
            self.set_status("PCB LINK ERROR",False)

        messagebox.showerror("PCB Serial Linking",msg)
'''

if old_fail not in text:
    raise SystemExit("Could not find the v13.66 _pcb_link_fail block. No file was changed.")
text = text.replace(old_fail, new_fail, 1)

old_success = '''    def success(self,reprint=False):
        self.set_busy(False); self.set_status("REPRINTED" if reprint else "PRINTED ✓",True)
        self.pcb_entry.configure(state="disabled")
        self.btn_reprint.configure(state="normal")
        self.count_var.set(f"Printed: {self.printed}   Reprints: {self.reprints}")
        if truth(self.cfg.get("AUTO_PRINT_NEXT","0")) and not reprint:
            self.after(int(float(self.cfg.get("AUTO_PRINT_DELAY_MS","500"))),self.print_next)
'''

new_success = '''    def success(self,reprint=False):
        self.set_busy(False); self.set_status("REPRINTED" if reprint else "PRINTED ✓",True)
        # Clear the scan box immediately after a successful physical print.
        self.pcb_scan_var.set("")
        self.pcb_entry.configure(state="disabled")
        self.btn_reprint.configure(state="normal")
        self.count_var.set(f"Printed: {self.printed}   Reprints: {self.reprints}")
        if truth(self.cfg.get("AUTO_PRINT_NEXT","0")) and not reprint:
            self.after(int(float(self.cfg.get("AUTO_PRINT_DELAY_MS","500"))),self.print_next)
'''

if old_success not in text:
    raise SystemExit("Could not find the v13.66 success block. No file was changed.")
text = text.replace(old_success, new_success, 1)

old_scan_success = '''    def _scanned_reprint_success(self,identity):
        self.set_busy(False)
        self.set_status("REPRINTED ✓",True)
        self.count_var.set(f"Printed: {self.printed}   Reprints: {self.reprints}")
'''
new_scan_success = '''    def _scanned_reprint_success(self,identity):
        self.set_busy(False)
        self.pcb_scan_var.set("")
        self.set_status("REPRINTED ✓",True)
        self.count_var.set(f"Printed: {self.printed}   Reprints: {self.reprints}")
'''
if old_scan_success in text:
    text = text.replace(old_scan_success, new_scan_success, 1)

if text == original:
    raise SystemExit("No changes were made.")

backup = TARGET.with_suffix(TARGET.suffix + ".v13_66.bak")
if not backup.exists():
    shutil.copy2(TARGET, backup)

TARGET.write_text(text, encoding="utf-8")
compile(text, str(TARGET), "exec")

print(f"Patched successfully: {TARGET}")
print(f"Backup: {backup}")
print("Version: v13.68")
print("Behavior:")
print(" - PCB scan field clears after every successful print.")
print(" - PCB conflict clears the old MAC/SN/GPON from the UI.")
print(" - NEXT LABEL is immediately available after a conflict.")
print(" - Non-conflict errors keep the current identity so the PCB can be rescanned.")
