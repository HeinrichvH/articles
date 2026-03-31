#!/usr/bin/env python3
"""entropy — Measure split-readiness of source files using information-theoretic metrics.

Computes a composite score based on:
  - File size (LOC relative to cognitive window ~400 LOC)
  - Concern count (distinct sections / logical groupings)
  - Semantic cohesion (optional, from external data)
  - Dependency fan-out (import/using/require count)

The tipping point formula (MDL principle, Rissanen 1978):
  Refactor when L(code|current) > L(code|new) + C(refactor)

Proxy: S(file) = 0.40*(LOC/400) + 0.25*(1-cohesion) + 0.20*(concerns/4) + 0.15*(deps/median)
  where 400 = cognitive review window (empirical), 4 = working memory chunks (Cowan 2001)

Supports: Python, JavaScript/TypeScript, Go, Rust, Java, C#, C/C++, Ruby, PHP.

Usage:
  python entropy.py src/                     Scan directory, show top 20 split candidates
  python entropy.py . --all                  Show all files with scores
  python entropy.py . --threshold 1.2        Custom threshold (default: 1.50)
  python entropy.py . --csv                  Output as CSV
  python entropy.py . --lang py              Only scan Python files
  python entropy.py . --include-tests        Include test files

References:
  Shannon (1948), Rissanen (1978), McCabe (1976), Halstead (1977),
  Cowan (2001), Peitek et al. (2021, ICSE), Sturtevant (2013, MIT)

Article: "Refactoring Is Not Heroism — An Information-Theoretic Proof"
  https://github.com/HeinrichvH/articles/blob/main/building-with-ai/01-entropy-cycle.md
"""

import argparse
import os
import re
import sys
import json
from dataclasses import dataclass
from typing import Optional


# --- Weights (from fMRI research + calibration against real splits) ---
W_SIZE = 0.40       # LOC / cognitive window (strongest signal per Peitek et al.)
W_COHESION = 0.25   # 1 - semantic cohesion
W_CONCERNS = 0.20   # concern count / working memory limit
W_DEPS = 0.15       # dependency fan-out / median

COGNITIVE_WINDOW = 400  # Code review effectiveness threshold (LOC)
WORKING_MEMORY = 4      # Working memory chunk limit (Cowan 2001)
DEFAULT_THRESHOLD = 1.50


# --- Language definitions ---

LANG_CONFIG = {
    "py": {
        "extensions": [".py"],
        "import_pattern": r"^\s*(import |from \S+ import )",
        "comment_line": "#",
        "section_markers": [r"^# ---", r"^# ===", r"^class ", r"^def "],
        "test_patterns": ["test_", "_test.py", "tests/", "conftest.py"],
        "exclude_files": ["__init__.py", "setup.py", "conftest.py"],
    },
    "js": {
        "extensions": [".js", ".jsx", ".ts", ".tsx", ".mjs"],
        "import_pattern": r"^\s*(import |require\(|from ['\"])",
        "comment_line": "//",
        "section_markers": [r"^// ---", r"^// ===", r"^export (class|function|const) "],
        "test_patterns": [".test.", ".spec.", "__tests__/", "test/"],
        "exclude_files": ["index.js", "index.ts"],
    },
    "go": {
        "extensions": [".go"],
        "import_pattern": r'^\s*"',
        "comment_line": "//",
        "section_markers": [r"^// ---", r"^func ", r"^type \w+ struct"],
        "test_patterns": ["_test.go"],
        "exclude_files": [],
    },
    "rs": {
        "extensions": [".rs"],
        "import_pattern": r"^\s*use ",
        "comment_line": "//",
        "section_markers": [r"^// ---", r"^pub fn ", r"^impl ", r"^pub struct "],
        "test_patterns": ["tests/", "#[cfg(test)]"],
        "exclude_files": ["mod.rs", "lib.rs", "main.rs"],
    },
    "java": {
        "extensions": [".java", ".kt"],
        "import_pattern": r"^\s*import ",
        "comment_line": "//",
        "section_markers": [r"^// ---", r"^\s*(public|private|protected) (class|interface) "],
        "test_patterns": ["Test.java", "Tests.java", "test/", "Test.kt"],
        "exclude_files": ["package-info.java"],
    },
    "cs": {
        "extensions": [".cs"],
        "import_pattern": r"^\s*using ",
        "comment_line": "//",
        "section_markers": [r"^#region", r"^// ---", r"^// ==="],
        "test_patterns": ["Tests.cs", ".UnitTests", ".IntegrationTests"],
        "exclude_files": ["GlobalUsings.cs", "Program.cs", "AssemblyInfo.cs"],
    },
    "c": {
        "extensions": [".c", ".cpp", ".cc", ".h", ".hpp"],
        "import_pattern": r"^\s*#include ",
        "comment_line": "//",
        "section_markers": [r"^// ---", r"^// ==="],
        "test_patterns": ["test_", "_test.", "tests/"],
        "exclude_files": [],
    },
    "rb": {
        "extensions": [".rb"],
        "import_pattern": r"^\s*require ",
        "comment_line": "#",
        "section_markers": [r"^# ---", r"^class ", r"^module "],
        "test_patterns": ["_test.rb", "_spec.rb", "test/", "spec/"],
        "exclude_files": [],
    },
    "php": {
        "extensions": [".php"],
        "import_pattern": r"^\s*use ",
        "comment_line": "//",
        "section_markers": [r"^// ---", r"^class ", r"^(public|private|protected) function "],
        "test_patterns": ["Test.php", "Tests.php", "tests/"],
        "exclude_files": [],
    },
}


