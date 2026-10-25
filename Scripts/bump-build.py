#!/usr/bin/env python3
"""Increment the XcodeGen build number above any existing local archive."""

from pathlib import Path
import plistlib
import re
import sys


def main(project: Path, archive_dir: Path) -> None:
    source = project.read_text()
    match = re.search(r'(?m)^(\s*CURRENT_PROJECT_VERSION:\s*["\']?)(\d+)(["\']?\s*)$', source)
    if not match:
        raise SystemExit(f"{project}: missing numeric CURRENT_PROJECT_VERSION")
    old = int(match.group(2))
    last = 0
    for info in archive_dir.glob("SIMAnywhere-*.xcarchive/Info.plist"):
        with info.open("rb") as stream:
            build = plistlib.load(stream).get("ApplicationProperties", {}).get("CFBundleVersion", 0)
        last = max(last, int(build))
    new = max(old, last) + 1
    project.write_text(source[:match.start(2)] + str(new) + source[match.end(2):])
    print(f"CFBundleVersion: {old} -> {new} (last archive: {last})")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: bump-build.py project.yml archive-directory")
    main(Path(sys.argv[1]), Path(sys.argv[2]))
