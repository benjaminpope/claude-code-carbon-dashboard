#!/usr/bin/env bash
# Install claude-code-carbon-dashboard: copy the tools into ~/.claude/carbon,
# add a Stop hook to ~/.claude/settings.json that refreshes the dashboard after
# every turn, and build the dashboard once. Safe to re-run; it updates the copy
# in place.
set -euo pipefail

repo="$(cd "$(dirname "$0")" && pwd)"
dest="${CLAUDE_CARBON_HOME:-$HOME/.claude/carbon}"
settings="$HOME/.claude/settings.json"

mkdir -p "$dest"
# The collectors and the report are flat modules imported as siblings of
# token_carbon.py, so they are copied alongside it.
cp "$repo/token_carbon.py" "$repo/dashboard_template.html" "$repo"/carbon_*.py "$dest/"
chmod +x "$dest/token_carbon.py" "$dest/carbon_report.py"

python3 - "$settings" "$dest" <<'EOF'
import json, shutil, sys
from pathlib import Path

settings, dest = Path(sys.argv[1]), sys.argv[2]
command = (f"python3 {dest}/token_carbon.py --html {dest}/dashboard.html"
           " >/dev/null 2>&1 || true")
data = json.loads(settings.read_text()) if settings.exists() else {}
stop = data.setdefault("hooks", {}).setdefault("Stop", [])
existing = [h for group in stop for h in group.get("hooks", [])
            if "token_carbon.py" in h.get("command", "")]
if existing:
    for h in existing:
        h.update(type="command", command=command, timeout=60, **{"async": True})
    print(f"Updated the existing Stop hook in {settings}")
else:
    if settings.exists():
        shutil.copy(settings, settings.with_suffix(".json.bak"))
    stop.append({"hooks": [{"type": "command", "command": command,
                            "async": True, "timeout": 60}]})
    print(f"Added a Stop hook to {settings}")
settings.parent.mkdir(parents=True, exist_ok=True)
settings.write_text(json.dumps(data, indent=2) + "\n")
EOF

python3 "$dest/token_carbon.py" --html "$dest/dashboard.html" 2>/dev/null >/dev/null
echo "Dashboard: $dest/dashboard.html"
echo "Open it with: open \"$dest/dashboard.html\""
