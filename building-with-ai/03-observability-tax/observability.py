#!/usr/bin/env python3
"""observability — Measure the observability tax: per-file information change
rate vs. what's captured by logs/traces/metrics, and the system-wide
competitive ratio that follows from the gap.

Computes, per file:
  - Information Change Rate (ICR): bits of state change produced per call
  - Captured Change Rate (CCR):   log + trace + metric emission density
  - Coverage = CCR / ICR  (clipped to [0, 1])
  - Gap score = ICR * (1 - coverage)

System-wide:
  - k = ICR-weighted share of files below the coverage threshold
  - Worst-case competitive ratio = 2k + 1  (Bar-Noy & Schieber 1991)

Formula (ICR, bits-per-call proxy):
  ICR = W_CC * log2(1 + branches)        # decision entropy (McCabe -> Shannon)
      + W_MUT * log2(1 + mutations)      # state delta per call (property manipulation)
      + W_IO  * log2(1 + external_calls) # boundary crossings (HTTP/gRPC/DB)

Supports: Python, JavaScript/TypeScript, Go, Rust, Java, C#, C/C++, Ruby, PHP.

Usage:
  python observability.py src/                Scan directory, show top 20 gap candidates
  python observability.py . --all             Show all files
  python observability.py . --threshold 0.2   Lower coverage threshold (default: 0.30)
  python observability.py . --csv             Output as CSV
  python observability.py . --lang go         Only scan Go files

References:
  Shannon (1948), McCabe (1976),
  Papadimitriou & Yannakakis (1991), Bar-Noy & Schieber (1991),
  Sleator & Tarjan (1985)

Article: "The Observability Tax — A Graph-Theoretic Proof"
  https://github.com/HeinrichvH/articles/blob/main/building-with-ai/03-observability-tax/03-observability-tax.md
"""

import argparse
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Optional


# --- Formula weights ---
W_CC = 0.40    # branches drive exploration cost (Dijkstra pruning argument)
W_MUT = 0.35   # mutation = state delta per call (Shannon entropy of output)
W_IO = 0.25    # I/O = cross-service span where info can vanish

DEFAULT_THRESHOLD = 0.30  # coverage below this = "hidden node"

# --- Shared branch/logic heuristics (match entropy.py) ---
BRANCH_KEYWORDS = re.compile(
    r'\b(if|elif|else\s+if|elsif|elseif|for|foreach|while|do'
    r'|switch|case|catch|except|when)\b'
)
LOGIC_OPS = re.compile(r'&&|\|\|')


# --- Language definitions (extends LANG_CONFIG from entropy.py with
#     log/trace/metric/mutation/io patterns) ---

