#!/usr/bin/env python3
"""entropy — Measure split-readiness of source files using information-theoretic metrics.

Computes a composite score based on:
  - File size (LOC relative to cognitive window ~400 LOC)
  - Cyclomatic complexity damper (avg CC per function / codebase median)
  - Concern count (distinct sections / logical groupings)
  - Semantic cohesion (via local ollama embeddings or external JSON)
  - Dependency fan-out (import/using/require count)

The tipping point formula (MDL principle, Rissanen 1978):
  Refactor when L(code|current) > L(code|new) + C(refactor)

Proxy: S(file) = 0.40*(LOC/400)*(avg_cc/baseline) + 0.25*(1-cohesion) + 0.20*(concerns/4) + 0.15*(deps/median)
  where 400 = cognitive review window (empirical), 4 = working memory chunks (Cowan 2001),
  and avg_cc/baseline dampens size for simple boilerplate and amplifies it for complex logic

Supports: Python, JavaScript/TypeScript, Go, Rust, Java, C#, C/C++, Ruby, PHP.

Usage:
  python entropy.py src/                     Scan directory, show top 20 split candidates
  python entropy.py . --all                  Show all files with scores
  python entropy.py . --threshold 1.2        Custom threshold (default: 1.50)
  python entropy.py . --csv                  Output as CSV
  python entropy.py . --lang py              Only scan Python files
  python entropy.py . --cohesion auto         Auto-compute cohesion via ollama embeddings
  python entropy.py . --include-tests        Include test files

References:
  Shannon (1948), Rissanen (1978), McCabe (1976), Halstead (1977),
  Cowan (2001), Peitek et al. (2021, ICSE), Sturtevant (2013, MIT)

Article: "Refactoring Is Not Heroism — An Information-Theoretic Proof"
  https://github.com/HeinrichvH/articles/blob/main/building-with-ai/01-entropy-cycle.md
"""

import argparse
import hashlib
import http.client
import json
import math
import os
import re
import sys
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

# --- Cyclomatic complexity heuristic ---
BRANCH_KEYWORDS = re.compile(
    r'\b(if|elif|else\s+if|elsif|elseif|for|foreach|while|do'
    r'|switch|case|catch|except|when)\b'
)
LOGIC_OPS = re.compile(r'&&|\|\|')


# --- Language definitions ---