@dataclass
class FileMetrics:
    path: str
    loc: int = 0
    concerns: int = 1
    imports: int = 0
    cohesion: Optional[float] = None
    score: float = 0.0
    size_score: float = 0.0
    cohesion_score: float = 0.0
    concern_score: float = 0.0
    dep_score: float = 0.0


def detect_language(filepath: str) -> Optional[str]:
    """Detect language from file extension."""
    ext = os.path.splitext(filepath)[1].lower()
    for lang, config in LANG_CONFIG.items():
        if ext in config["extensions"]:
            return lang
    return None


def count_concerns(content: str, lang: str) -> int:
    """Estimate distinct concerns via section markers and whitespace gaps."""
    config = LANG_CONFIG[lang]
    concerns = 1
    blank_run = 0

    for line in content.split('\n'):
        stripped = line.strip()

        # Section markers
        for pattern in config["section_markers"]:
            if re.match(pattern, stripped):
                concerns += 1
                break

        # Comment separators (--- or === lines)
        comment = config["comment_line"]
        if stripped.startswith(comment) and re.search(r'[-=─]{3,}', stripped):
            concerns += 1

        # Blank line gaps (3+ consecutive = intentional separation)
        if stripped == '':
            blank_run += 1
        else:
            if blank_run >= 3:
                concerns += 1
            blank_run = 0

    return min(concerns, 15)


def count_imports(content: str, lang: str) -> int:
    """Count import/using/require statements."""
    config = LANG_CONFIG[lang]
    pattern = config["import_pattern"]
    return sum(1 for line in content.split('\n') if re.match(pattern, line))


def analyze_file(filepath: str) -> Optional[FileMetrics]:
    """Analyze a single source file."""
    lang = detect_language(filepath)
    if lang is None:
        return None

    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
    except (OSError, IOError):
        return None

    lines = content.split('\n')
    loc = len([l for l in lines if l.strip()])
    if loc < 20:
        return None

    return FileMetrics(
        path=filepath,
        loc=loc,
        concerns=count_concerns(content, lang),
        imports=count_imports(content, lang),
    )


def is_test_file(filepath: str, lang: str) -> bool:
    """Check if a file is a test file."""
    config = LANG_CONFIG[lang]
    lower = filepath.lower()
    return any(p in lower for p in config["test_patterns"])


def is_excluded(filepath: str, lang: str) -> bool:
    """Check if a file should be excluded (generated, config, etc.)."""
    config = LANG_CONFIG[lang]
    basename = os.path.basename(filepath)
    return basename in config["exclude_files"]


def find_source_files(root: str, lang_filter: Optional[str] = None,
                      include_tests: bool = False) -> list[str]:
    """Find source files, respecting language filters and exclusions."""
    skip_dirs = {'bin', 'obj', 'node_modules', '.git', 'vendor', 'dist',
                 'build', '__pycache__', '.tox', '.venv', 'venv',
                 'target', '.next', '.nuxt', 'coverage'}
    files = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]

        for f in filenames:
            full = os.path.join(dirpath, f)
            lang = detect_language(full)
            if lang is None:
                continue
            if lang_filter and lang != lang_filter:
                continue
            if is_excluded(full, lang):
                continue
            if not include_tests and is_test_file(full, lang):
                continue
            # Skip generated files
            if f.endswith(('.g.cs', '.generated.cs', '.gen.go', '.pb.go',
                           '.min.js', '.d.ts', '.map')):
                continue
            files.append(full)

    return files