LANG_CONFIG = {
    "py": {
        "extensions": [".py"],
        "comment_line": "#",
        "function_pattern": r"^\s*(async\s+)?def\s+\w+",
        "test_patterns": ["test_", "_test.py", "tests/", "conftest.py"],
        "exclude_files": ["__init__.py", "setup.py", "conftest.py"],
        "log_patterns": [
            r"\b(?:logging|logger|log|_log)\.(?:debug|info|warning|warn|error|critical|exception)\s*\(",
            r"\bprint\s*\(",
        ],
        "trace_patterns": [
            r"\b(?:tracer|otel)\.start(?:_as_current_span|_span)?\s*\(",
            r"\bwith\s+tracer\.",
            r"@trace\b",
        ],
        "metric_patterns": [
            r"\b(?:counter|histogram|gauge|meter)\.(?:add|record|inc|observe|set)\s*\(",
            r"\bmetrics\.",
            r"\.inc\(\)|\.dec\(\)",
        ],
        "mutation_patterns": [
            r"^\s*(?:self\.)?\w+(?:\.\w+)*\s*(?:\+=|-=|\*=|/=|=)\s*[^=]",
            r"\.(?:append|extend|insert|pop|remove|update|setdefault|clear)\s*\(",
        ],
        "io_patterns": [
            r"\b(?:requests|httpx|aiohttp|urllib)\.",
            r"\.(?:get|post|put|delete|patch)\s*\(",
            r"\b(?:session|client|conn|cursor|db|engine)\.(?:execute|query|fetch|commit)",
            r"\bopen\s*\(",
            r"\bsubprocess\.",
        ],
    },
    "js": {
        "extensions": [".js", ".jsx", ".ts", ".tsx", ".mjs"],
        "comment_line": "//",
        "function_pattern": r"^\s*(?:export\s+)?(?:async\s+)?function\b",
        "test_patterns": [".test.", ".spec.", "__tests__/", "test/"],
        "exclude_files": ["index.js", "index.ts"],
        "log_patterns": [
            r"\bconsole\.(?:log|info|warn|error|debug)\s*\(",
            r"\b(?:logger|log)\.(?:debug|info|warn|error|trace)\s*\(",
        ],
        "trace_patterns": [
            r"\btracer\.startSpan\s*\(",
            r"\bstartActiveSpan\s*\(",
            r"@Trace\b",
            r"\bopentelemetry\.",
        ],
        "metric_patterns": [
            r"\b(?:counter|histogram|gauge|meter)\.(?:add|record|inc|observe|set)\s*\(",
            r"\bmetrics\.",
        ],
        "mutation_patterns": [
            r"^\s*(?:this\.)?\w+(?:\.\w+)*\s*(?:\+=|-=|\*=|/=|=)\s*[^=]",
            r"\.(?:push|pop|shift|unshift|splice|set|delete)\s*\(",
        ],
        "io_patterns": [
            r"\bfetch\s*\(",
            r"\baxios\.",
            r"\.(?:get|post|put|delete|patch)\s*\(",
            r"\bfs\.(?:read|write|open)",
            r"\bawait\s+",
        ],
    },
    "go": {
        "extensions": [".go"],
        "comment_line": "//",
        "function_pattern": r"^func\s",
        "test_patterns": ["_test.go"],
        "exclude_files": [],
        "log_patterns": [
            # Any receiver invoking a log-verb method — covers `log.Info`,
            # `logger.Info`, `c.log.Info`, `l.Debugw`, `lgr.Errorf`, etc.
            # The suffix matches Go logger idioms: f / w / Context / ln.
            r"\b\w+\.(?:Debug|Info|Warn|Error|Fatal|Trace|Print)"
            r"(?:Context|f|w|ln)?\s*\(",
            # Package-qualified loggers (covers slog.Info, zap.S().Info, etc.)
            r"\b(?:zap|slog|logrus|klog|glog|log)\.(?:S|L|Sugar|With)\s*\(",
            # Structured-logging builder chains
            r"\.With(?:Field|Error|Context|Attrs|Values)s?\s*\(",
        ],
        "trace_patterns": [
            r"\btracer\.Start(?:Span)?\s*\(",
            r"\botel\.(?:Tracer|GetTracerProvider|SetTracerProvider|Meter)",
            r"\btrace\.(?:SpanFromContext|ContextWithSpan|StartSpan|"
            r"WithAttributes|NewSpan)",
            # Span method usage — anyone calling span.End, span.RecordError,
            # span.SetAttributes, span.AddEvent is participating in tracing.
            r"\b(?:span|sp)\.(?:End|SetAttributes|RecordError|AddEvent|"
            r"SetStatus|IsRecording)\s*\(",
            r"\bopentracing\.",
            r"\botelhttp\.",
            r"\botelgrpc\.",
        ],
        "metric_patterns": [
            # Prometheus constructors and registrations
            r"\bprometheus\.(?:NewCounter|NewGauge|NewHistogram|NewSummary|"
            r"MustRegister|CounterOpts|GaugeOpts|HistogramOpts|"
            r"NewCounterVec|NewGaugeVec|NewHistogramVec|NewSummaryVec)",
            r"\b(?:MustNewConstMetric|NewDesc)\s*\(",
            # The canonical Prometheus update chain: .WithLabelValues(…).Inc()
            r"\.WithLabelValues\s*\([^)]*\)\s*\.(?:Inc|Dec|Add|Observe|Set)",
            # OTel metrics instruments
            r"\.(?:Int64Counter|Float64Counter|Int64Histogram|"
            r"Float64Histogram|Int64UpDownCounter|Int64Gauge|Float64Gauge)"
            r"\s*\(",
            r"\b(?:meter|otel\.Meter)\b",
            # Struct-field metric handles (common pattern in Go services)
            r"\.(?:counter|gauge|histogram|summary|metric)s?"
            r"\.(?:Inc|Add|Observe|Set)\s*\(",
        ],
        "mutation_patterns": [
            r"^\s*\w+(?:\.\w+)*\s*(?:\+=|-=|\*=|/=|=)\s*[^=]",
            r"\bappend\s*\(",
        ],
        "io_patterns": [
            r"\bhttp\.(?:Get|Post|NewRequest|Do)",
            r"\b(?:client|conn|db|tx)\.(?:Query|Exec|Do|Send)",
            r"\bos\.(?:Open|Create|Read|Write)",
        ],
    },
    "rs": {
        "extensions": [".rs"],
        "comment_line": "//",
        "function_pattern": r"^\s*(?:pub\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s",
        "test_patterns": ["tests/", "#[cfg(test)]"],
        "exclude_files": ["mod.rs", "lib.rs", "main.rs"],
        "log_patterns": [
            r"\b(?:info|warn|error|debug|trace)!\s*\(",
            r"\bprintln!\s*\(",
            r"\beprintln!\s*\(",
        ],
        "trace_patterns": [
            r"\b#\[tracing::instrument",
            r"\btracing::",
            r"\bspan!\s*\(",
        ],
        "metric_patterns": [
            r"\b(?:counter|histogram|gauge)!\s*\(",
            r"\bmetrics::",
        ],
        "mutation_patterns": [
            r"^\s*(?:let\s+mut\s+)?\w+(?:\.\w+)*\s*(?:\+=|-=|\*=|/=|=)\s*[^=]",
            r"\.(?:push|pop|insert|remove|clear)\s*\(",
        ],
        "io_patterns": [
            r"\breqwest::",
            r"\bawait\b",
            r"\bstd::fs::",
            r"\bsqlx::",
        ],
    },
    "java": {
        "extensions": [".java", ".kt"],
        "comment_line": "//",
        "function_pattern": r"^\s*(?:public|private|protected)[\w\s<>]*\s+\w+\s*\(",
        "test_patterns": ["Test.java", "Tests.java", "test/", "Test.kt"],
        "exclude_files": ["package-info.java"],
        "log_patterns": [
            r"\b(?:log|logger|LOG|LOGGER)\.(?:debug|info|warn|error|trace)\s*\(",
            r"\bSystem\.(?:out|err)\.",
        ],
        "trace_patterns": [
            r"\btracer\.spanBuilder\s*\(",
            r"@WithSpan\b",
            r"@Trace\b",
        ],
        "metric_patterns": [
            r"\b(?:counter|histogram|gauge|meter)\.(?:add|record|inc|observe|set)\s*\(",
            r"\bMetrics\.",
            r"@Timed\b",
        ],
        "mutation_patterns": [
            r"^\s*(?:this\.)?\w+(?:\.\w+)*\s*(?:\+=|-=|\*=|/=|=)\s*[^=]",
            r"\.(?:add|put|remove|clear|set)\s*\(",
        ],
        "io_patterns": [
            r"\bHttpClient\.",
            r"\brestTemplate\.",
            r"\b(?:connection|statement|entityManager|repository)\.",
        ],
    },
    "cs": {
        "extensions": [".cs"],
        "comment_line": "//",
        "function_pattern": r"^\s*(?:public|private|protected|internal)[\w\s<>]*\s+\w+\s*\(",
        "test_patterns": ["Tests.cs", ".UnitTests", ".IntegrationTests"],
        "exclude_files": ["GlobalUsings.cs", "Program.cs", "AssemblyInfo.cs"],
        "log_patterns": [
            # Any receiver invoking a log-verb method — covers Microsoft
            # ILogger (LogInformation, LogWarning…), Serilog (ILogger with
            # Information, Warning…), and wrapped receivers like
            # `_logger.LogInformation`, `logger.Information`,
            # `Log.ForContext(...).Error`.
            r"\b\w+\.(?:Log(?:Trace|Debug|Information|Warning|Error|Critical)|"
            r"Trace|Debug|Information|Warn|Warning|Error|Critical|Fatal|Verbose)"
            r"(?:Async)?\s*\(",
            # Serilog / ForContext / enrichers
            r"\bLog\.(?:ForContext|Logger|Information|Warning|Error|Debug)",
            r"\.ForContext(?:<\w+>)?\s*\(",
            # Static Console (weak signal but used for diagnostics)
            r"\bConsole\.(?:WriteLine|Error\.WriteLine)\s*\(",
        ],
        "trace_patterns": [
            # OpenTelemetry / System.Diagnostics.Activity
            r"\bActivity\.(?:Start|Current|StartActivity)\b",
            r"\b_?[Aa]ctivitySource\.(?:Start|CreateActivity)",
            r"\b(?:activity|_activity)\?\.(?:SetTag|AddEvent|SetStatus|"
            r"RecordException|Dispose)\s*\(",
            r"\bactivity\.Set(?:Tag|Status)\s*\(",
            r"\b\[Activity(?:Source)?\(",
        ],
        "metric_patterns": [
            # System.Diagnostics.Metrics (OTel-friendly)
            r"\.(?:CreateCounter|CreateHistogram|CreateUpDownCounter|"
            r"CreateObservableGauge|CreateObservableCounter)\s*<",
            r"\b(?:counter|histogram|gauge)\.(?:Add|Record|Observe|Set)\s*\(",
            # Prometheus-net
            r"\bMetrics\.(?:CreateCounter|CreateGauge|CreateHistogram|"
            r"CreateSummary|DefaultRegistry)",
            r"\.(?:Inc|Dec|Observe)\s*\(\s*(?:\d|_|labels)",
            # Meter constructors
            r"\bnew\s+Meter\s*\(",
        ],
        "mutation_patterns": [
            r"^\s*(?:this\.)?\w+(?:\.\w+)*\s*(?:\+=|-=|\*=|/=|=)\s*[^=]",
            r"\.(?:Add|Remove|Clear|Set)\w*\s*\(",
        ],
        "io_patterns": [
            r"\bHttpClient\.",
            r"\bawait\b",
            r"\b(?:_db|_context|dbContext)\.",
            r"\bFile\.(?:Read|Write|Open)",
        ],
    },
    "c": {
        "extensions": [".c", ".cpp", ".cc", ".h", ".hpp"],
        "comment_line": "//",
        "function_pattern": r"^[a-zA-Z_][\w\s*]+\w+\s*\([^;]*$",
        "test_patterns": ["test_", "_test.", "tests/"],
        "exclude_files": [],
        "log_patterns": [
            r"\b(?:printf|fprintf|perror)\s*\(",
            r"\b(?:LOG|log)_(?:DEBUG|INFO|WARN|ERROR)\s*\(",
            r"\bstd::(?:cout|cerr)\s*<<",
        ],
        "trace_patterns": [
            r"\btrace_",
            r"\bOTEL_",
        ],
        "metric_patterns": [
            r"\bmetric_",
            r"\bcounter_",
        ],
        "mutation_patterns": [
            r"^\s*\w+(?:\.\w+|->\w+|\[\w+\])*\s*(?:\+=|-=|\*=|/=|=)\s*[^=]",
        ],
        "io_patterns": [
            r"\b(?:fopen|fread|fwrite|read|write|send|recv|connect|accept)\s*\(",
        ],
    },
    "rb": {
        "extensions": [".rb"],
        "comment_line": "#",
        "function_pattern": r"^\s*def\s+\w+",
        "test_patterns": ["_test.rb", "_spec.rb", "test/", "spec/"],
        "exclude_files": [],
        "log_patterns": [
            r"\b(?:logger|Rails\.logger|log)\.(?:debug|info|warn|error|fatal)\s*",
            r"\b(?:puts|print|pp)\s+",
        ],
        "trace_patterns": [
            r"\bTracing\.",
            r"\bTracer\.",
        ],
        "metric_patterns": [
            r"\bStatsD\.",
            r"\bMetrics\.",
            r"\.(?:increment|gauge|histogram|timing)\s*",
        ],
        "mutation_patterns": [
            r"^\s*@?\w+(?:\.\w+)*\s*(?:\+=|-=|\*=|/=|=)\s*[^=]",
            r"\.(?:push|pop|shift|unshift|<<|delete|clear)",
        ],
        "io_patterns": [
            r"\b(?:Net::HTTP|Faraday|HTTParty|RestClient)\.",
            r"\b(?:ActiveRecord|Sequel)",
            r"\bFile\.(?:open|read|write)",
        ],
    },
    "php": {
        "extensions": [".php"],
        "comment_line": "//",
        "function_pattern": r"^\s*(?:public\s+|private\s+|protected\s+|static\s+)*function\s+\w+",
        "test_patterns": ["Test.php", "Tests.php", "tests/"],
        "exclude_files": [],
        "log_patterns": [
            r"\b(?:\$logger|\$log|Log)::(?:debug|info|warning|error|critical)\s*\(",
            r"\b(?:error_log|echo|print|var_dump)\s*\(",
        ],
        "trace_patterns": [
            r"\bTracer::",
            r"\bSpan::",
        ],
        "metric_patterns": [
            r"\bMetrics::",
            r"\bStatsD::",
        ],
        "mutation_patterns": [
            r"^\s*\$\w+(?:->\w+)*\s*(?:\+=|-=|\*=|/=|=)\s*[^=]",
            r"->(?:push|add|remove|set|delete)\s*\(",
        ],
        "io_patterns": [
            r"\bcurl_",
            r"\b(?:file_get_contents|fopen|fwrite)\s*\(",
            r"\bPDO::",
        ],
    },
}