LANG_CONFIG = {
    "py": {
        "extensions": [".py"],
        "import_pattern": r"^\s*(import |from \S+ import )",
        "comment_line": "#",
        "section_markers": [r"^# ---", r"^# ===", r"^class ", r"^def "],
        "function_pattern": r"^\s*(async\s+)?def\s+\w+",
        "test_patterns": ["test_", "_test.py", "tests/", "conftest.py"],
        "exclude_files": ["__init__.py", "setup.py", "conftest.py"],
    },
    "js": {
        "extensions": [".js", ".jsx", ".ts", ".tsx", ".mjs"],
        "import_pattern": r"^\s*(import |require\(|from ['\"])",
        "comment_line": "//",
        "section_markers": [r"^// ---", r"^// ===", r"^export (class|function|const) "],
        "function_pattern": r"^\s*(?:export\s+)?(?:async\s+)?function\b",
        "test_patterns": [".test.", ".spec.", "__tests__/", "test/"],
        "exclude_files": ["index.js", "index.ts"],
    },
    "go": {
        "extensions": [".go"],
        "import_pattern": r'^\s*"',
        "comment_line": "//",
        "section_markers": [r"^// ---", r"^func ", r"^type \w+ struct"],
        "function_pattern": r"^func\s",
        "test_patterns": ["_test.go"],
        "exclude_files": [],
    },
    "rs": {
        "extensions": [".rs"],
        "import_pattern": r"^\s*use ",
        "comment_line": "//",
        "section_markers": [r"^// ---", r"^pub fn ", r"^impl ", r"^pub struct "],
        "function_pattern": r"^\s*(?:pub\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s",
        "test_patterns": ["tests/", "#[cfg(test)]"],
        "exclude_files": ["mod.rs", "lib.rs", "main.rs"],
    },
    "java": {
        "extensions": [".java", ".kt"],
        "import_pattern": r"^\s*import ",
        "comment_line": "//",
        "section_markers": [r"^// ---", r"^\s*(public|private|protected) (class|interface) "],
        "function_pattern": r"^\s*(?:public|private|protected)[\w\s<>]*\s+\w+\s*\(",
        "test_patterns": ["Test.java", "Tests.java", "test/", "Test.kt"],
        "exclude_files": ["package-info.java"],
    },
    "cs": {
        "extensions": [".cs"],
        "import_pattern": r"^\s*using ",
        "comment_line": "//",
        "section_markers": [r"^#region", r"^// ---", r"^// ==="],
        "function_pattern": r"^\s*(?:public|private|protected|internal)[\w\s<>]*\s+\w+\s*\(",
        "test_patterns": ["Tests.cs", ".UnitTests", ".IntegrationTests"],
        "exclude_files": ["GlobalUsings.cs", "Program.cs", "AssemblyInfo.cs"],
    },
    "c": {
        "extensions": [".c", ".cpp", ".cc", ".h", ".hpp"],
        "import_pattern": r"^\s*#include ",
        "comment_line": "//",
        "section_markers": [r"^// ---", r"^// ==="],
        "function_pattern": r"^[a-zA-Z_][\w\s*]+\w+\s*\([^;]*$",
        "test_patterns": ["test_", "_test.", "tests/"],
        "exclude_files": [],
    },
    "rb": {
        "extensions": [".rb"],
        "import_pattern": r"^\s*require ",
        "comment_line": "#",
        "section_markers": [r"^# ---", r"^class ", r"^module "],
        "function_pattern": r"^\s*def\s+\w+",
        "test_patterns": ["_test.rb", "_spec.rb", "test/", "spec/"],
        "exclude_files": [],
    },
    "php": {
        "extensions": [".php"],
        "import_pattern": r"^\s*use ",
        "comment_line": "//",
        "section_markers": [r"^// ---", r"^class ", r"^(public|private|protected) function "],
        "function_pattern": r"^\s*(?:public\s+|private\s+|protected\s+|static\s+)*function\s+\w+",
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
    avg_cc: float = 1.0
    cohesion: Optional[float] = None
    score: float = 0.0
    size_score: float = 0.0
    cohesion_score: float = 0.0
    concern_score: float = 0.0
    dep_score: float = 0.0
    cc_ratio: float = 1.0


def detect_language(filepath: str) -> Optional[str]:
    """Detect language from file extension."""
    ext = os.path.splitext(filepath)[1].lower()
    for lang, config in LANG_CONFIG.items():
        if ext in config["extensions"]:
            return lang
    return None


def find_section_boundaries(content: str, lang: str) -> list[int]:
    """Find line indices where logical section boundaries occur."""
    config = LANG_CONFIG[lang]
    lines = content.split('\n')
    boundaries = []
    blank_run = 0

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Section markers
        for pattern in config["section_markers"]:
            if re.match(pattern, stripped):
                boundaries.append(i)
                break

        # Comment separators (--- or === lines)
        comment = config["comment_line"]
        if stripped.startswith(comment) and re.search(r'[-=─]{3,}', stripped):
            boundaries.append(i)

        # Blank line gaps (3+ consecutive = intentional separation)
        if stripped == '':
            blank_run += 1
        else:
            if blank_run >= 3:
                boundaries.append(i)
            blank_run = 0

    return sorted(set(boundaries))


def count_concerns(content: str, lang: str) -> int:
    """Estimate distinct concerns via section markers and whitespace gaps."""
    return min(len(find_section_boundaries(content, lang)) + 1, 15)


def split_sections(content: str, lang: str) -> list[str]:
    """Split file content into logical sections for cohesion analysis."""
    lines = content.split('\n')
    boundaries = find_section_boundaries(content, lang)

    if not boundaries:
        return [content]

    cuts = [0] + boundaries + [len(lines)]
    sections = []
    for i in range(len(cuts) - 1):
        section = '\n'.join(lines[cuts[i]:cuts[i + 1]])
        # Merge tiny sections (<3 non-blank lines) into previous
        non_blank = sum(1 for l in lines[cuts[i]:cuts[i + 1]] if l.strip())
        if non_blank < 3 and sections:
            sections[-1] += '\n' + section
        else:
            sections.append(section)

    return [s for s in sections if s.strip()]


def count_imports(content: str, lang: str) -> int:
    """Count import/using/require statements."""
    config = LANG_CONFIG[lang]
    pattern = config["import_pattern"]
    return sum(1 for line in content.split('\n') if re.match(pattern, line))


def estimate_complexity(content: str, lang: str) -> float:
    """Estimate average cyclomatic complexity per function (heuristic).

    Counts branching keywords between function boundaries.
    Returns average (branches + 1) per function, or 1.0 if no functions detected.
    """
    config = LANG_CONFIG[lang]
    func_pattern = config.get("function_pattern")
    if not func_pattern:
        return 1.0

    lines = content.split('\n')
    comment_char = config["comment_line"]

    func_starts = [i for i, line in enumerate(lines) if re.match(func_pattern, line)]
    if not func_starts:
        return 1.0

    complexities = []
    for idx, start in enumerate(func_starts):
        end = func_starts[idx + 1] if idx + 1 < len(func_starts) else len(lines)
        branches = 0
        for line in lines[start:end]:
            stripped = line.strip()
            if stripped.startswith(comment_char):
                continue
            branches += len(BRANCH_KEYWORDS.findall(stripped))
            branches += len(LOGIC_OPS.findall(stripped))
        complexities.append(1 + branches)

    return sum(complexities) / len(complexities)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def embed_sections(sections: list[str], model: str) -> list[list[float]]:
    """Embed text sections via ollama's /api/embed endpoint."""
    truncated = [s[:6000] for s in sections]
    body = json.dumps({"model": model, "input": truncated})
    conn = http.client.HTTPConnection("localhost", 11434, timeout=30)
    try:
        conn.request("POST", "/api/embed", body=body,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        if "embeddings" in data:
            return data["embeddings"]
    finally:
        conn.close()
    # Fallback for older ollama: single-prompt endpoint
    embeddings = []
    for section in truncated:
        body = json.dumps({"model": model, "prompt": section})
        c = http.client.HTTPConnection("localhost", 11434, timeout=30)
        try:
            c.request("POST", "/api/embeddings", body=body,
                      headers={"Content-Type": "application/json"})
            resp = c.getresponse()
            result = json.loads(resp.read())
            embeddings.append(result.get("embedding", []))
        finally:
            c.close()
    return embeddings


def compute_file_cohesion(embeddings: list[list[float]]) -> float:
    """Average pairwise cosine similarity across section embeddings."""
    if len(embeddings) <= 1:
        return 1.0
    pairs = []
    for i in range(len(embeddings)):
        for j in range(i + 1, len(embeddings)):
            if embeddings[i] and embeddings[j]:
                pairs.append(cosine_similarity(embeddings[i], embeddings[j]))
    return sum(pairs) / len(pairs) if pairs else 1.0


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
        avg_cc=estimate_complexity(content, lang),
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


def compute_auto_cohesion(source_files: list[str], root: str,
                          model: str = "nomic-embed-text",
                          use_cache: bool = True) -> dict[str, float]:
    """Compute cohesion scores via local ollama embeddings.

    Splits each file into sections, embeds them, and computes average
    pairwise cosine similarity as the cohesion score (0-1).
    """
    # Probe ollama
    try:
        probe = http.client.HTTPConnection("localhost", 11434, timeout=5)
        probe.request("GET", "/api/tags")
        resp = probe.getresponse()
        resp.read()
        probe.close()
        if resp.status != 200:
            raise ConnectionError()
    except Exception:
        print("Warning: ollama not reachable at localhost:11434 — "
              "skipping auto-cohesion (using default penalty 0.4)",
              file=sys.stderr)
        print("Hint: run 'ollama serve' to enable embedding-based "
              "cohesion scoring", file=sys.stderr)
        return {}

    # Load cache
    cache_path = os.path.join(root, '.entropy-cache.json')
    cache = {}
    if use_cache and os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                cache = json.load(f)
            if cache.get("model") != model:
                cache = {}  # Model changed, invalidate
        except (json.JSONDecodeError, IOError):
            cache = {}

    files_cache = cache.get("files", {})
    result = {}
    embedded = 0
    cache_hits = 0
    total = len(source_files)

    print(f"Auto-cohesion: embedding with {model} via ollama",
          file=sys.stderr)

    for i, fp in enumerate(source_files):
        lang = detect_language(fp)
        if lang is None:
            continue

        try:
            with open(fp, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
        except (OSError, IOError):
            continue

        rel = os.path.relpath(fp, root)
        basename = os.path.basename(fp)
        content_hash = hashlib.sha256(content.encode()).hexdigest()

        # Check cache
        cached = files_cache.get(rel)
        if cached and cached.get("hash") == content_hash:
            result[basename] = cached["cohesion"]
            cache_hits += 1
            continue

        # Split and embed
        sections = split_sections(content, lang)
        if len(sections) <= 1:
            cohesion = 1.0
            embeddings = []
        else:
            try:
                embeddings = embed_sections(sections, model)
                cohesion = compute_file_cohesion(embeddings)
            except Exception as e:
                print(f"  Warning: embedding failed for {rel}: {e}",
                      file=sys.stderr)
                continue

        result[basename] = cohesion
        embedded += 1

        # Update cache
        files_cache[rel] = {
            "hash": content_hash,
            "sections": len(sections),
            "cohesion": cohesion,
        }

        print(f"  [{i+1}/{total}] {rel} ({len(sections)} sections) "
              f"— {cohesion:.2f}", file=sys.stderr)

    # Save cache
    if use_cache:
        cache = {"model": model, "version": 1, "files": files_cache}
        try:
            with open(cache_path, 'w') as f:
                json.dump(cache, f, indent=2)
        except IOError:
            pass

    print(f"  Done: {total} files, {cache_hits} from cache, "
          f"{embedded} embedded", file=sys.stderr)

    return result


def compute_scores(files: list[FileMetrics], median_deps: float,
                   baseline_cc: float = 1.0) -> list[FileMetrics]:
    """Compute split-readiness score for each file."""
    for f in files:
        f.cc_ratio = f.avg_cc / baseline_cc if baseline_cc > 0 else 1.0
        f.size_score = (f.loc / COGNITIVE_WINDOW) * f.cc_ratio
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

    print(f"\n{'Score':>6}  {'LOC':>5}  {'CC':>4}  {'Coh':>5}  {'Con':>3}  {'Imp':>3}  Path")
    print(f"{'─'*6}  {'─'*5}  {'─'*4}  {'─'*5}  {'─'*3}  {'─'*3}  {'─'*60}")

    for f in display:
        rel = os.path.relpath(f.path, root)
        coh = f"{f.cohesion:.2f}" if f.cohesion is not None else "  n/a"
        print(f"{f.score:6.2f}  {f.loc:5d}  {f.avg_cc:4.1f}  {coh}  {f.concerns:3d}  {f.imports:3d}  {rel}")


def print_csv(files: list[FileMetrics], root: str):
    """Output as CSV."""
    print("score,loc,avg_cc,cc_ratio,cohesion,concerns,imports,path")
    for f in files:
        rel = os.path.relpath(f.path, root)
        coh = f"{f.cohesion:.4f}" if f.cohesion is not None else ""
        print(f"{f.score:.4f},{f.loc},{f.avg_cc:.2f},{f.cc_ratio:.2f},{coh},{f.concerns},{f.imports},{rel}")


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
    parser.add_argument('--cohesion', metavar='SOURCE',
                        help='Cohesion data: "auto" for local ollama embeddings, '
                             'or path to JSON file (filename → 0-1)')
    parser.add_argument('--cohesion-model', default='nomic-embed-text',
                        help='Embedding model for --cohesion auto '
                             '(default: nomic-embed-text)')
    parser.add_argument('--no-cache', action='store_true',
                        help='Skip embedding cache (re-embed all files)')
    parser.add_argument('--include-tests', action='store_true',
                        help='Include test files')
    args = parser.parse_args()

    root = os.path.abspath(args.path)
    if not os.path.isdir(root):
        print(f"Error: {root} is not a directory", file=sys.stderr)
        sys.exit(1)

    # Find source files first (needed for both analysis and auto-cohesion)
    source_files = find_source_files(root, lang_filter=args.lang,
                                     include_tests=args.include_tests)

    # Load or compute cohesion data
    if args.cohesion == "auto":
        cohesion_map = compute_auto_cohesion(
            source_files, root,
            model=args.cohesion_model,
            use_cache=not args.no_cache)
    elif args.cohesion:
        cohesion_map = load_cohesion_data(args.cohesion)
    else:
        cohesion_map = {}

    # Analyze files
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

    # Median cyclomatic complexity (baseline for damper)
    all_cc = sorted(m.avg_cc for m in metrics)
    baseline_cc = max(all_cc[len(all_cc) // 2], 1.0) if all_cc else 1.0

    # Score
    metrics = compute_scores(metrics, median_deps, baseline_cc)

    if args.above:
        metrics = [m for m in metrics if m.score >= args.threshold]

    # Output
    above = sum(1 for m in metrics if m.score >= args.threshold)

    if args.csv:
        print_csv(metrics, root)
    else:
        print(f"\n  Split-Readiness Analysis (threshold: {args.threshold})")
        print(f"  {len(metrics)} files analyzed, {above} above threshold")
        print(f"  Median imports: {median_deps}, Baseline CC: {baseline_cc:.1f}")
        print_table(metrics, root, limit=args.top, show_all=args.all)

        if above > 0:
            print(f"\n  S = {W_SIZE}*(LOC/{COGNITIVE_WINDOW})*(avg_cc/baseline) "
                  f"+ {W_COHESION}*(1-cohesion) "
                  f"+ {W_CONCERNS}*(concerns/{WORKING_MEMORY}) "
                  f"+ {W_DEPS}*(imports/median)")


if __name__ == '__main__':
    main()
