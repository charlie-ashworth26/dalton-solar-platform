"""
Builds the downstream developer submission package: a ZIP whose contents
follow the requested naming convention, e.g.

  ENR-2026-000001/
    enrollment-summary.json
    enrollment-packet.pdf
    utility-bill.pdf
    lmi-document.pdf
    signed-subscription-agreement.pdf
    ny-cdg-disclosure.pdf
    signature-certificate.pdf
    validation-results.json
"""
import os
import zipfile
import json

STORAGE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "storage", "packages")
os.makedirs(STORAGE_DIR, exist_ok=True)


def build_package_zip(enrollment_code, files_by_name: dict, summary_json: dict, validation_json: dict, out_path):
    """files_by_name: {"utility-bill.pdf": "/abs/path/on/disk.pdf", ...} — only existing files are included."""
    root = f"{enrollment_code}/"
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(root + "enrollment-summary.json", json.dumps(summary_json, indent=2, default=str))
        zf.writestr(root + "validation-results.json", json.dumps(validation_json, indent=2, default=str))
        for arcname, path in files_by_name.items():
            if path and os.path.exists(path):
                zf.write(path, root + arcname)
    return out_path