@dataclass
class ObsMetrics:
    path: str
    loc: int = 0
    funcs: int = 0
    branches: int = 0
    mutations: int = 0
    io_calls: int = 0
    logs: int = 0
    spans: int = 0
    metrics_: int = 0
    icr: float = 0.0
    ccr: float = 0.0
    coverage: float = 0.0          # own coverage (CCR / ICR)
    gap: float = 0.0
    covered_by: Optional[str] = None  # name of validated pipeline, if any
    effective_coverage: float = 0.0   # max(coverage, 1.0 if covered_by else coverage)


@dataclass
class Pipeline:
    """Declaration that `sources` instrument every file matching `covers`.

    Validated at load time: if the sources themselves don't meet a coverage
    bar (CCR/ICR across source files ≥ pipeline_threshold), the pipeline is
    rejected and files matching `covers` don't get credit. This prevents
    declaring fake pipelines.
    """
    name: str
    sources: list[str] = field(default_factory=list)
    covers: list[str] = field(default_factory=list)
    # Populated post-scan
    source_coverage: float = 0.0
    source_icr: float = 0.0
    source_ccr: float = 0.0
    validated: bool = False
    rejection_reason: str = ""


@dataclass
class Config:
    threshold: float = 0.30
    # Minimum aggregate CCR/ICR in pipeline source files. Same semantic as
    # the per-file threshold: if the pipeline doesn't instrument its own
    # work at this level, don't credit downstream files either.
    pipeline_threshold: float = 0.30
    pipelines: list[Pipeline] = field(default_factory=list)


