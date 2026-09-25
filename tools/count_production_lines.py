from pathlib import Path

root = Path(__file__).resolve().parents[1]
total = 0
for path in sorted((root / "app").rglob("*.py")):
    count = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    total += count
    print(f"{count:5d} {path.relative_to(root)}")
print(f"production_nonblank_lines={total}")
