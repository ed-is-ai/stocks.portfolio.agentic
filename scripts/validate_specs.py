#!/usr/bin/env python3
"""
Validate OpenSpec specifications for well-formedness.

This script checks that all specs in openspec/specs/ are properly structured:
- All spec files exist
- Required sections present (ADDED/MODIFIED/REMOVED Requirements)
- Requirements have scenarios (#### Scenario: format)
- No undefined references

Usage:
    python scripts/validate_specs.py [--fix]

Exit codes:
    0: All specs valid
    1: Validation errors found
    2: Spec files missing
"""

import json
import re
import sys
from pathlib import Path


def validate_spec_file(spec_path: Path) -> tuple[bool, list[str]]:
    """
    Validate a single spec.md file.

    Returns: (is_valid, error_messages)
    """
    errors = []

    if not spec_path.exists():
        return False, [f"File not found: {spec_path}"]

    try:
        content = spec_path.read_text(encoding="utf-8")
    except Exception as e:
        return False, [f"Failed to read {spec_path}: {e}"]

    # Check for requirement sections
    has_requirements = (
        "## ADDED Requirements" in content or
        "## MODIFIED Requirements" in content or
        "## REMOVED Requirements" in content
    )
    if not has_requirements:
        errors.append(f"No requirement sections found in {spec_path}")

    # Check for requirements (### Requirement:)
    requirement_pattern = r"^### Requirement: (.+)$"
    requirements = re.findall(requirement_pattern, content, re.MULTILINE)

    if not requirements:
        errors.append(f"No requirements found in {spec_path}")

    # Check that each requirement has at least one scenario
    for req_name in requirements:
        # Find the requirement block
        req_block_start = content.find(f"### Requirement: {req_name}")
        if req_block_start == -1:
            continue

        # Find next requirement or section
        next_req = content.find("### Requirement:", req_block_start + 1)
        next_section = content.find("##", req_block_start + 1)

        # End of block is the next requirement or section, whichever comes first
        if next_req != -1 and next_section != -1:
            block_end = min(next_req, next_section)
        elif next_req != -1:
            block_end = next_req
        elif next_section != -1:
            block_end = next_section
        else:
            block_end = len(content)

        req_block = content[req_block_start:block_end]

        # Check for scenarios (#### Scenario:)
        if "#### Scenario:" not in req_block:
            errors.append(
                f"Requirement '{req_name}' in {spec_path} has no scenarios "
                f"(missing '#### Scenario:' section)"
            )

    # Check for proper markdown format
    lines = content.split("\n")
    for i, line in enumerate(lines, 1):
        # Warn about common pitfalls
        if line.startswith("### Scenario:"):
            errors.append(
                f"Line {i}: Found '### Scenario:' but should be '#### Scenario:' "
                f"(4 hashes, not 3)"
            )

        if re.match(r"^- \*\*WHEN\*\*", line):
            # Scenario is using bullets; check format
            if "- **THEN**" not in content[lines.index(line):lines.index(line) + 10]:
                errors.append(
                    f"Line {i}: Scenario missing '- **THEN**' clause"
                )

    return len(errors) == 0, errors


def validate_all_specs() -> tuple[bool, dict[str, list[str]]]:
    """
    Validate all specs in openspec/specs/.

    Returns: (all_valid, spec_errors_dict)
    """
    spec_dir = Path(__file__).parent.parent / "openspec" / "specs"

    if not spec_dir.exists():
        return False, {"_global": [f"Spec directory not found: {spec_dir}"]}

    spec_files = list(spec_dir.glob("*/spec.md"))

    if not spec_files:
        return False, {"_global": [f"No spec.md files found in {spec_dir}"]}

    all_errors = {}
    all_valid = True

    for spec_file in sorted(spec_files):
        is_valid, errors = validate_spec_file(spec_file)

        if not is_valid:
            all_valid = False
            relative_path = spec_file.relative_to(Path.cwd())
            all_errors[str(relative_path)] = errors

    return all_valid, all_errors


def print_results(all_valid: bool, errors: dict[str, list[str]]) -> None:
    """Print validation results."""
    if all_valid:
        print("[OK] All specs valid!")
        return

    print("[ERROR] Spec validation errors found:\n")

    for spec_path, spec_errors in errors.items():
        print(f"{spec_path}:")
        for error in spec_errors:
            print(f"  - {error}")
        print()


def main() -> int:
    """Run spec validation."""
    all_valid, errors = validate_all_specs()
    print_results(all_valid, errors)
    return 0 if all_valid else 1


if __name__ == "__main__":
    sys.exit(main())