def load_config(root: str, explicit_path: Optional[str] = None) -> Config:
    """Load pipeline config from .observability.json at the scan root.

    The file format is JSON (stdlib, no extra dependency). An explicit path
    overrides auto-discovery.
    """
    candidates = []
    if explicit_path:
        candidates.append(explicit_path)
    else:
        for name in ('.observability.json', 'observability.json'):
            candidates.append(os.path.join(root, name))

    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"Warning: could not read {path}: {e}", file=sys.stderr)
            continue

        pipelines = [
            Pipeline(
                name=p.get("name", f"pipeline-{i}"),
                sources=list(p.get("sources", [])),
                covers=list(p.get("covers", [])),
            )
            for i, p in enumerate(data.get("pipelines", []))
        ]
        return Config(
            threshold=float(data.get("threshold", 0.30)),
            pipeline_threshold=float(data.get("pipeline_threshold", 1.0)),
            pipelines=pipelines,
        )

    return Config()


def _glob_to_regex(pattern: str) -> re.Pattern:
    """Convert a glob with `**` support into a regex. Path separator is `/`.

    Semantics:
      - `**` matches any sequence of path segments (including empty)
      - `*`  matches any characters except `/`
      - `?`  matches any single non-`/` character
    """
    pat = pattern.replace(os.sep, '/')
    out = []
    i = 0
    while i < len(pat):
        c = pat[i]
        if c == '*':
            if i + 1 < len(pat) and pat[i + 1] == '*':
                # `**/` → zero or more path segments; `**` at end → rest of path
                if i + 2 < len(pat) and pat[i + 2] == '/':
                    out.append(r'(?:.*/)?')
                    i += 3
                    continue
                out.append(r'.*')
                i += 2
                continue
            out.append(r'[^/]*')
            i += 1
        elif c == '?':
            out.append(r'[^/]')
            i += 1
        elif c in '.+^$(){}|\\':
            out.append(re.escape(c))
            i += 1
        else:
            out.append(c)
            i += 1
    return re.compile('^' + ''.join(out) + '$')