def load_cohesion_data(path: Optional[str] = None) -> dict[str, float]:
    """Load cohesion data from a JSON file (filename → cohesion score 0-1).

    Expected format: {"MyFile.py": 0.72, "OtherFile.ts": 0.45, ...}
    Generate with embedding-based similarity tools, or omit for default penalty.
    """
    if path and os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def compute_scores(files: list[FileMetrics], median_deps: float) -> list[FileMetrics]:
    """Compute split-readiness score for each file."""
    for f in files:
        f.size_score = f.loc / COGNITIVE_WINDOW
        f.cohesion_score = (1.0 - f.cohesion) if f.cohesion is not None else 0.4
        f.concern_score = f.concerns / WORKING_MEMORY
        f.dep_score = f.imports / median_deps if median_deps > 0 else 0

        f.score = (
            W_SIZE * f.size_score
            + W_COHESION * f.cohesion_score
            + W_CONCERNS * f.concern_score
            + W_DEPS * f.dep_score
        )

    return sorted(files, key=lambda x: x.score, reverse=True)


def print_table(files: list[FileMetrics], root: str, limit: int = 20,
                show_all: bool = False):
    """Print results as a formatted table."""
    display = files if show_all else files[:limit]

    print(f"\n{'Score':>6}  {'LOC':>5}  {'Coh':>5}  {'Con':>3}  {'Imp':>3}  Path")
    print(f"{'─'*6}  {'─'*5}  {'─'*5}  {'─'*3}  {'─'*3}  {'─'*60}")

    for f in display:
        rel = os.path.relpath(f.path, root)
        coh = f"{f.cohesion:.2f}" if f.cohesion is not None else "  n/a"
        print(f"{f.score:6.2f}  {f.loc:5d}  {coh}  {f.concerns:3d}  {f.imports:3d}  {rel}")


def print_csv(files: list[FileMetrics], root: str):
    """Output as CSV."""
    print("score,loc,cohesion,concerns,imports,path")
    for f in files:
        rel = os.path.relpath(f.path, root)
        coh = f"{f.cohesion:.4f}" if f.cohesion is not None else ""
        print(f"{f.score:.4f},{f.loc},{coh},{f.concerns},{f.imports},{rel}")


def main():
    parser = argparse.ArgumentParser(
        description='Measure split-readiness of source files (entropy cycle metric)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument('path', nargs='?', default='.',
                        help='Directory to scan (default: current directory)')
    parser.add_argument('--all', action='store_true', help='Show all files')
    parser.add_argument('--top', type=int, default=20, help='Top N candidates')
    parser.add_argument('--threshold', type=float, default=DEFAULT_THRESHOLD,
                        help=f'Score threshold (default: {DEFAULT_THRESHOLD})')
    parser.add_argument('--csv', action='store_true', help='Output as CSV')
    parser.add_argument('--above', action='store_true',
                        help='Only files above threshold')
    parser.add_argument('--lang', choices=list(LANG_CONFIG.keys()),
                        help='Only scan this language')
    parser.add_argument('--cohesion', metavar='FILE',
                        help='JSON file with cohesion data (filename → 0-1)')
    parser.add_argument('--include-tests', action='store_true',
                        help='Include test files')
    args = parser.parse_args()

    root = os.path.abspath(args.path)
    if not os.path.isdir(root):
        print(f"Error: {root} is not a directory", file=sys.stderr)
        sys.exit(1)

    # Load optional cohesion data
    cohesion_map = load_cohesion_data(args.cohesion)

    # Find and analyze files
    source_files = find_source_files(root, lang_filter=args.lang,
                                     include_tests=args.include_tests)
    metrics = []
    for fp in source_files:
        m = analyze_file(fp)
        if m is None:
            continue
        basename = os.path.basename(m.path)
        if basename in cohesion_map:
            m.cohesion = cohesion_map[basename]
        metrics.append(m)

    if not metrics:
        print("No source files found.", file=sys.stderr)
        sys.exit(1)

    # Median dependency count
    all_imports = sorted(m.imports for m in metrics)
    median_deps = all_imports[len(all_imports) // 2] if all_imports else 1

    # Score
    metrics = compute_scores(metrics, median_deps)

    if args.above:
        metrics = [m for m in metrics if m.score >= args.threshold]

    # Output
    above = sum(1 for m in metrics if m.score >= args.threshold)

    if args.csv:
        print_csv(metrics, root)
    else:
        print(f"\n  Split-Readiness Analysis (threshold: {args.threshold})")
        print(f"  {len(metrics)} files analyzed, {above} above threshold")
        print(f"  Median imports: {median_deps}")
        print_table(metrics, root, limit=args.top, show_all=args.all)

        if above > 0:
            print(f"\n  S = {W_SIZE}*(LOC/{COGNITIVE_WINDOW}) "
                  f"+ {W_COHESION}*(1-cohesion) "
                  f"+ {W_CONCERNS}*(concerns/{WORKING_MEMORY}) "
                  f"+ {W_DEPS}*(imports/median)")


if __name__ == '__main__':
    main()