_GLOB_CACHE: dict[str, re.Pattern] = {}


def _match_any(rel_path: str, patterns: list[str]) -> bool:
    norm = rel_path.replace(os.sep, '/')
    for pat in patterns:
        rx = _GLOB_CACHE.get(pat)
        if rx is None:
            rx = _glob_to_regex(pat)
            _GLOB_CACHE[pat] = rx
        if rx.match(norm):
            return True
    return False


def validate_pipelines(metrics: list[ObsMetrics], root: str,
                       config: Config) -> None:
    """Compute each pipeline's own CCR density and decide if it's validated."""
    by_rel = {os.path.relpath(m.path, root).replace(os.sep, '/'): m
              for m in metrics}

    for pipe in config.pipelines:
        sources = [by_rel[s.replace(os.sep, '/')] for s in pipe.sources
                   if s.replace(os.sep, '/') in by_rel]
        if not sources:
            pipe.rejection_reason = "no source files found in scan"
            continue
        pipe.source_icr = sum(m.icr for m in sources)
        pipe.source_ccr = sum(m.ccr for m in sources)
        pipe.source_coverage = (
            pipe.source_ccr / pipe.source_icr if pipe.source_icr > 0 else 1.0
        )
        if pipe.source_coverage >= config.pipeline_threshold:
            pipe.validated = True
        else:
            pipe.rejection_reason = (
                f"source coverage {pipe.source_coverage:.2f} "
                f"< threshold {config.pipeline_threshold}"
            )


def apply_pipeline_coverage(metrics: list[ObsMetrics], root: str,
                            config: Config) -> None:
    """Mark each file with the first validated pipeline that covers it."""
    for m in metrics:
        rel = os.path.relpath(m.path, root)
        for pipe in config.pipelines:
            if not pipe.validated:
                continue
            if _match_any(rel, pipe.covers):
                m.covered_by = pipe.name
                break


def detect_language(filepath: str) -> Optional[str]:
    ext = os.path.splitext(filepath)[1].lower()
    for lang, config in LANG_CONFIG.items():
        if ext in config["extensions"]:
            return lang
    return None


def count_patterns(content: str, patterns: list[str], comment_char: str) -> int:
    """Count pattern matches, skipping commented-out lines."""
    total = 0
    compiled = [re.compile(p) for p in patterns]
    for line in content.split('\n'):
        stripped = line.strip()
        if stripped.startswith(comment_char):
            continue
        for rx in compiled:
            total += len(rx.findall(line))
    return total


def analyze_file(filepath: str) -> Optional[ObsMetrics]:
    lang = detect_language(filepath)
    if lang is None:
        return None
    config = LANG_CONFIG[lang]

    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
    except (OSError, IOError):
        return None

    lines = content.split('\n')
    loc = len([l for l in lines if l.strip()])
    if loc < 20:
        return None

    comment_char = config["comment_line"]
    func_pattern = re.compile(config["function_pattern"])
    funcs = sum(1 for l in lines if func_pattern.match(l))

    branches = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(comment_char):
            continue
        branches += len(BRANCH_KEYWORDS.findall(stripped))
        branches += len(LOGIC_OPS.findall(stripped))

    mutations = count_patterns(content, config["mutation_patterns"], comment_char)
    io_calls = count_patterns(content, config["io_patterns"], comment_char)
    logs = count_patterns(content, config["log_patterns"], comment_char)
    spans = count_patterns(content, config["trace_patterns"], comment_char)
    metrics_ = count_patterns(content, config["metric_patterns"], comment_char)

    return ObsMetrics(
        path=filepath, loc=loc, funcs=funcs,
        branches=branches, mutations=mutations, io_calls=io_calls,
        logs=logs, spans=spans, metrics_=metrics_,
    )


def is_test_file(filepath: str, lang: str) -> bool:
    config = LANG_CONFIG[lang]
    lower = filepath.lower()
    return any(p in lower for p in config["test_patterns"])


def is_excluded(filepath: str, lang: str) -> bool:
    config = LANG_CONFIG[lang]
    return os.path.basename(filepath) in config["exclude_files"]


def find_source_files(root: str, lang_filter: Optional[str] = None,
                      include_tests: bool = False) -> list[str]:
    # Build + tool output
    skip_dirs = {'bin', 'obj', 'node_modules', '.git', 'vendor', 'dist',
                 'build', '__pycache__', '.tox', '.venv', 'venv',
                 'target', '.next', '.nuxt', 'coverage',
                 # Frontend bundles shipped alongside backend services —
                 # the tool is aimed at service-side observability, and
                 # browser instrumentation uses a different model (RUM,
                 # error boundaries) that this scanner doesn't reason about.
                 'public', 'ui', 'frontend', 'client', 'webui'}
    # Case-insensitive match: "Public" should skip as well as "public".
    skip_dirs_lower = {d.lower() for d in skip_dirs}
    # Files that mark a directory as a backend project — if a package.json
    # lives *alongside* one of these, it's Tailwind/ESLint tooling, not a
    # JS/TS subproject, and we should keep scanning.
    backend_markers = {'go.mod', 'pom.xml', 'build.gradle', 'build.gradle.kts',
                       'pyproject.toml', 'requirements.txt', 'setup.py',
                       'Cargo.toml', 'composer.json', 'Gemfile', 'mix.exs'}
    backend_marker_suffixes = ('.csproj', '.sln', '.slnx', '.fsproj',
                               '.vbproj', '.vcxproj')
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip nested JS/TS subprojects: package.json with NO backend marker
        # alongside it. A mixed-mode project (e.g. .csproj + package.json
        # for Tailwind) stays in scope.
        if dirpath != root and 'package.json' in filenames:
            has_backend_marker = (
                any(f in backend_markers for f in filenames)
                or any(f.endswith(backend_marker_suffixes) for f in filenames)
            )
            if not has_backend_marker:
                dirnames[:] = []
                continue
        dirnames[:] = [d for d in dirnames if d.lower() not in skip_dirs_lower]
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
            if f.endswith(('.g.cs', '.generated.cs', '.gen.go', '.pb.go',
                           '.pb.gw.go', '_grpc.pb.go',
                           '.min.js', '.d.ts', '.map',
                           '.pb.ts', '.gen.ts', '.generated.ts',
                           '_pb2.py', '_pb2_grpc.py')):
                continue
            files.append(full)
    return files


def compute_scores(metrics: list[ObsMetrics]) -> list[ObsMetrics]:
    """Fill icr, ccr, coverage, gap. Returns list sorted by gap desc.

    `effective_coverage` is set later, after pipeline validation, so that
    files covered by a validated pipeline are credited with full coverage.
    """
    for m in metrics:
        per_func = max(m.funcs, 1)
        br = m.branches / per_func
        mu = m.mutations / per_func
        io = m.io_calls / per_func

        m.icr = (
            W_CC * math.log2(1 + br)
            + W_MUT * math.log2(1 + mu)
            + W_IO * math.log2(1 + io)
        )
        m.ccr = (m.logs + m.spans + m.metrics_) / per_func

        if m.icr <= 0:
            m.coverage = 1.0
        else:
            m.coverage = min(m.ccr / m.icr, 1.0)

        m.effective_coverage = m.coverage
        m.gap = m.icr * (1.0 - m.coverage)

    return sorted(metrics, key=lambda x: x.gap, reverse=True)


def finalize_effective_coverage(metrics: list[ObsMetrics]) -> None:
    """After pipeline attribution, lift effective_coverage to 1.0 for covered files."""
    for m in metrics:
        if m.covered_by:
            m.effective_coverage = 1.0
            m.gap = 0.0  # pipeline covers it; not a hidden node


def system_competitive_ratio(metrics: list[ObsMetrics], root: str,
                             threshold: float
                             ) -> tuple[int, int, int, int, float]:
    """Return (hidden_files, pipeline_covered_files, total_dirs,
    hidden_dirs, competitive_ratio).

    k is counted over top-level directories (a proxy for service
    boundaries), using `effective_coverage` — pipeline-covered files count
    as instrumented. A top-level directory is "hidden" when ≥50% of its
    ICR lives in files whose *effective* coverage is below threshold.
    """
    def top_dir(abs_path: str) -> str:
        rel = os.path.relpath(abs_path, root)
        parts = rel.split(os.sep)
        return parts[0] if parts else rel

    by_dir: dict[str, tuple[float, float]] = {}
    for m in metrics:
        d = top_dir(m.path)
        total, hidden = by_dir.get(d, (0.0, 0.0))
        total += m.icr
        if m.effective_coverage < threshold:
            hidden += m.icr
        by_dir[d] = (total, hidden)

    hidden_dirs = sum(1 for t, h in by_dir.values() if t > 0 and h / t >= 0.5)
    hidden_files = sum(1 for m in metrics
                       if m.effective_coverage < threshold and not m.covered_by)
    pipeline_covered = sum(1 for m in metrics if m.covered_by)
    cr = 2.0 * hidden_dirs + 1.0
    return hidden_files, pipeline_covered, len(by_dir), hidden_dirs, cr


def print_table(files: list[ObsMetrics], root: str, limit: int = 20,
                show_all: bool = False):
    display = files if show_all else files[:limit]
    print(f"\n{'Gap':>5}  {'Cov':>5}  {'ICR':>5}  {'CCR':>5}  "
          f"{'LOC':>5}  {'Br':>4}  {'Mut':>4}  {'IO':>3}  "
          f"{'Log':>3}  {'Spn':>3}  {'Met':>3}  Path")
    print(f"{'─'*5}  {'─'*5}  {'─'*5}  {'─'*5}  {'─'*5}  {'─'*4}  "
          f"{'─'*4}  {'─'*3}  {'─'*3}  {'─'*3}  {'─'*3}  {'─'*60}")
    for f in display:
        rel = os.path.relpath(f.path, root)
        print(f"{f.gap:5.2f}  {f.coverage:5.2f}  {f.icr:5.2f}  "
              f"{f.ccr:5.2f}  {f.loc:5d}  {f.branches:4d}  "
              f"{f.mutations:4d}  {f.io_calls:3d}  "
              f"{f.logs:3d}  {f.spans:3d}  {f.metrics_:3d}  {rel}")


def print_csv(files: list[ObsMetrics], root: str):
    print("gap,coverage,effective_coverage,covered_by,icr,ccr,loc,funcs,"
          "branches,mutations,io_calls,logs,spans,metrics,path")
    for f in files:
        rel = os.path.relpath(f.path, root)
        print(f"{f.gap:.4f},{f.coverage:.4f},{f.effective_coverage:.4f},"
              f"{f.covered_by or ''},"
              f"{f.icr:.4f},{f.ccr:.4f},"
              f"{f.loc},{f.funcs},{f.branches},{f.mutations},"
              f"{f.io_calls},{f.logs},{f.spans},{f.metrics_},{rel}")


def main():
    parser = argparse.ArgumentParser(
        description='Measure observability tax: ICR vs CCR + competitive ratio',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument('path', nargs='?', default='.',
                        help='Directory to scan (default: current)')
    parser.add_argument('--all', action='store_true', help='Show all files')
    parser.add_argument('--top', type=int, default=20, help='Top N gap candidates')
    parser.add_argument('--threshold', type=float, default=DEFAULT_THRESHOLD,
                        help=f'Coverage threshold τ (default: {DEFAULT_THRESHOLD})')
    parser.add_argument('--csv', action='store_true', help='Output as CSV')
    parser.add_argument('--above', action='store_true',
                        help='Only files below coverage threshold')
    parser.add_argument('--lang', choices=list(LANG_CONFIG.keys()),
                        help='Only scan this language')
    parser.add_argument('--include-tests', action='store_true',
                        help='Include test files')
    parser.add_argument('--config', metavar='PATH',
                        help='Pipeline config (default: auto-detect '
                             '.observability.json at scan root)')
    parser.add_argument('--explain', metavar='FILE',
                        help='Explain classification of a specific file '
                             '(relative to scan root)')
    args = parser.parse_args()

    root = os.path.abspath(args.path)
    if not os.path.isdir(root):
        print(f"Error: {root} is not a directory", file=sys.stderr)
        sys.exit(1)

    config = load_config(root, explicit_path=args.config)
    if args.threshold != DEFAULT_THRESHOLD:
        config.threshold = args.threshold

    source_files = find_source_files(root, lang_filter=args.lang,
                                     include_tests=args.include_tests)

    metrics = []
    for fp in source_files:
        m = analyze_file(fp)
        if m is not None:
            metrics.append(m)

    if not metrics:
        print("No source files found.", file=sys.stderr)
        sys.exit(1)

    metrics = compute_scores(metrics)
    validate_pipelines(metrics, root, config)
    apply_pipeline_coverage(metrics, root, config)
    finalize_effective_coverage(metrics)
    metrics = sorted(metrics, key=lambda x: x.gap, reverse=True)

    if args.explain:
        target = args.explain.replace(os.sep, '/')
        match = next((m for m in metrics
                      if os.path.relpath(m.path, root).replace(os.sep, '/')
                      == target), None)
        if match is None:
            print(f"File not found in scan: {args.explain}", file=sys.stderr)
            sys.exit(2)
        print(f"\nExplain: {args.explain}")
        print(f"  funcs={match.funcs}  branches={match.branches}  "
              f"mutations={match.mutations}  io={match.io_calls}")
        print(f"  logs={match.logs}  spans={match.spans}  "
              f"metrics={match.metrics_}")
        print(f"  ICR={match.icr:.2f}  CCR={match.ccr:.2f}  "
              f"own_coverage={match.coverage:.2f}")
        if match.covered_by:
            print(f"  Covered by pipeline: {match.covered_by} → effective_coverage=1.00")
        else:
            print(f"  Not covered by any pipeline → effective_coverage={match.effective_coverage:.2f}")
        return

    if args.above:
        metrics = [m for m in metrics
                   if m.effective_coverage < args.threshold and not m.covered_by]

    (hidden_files, pipeline_covered, total_dirs,
     hidden_dirs, cr) = system_competitive_ratio(
        metrics, root, args.threshold)

    if args.csv:
        print_csv(metrics, root)
        return

    print(f"\n  Observability-Tax Analysis (coverage threshold: {args.threshold})")
    print(f"  {len(metrics)} files analyzed")
    print_table(metrics, root, limit=args.top, show_all=args.all)

    print(f"\n  ICR = {W_CC}*log2(1+branches) + {W_MUT}*log2(1+mutations) "
          f"+ {W_IO}*log2(1+io_calls)  (per-function averages)")
    print(f"  CCR = (logs + spans + metrics) / functions")
    print(f"  Coverage = min(CCR/ICR, 1); effective_coverage upgrades to 1.0 "
          f"if a validated pipeline covers the file")

    if config.pipelines:
        print(f"\n  Pipelines declared: {len(config.pipelines)}")
        for p in config.pipelines:
            if p.validated:
                print(f"    ✓ {p.name}: source coverage {p.source_coverage:.2f} "
                      f"across {len(p.sources)} source(s)")
            else:
                print(f"    ✗ {p.name}: rejected — {p.rejection_reason}")

    direct = len(metrics) - pipeline_covered - hidden_files
    print(f"\n  Directly instrumented (own coverage ≥ {args.threshold}): "
          f"{direct}")
    print(f"  Pipeline-covered:                                {pipeline_covered}")
    print(f"  Dark (no direct + no pipeline):                  {hidden_files}")
    print(f"  Hidden top-level dirs (k, proxy for services):   "
          f"{hidden_dirs} of {total_dirs}")
    cr_str = f"{cr:,.1f}x" if cr < 1e9 else f"{cr:.2e}x"
    print(f"  Worst-case competitive ratio:     2·{hidden_dirs} + 1 = {cr_str}")


if __name__ == '__main__':
    main()
