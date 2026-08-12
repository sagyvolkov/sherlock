#!/usr/bin/env bash
# shellcheck disable=SC2034
# =============================================================================
# sherlock.sh — Sherlock v2 helper script
#
# A thin wrapper around kubectl for managing Sherlock benchmark Suite CRs.
# No installation required — just kubectl and this script.
#
# Usage:
#   sherlock.sh <command> [options]
#
# Commands:
#   run      <file>              Apply a Suite CR from a YAML file
#   status   <suite> [-k kind]  Show run matrix progress table
#   results  <suite> [-k kind]  Show parsed metrics table
#   export   <suite> [-k kind] [-f json|csv]  Export results to stdout
#   list     [-n namespace]     List all Suite CRs across all kinds
#   logs     <suite> <run>      Stream logs from a benchmark job pod
#   delete   <suite> [-k kind] [--delete-pvcs]  Delete a Suite CR
#
# Namespace:
#   Defaults to 'sherlock'. Override with -n flag or SHERLOCK_NAMESPACE env var.
#
# Examples:
#   sherlock.sh run ./examples/hammerdb-example.yaml
#   sherlock.sh status mssql-vu-sweep
#   sherlock.sh results mssql-vu-sweep --format csv > results.csv
#   sherlock.sh list
#   sherlock.sh logs mssql-vu-sweep suite-vu16-wh100-rm2-tm5
#   SHERLOCK_NAMESPACE=team-a sherlock.sh status mssql-vu-sweep
# =============================================================================

set -euo pipefail

# ── Constants ─────────────────────────────────────────────────────────────────

VERSION="v2.0.0-alpha1"
DEFAULT_NAMESPACE="sherlock"

# All Sherlock Suite CRD plural names and their short display labels
declare -A SUITE_KINDS=(
    [sherlockpgbenchsuites]="pgbench"
    [sherlockhammerdbsuites]="hammerdb"
    [sherlocksysbenchsuites]="sysbench"
    [sherlockycsbbenchsuites]="ycsb"
    [sherlockfiosuites]="fio"
)

# Short name → plural CRD name (for -k flag)
declare -A KIND_SHORT_TO_PLURAL=(
    [pgbench]="sherlockpgbenchsuites"
    [hammerdb]="sherlockhammerdbsuites"
    [sysbench]="sherlocksysbenchsuites"
    [ycsb]="sherlockycsbbenchsuites"
    [fio]="sherlockfiosuites"
)

# ── Color support ─────────────────────────────────────────────────────────────
# Colors are emitted only when stdout is a TTY.
# When piped or redirected, all output is plain text.

if [ -t 1 ]; then
    C_RESET="\033[0m"
    C_BOLD="\033[1m"
    C_RED="\033[31m"
    C_GREEN="\033[32m"
    C_YELLOW="\033[33m"
    C_BLUE="\033[34m"
    C_CYAN="\033[36m"
    C_GRAY="\033[90m"
else
    C_RESET="" C_BOLD="" C_RED="" C_GREEN="" C_YELLOW=""
    C_BLUE="" C_CYAN="" C_GRAY=""
fi

# ── Helpers ───────────────────────────────────────────────────────────────────

info()    { echo -e "${C_BLUE}[sherlock]${C_RESET} $*"; }
success() { echo -e "${C_GREEN}[sherlock]${C_RESET} $*"; }
warn()    { echo -e "${C_YELLOW}[sherlock]${C_RESET} $*" >&2; }
error()   { echo -e "${C_RED}[sherlock]${C_RESET} $*" >&2; exit 1; }

# Print a horizontal rule
rule() { printf "${C_GRAY}%s${C_RESET}\n" "$(printf '─%.0s' {1..72})"; }

# Phase → colored string
phase_color() {
    local phase="$1"
    case "$phase" in
        Completed)  echo -e "${C_GREEN}${phase}${C_RESET}" ;;
        Running)    echo -e "${C_CYAN}${phase}${C_RESET}" ;;
        Failed)     echo -e "${C_RED}${phase}${C_RESET}" ;;
        Degraded)   echo -e "${C_YELLOW}${phase}${C_RESET}" ;;
        Sleeping)   echo -e "${C_GRAY}${phase}${C_RESET}" ;;
        *)          echo -e "${phase}" ;;
    esac
}

# Check kubectl is available
require_kubectl() {
    if ! command -v kubectl &>/dev/null; then
        error "kubectl not found. Please install kubectl and configure it for your cluster."
    fi
}

# Resolve namespace: -n flag > SHERLOCK_NAMESPACE env > default
resolve_namespace() {
    echo "${SHERLOCK_NAMESPACE:-${DEFAULT_NAMESPACE}}"
}

# Auto-detect the CRD kind for a named Suite by searching all Suite CRDs
detect_kind() {
    local suite_name="$1"
    local namespace="$2"
    local found_kind=""

    for plural in "${!SUITE_KINDS[@]}"; do
        if kubectl get "$plural" "$suite_name" -n "$namespace" \
                --ignore-not-found -o name &>/dev/null 2>&1; then
            found_kind="$plural"
            break
        fi
    done

    if [[ -z "$found_kind" ]]; then
        error "Suite '${suite_name}' not found in namespace '${namespace}'.\n" \
              "Run 'sherlock.sh list' to see available suites."
    fi
    echo "$found_kind"
}

# Resolve kind: -k short name > auto-detect
resolve_kind() {
    local suite_name="$1"
    local namespace="$2"
    local kind_flag="${3:-}"

    if [[ -n "$kind_flag" ]]; then
        local plural="${KIND_SHORT_TO_PLURAL[$kind_flag]:-}"
        if [[ -z "$plural" ]]; then
            error "Unknown kind '${kind_flag}'. Valid kinds: ${!KIND_SHORT_TO_PLURAL[*]}"
        fi
        echo "$plural"
    else
        detect_kind "$suite_name" "$namespace"
    fi
}

# ── Commands ──────────────────────────────────────────────────────────────────

cmd_run() {
    local file="" namespace=""
    namespace="$(resolve_namespace)"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -n) namespace="$2"; shift 2 ;;
            -*) error "Unknown flag: $1" ;;
            *)  file="$1"; shift ;;
        esac
    done

    [[ -z "$file" ]] && error "Usage: sherlock.sh run <file.yaml> [-n namespace]"
    [[ ! -f "$file" ]] && error "File not found: $file"

    info "Applying Suite from ${file} in namespace '${namespace}'..."

    # Ensure namespace exists
    if ! kubectl get namespace "$namespace" &>/dev/null; then
        info "Creating namespace '${namespace}'..."
        kubectl create namespace "$namespace"
    fi

    kubectl apply -f "$file" -n "$namespace"
    success "Suite applied. Run 'sherlock.sh status <suite-name>' to monitor progress."
}


cmd_status() {
    local suite_name="" namespace="" kind_flag=""
    namespace="$(resolve_namespace)"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -n) namespace="$2"; shift 2 ;;
            -k|--kind) kind_flag="$2"; shift 2 ;;
            -*) error "Unknown flag: $1" ;;
            *)  suite_name="$1"; shift ;;
        esac
    done

    [[ -z "$suite_name" ]] && error "Usage: sherlock.sh status <suite-name> [-n ns] [-k kind]"

    local plural
    plural="$(resolve_kind "$suite_name" "$namespace" "$kind_flag")"
    local label="${SUITE_KINDS[$plural]}"

    # Fetch the full Suite object as JSON
    local suite_json
    suite_json="$(kubectl get "$plural" "$suite_name" -n "$namespace" -o json)"

    local phase total completed failed remaining current_run
    phase="$(echo "$suite_json" | jq -r '.status.phase // "Unknown"')"
    total="$(echo "$suite_json" | jq -r '.status.summary.totalRuns // 0')"
    completed="$(echo "$suite_json" | jq -r '.status.summary.runsCompleted // 0')"
    failed="$(echo "$suite_json" | jq -r '.status.summary.runsFailed // 0')"
    remaining="$(echo "$suite_json" | jq -r '.status.summary.runsRemaining // 0')"
    current_run="$(echo "$suite_json" | jq -r '.status.summary.currentRunName // "-"')"

    # Header
    echo ""
    echo -e "${C_BOLD}Sherlock Suite: ${suite_name}${C_RESET}  ${C_GRAY}[${label}]${C_RESET}  namespace: ${namespace}"
    rule
    printf "  %-20s %s\n" "Phase:" "$(phase_color "$phase")"
    printf "  %-20s %s / %s  (failed: %s, remaining: %s)\n" \
        "Progress:" "$completed" "$total" "$failed" "$remaining"
    [[ "$current_run" != "-" && "$current_run" != "null" ]] && \
        printf "  %-20s %s\n" "Current run:" "$current_run"
    rule

    # Run matrix table
    local run_count
    run_count="$(echo "$suite_json" | jq '.status.runMatrix | length')"

    if [[ "$run_count" -eq 0 ]]; then
        echo "  No runs started yet."
    else
        printf "\n  ${C_BOLD}%-45s %-12s %-10s %-10s${C_RESET}\n" \
            "Run name" "Phase" "Started" "Completed"
        rule

        echo "$suite_json" | jq -r '
            .status.runMatrix[] |
            [
                .name,
                (.phase // "Pending"),
                (.startTime // "-" | if . != "-" then .[11:16] else "-" end),
                (.completionTime // "-" | if . != "-" then .[11:16] else "-" end)
            ] | @tsv
        ' | while IFS=$'\t' read -r name phase start end; do
            local colored_phase
            colored_phase="$(phase_color "$phase")"
            printf "  %-45s %-20s %-10s %-10s\n" \
                "$name" "$colored_phase" "$start" "$end"
        done
    fi
    echo ""
}


cmd_results() {
    local suite_name="" namespace="" kind_flag="" format="table"
    namespace="$(resolve_namespace)"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -n) namespace="$2"; shift 2 ;;
            -k|--kind) kind_flag="$2"; shift 2 ;;
            -f|--format) format="$2"; shift 2 ;;
            -*) error "Unknown flag: $1" ;;
            *)  suite_name="$1"; shift ;;
        esac
    done

    [[ -z "$suite_name" ]] && \
        error "Usage: sherlock.sh results <suite-name> [-n ns] [-k kind] [-f table|json|csv]"

    local plural
    plural="$(resolve_kind "$suite_name" "$namespace" "$kind_flag")"
    local label="${SUITE_KINDS[$plural]}"

    local suite_json
    suite_json="$(kubectl get "$plural" "$suite_name" -n "$namespace" -o json)"

    case "$format" in
        json) _results_json "$suite_json" ;;
        csv)  _results_csv  "$suite_json" "$label" ;;
        table|*) _results_table "$suite_json" "$suite_name" "$label" "$namespace" ;;
    esac
}

_results_json() {
    local suite_json="$1"
    echo "$suite_json" | jq '[
        .status.runMatrix[] |
        select(.phase == "Completed") |
        {
            name: .name,
            params: .params,
            result: .result,
            completionTime: .completionTime
        }
    ]'
}

_results_csv() {
    local suite_json="$1"
    local label="$2"

    # Detect which primary metric is present
    local primary_key
    case "$label" in
        pgbench)  primary_key="tps" ;;
        sysbench) primary_key="tps" ;;
        hammerdb) primary_key="nopm" ;;
        ycsb)     primary_key="throughputOpsPerSec" ;;
        fio)      primary_key="totalIops" ;;
        *)        primary_key="tps" ;;
    esac

    # CSV header
    echo "run_name,phase,${primary_key},avgReadIops,avgWriteIops,avgReadBwMBs,avgWriteBwMBs,avgLatencyMs,p95LatencyMs,p99LatencyMs,completionTime"

    # CSV rows
    echo "$suite_json" | jq -r --arg pk "$primary_key" '
        .status.runMatrix[] |
        select(.phase == "Completed") |
        [
            .name,
            .phase,
            (.result[$pk] // ""),
            (.result.avgReadIops // ""),
            (.result.avgWriteIops // ""),
            (.result.avgReadBwMBs // ""),
            (.result.avgWriteBwMBs // ""),
            (.result.avgLatencyMs // ""),
            (.result.p95LatencyMs // ""),
            (.result.p99LatencyMs // ""),
            (.completionTime // "")
        ] | @csv
    '
}

_results_table() {
    local suite_json="$1"
    local suite_name="$2"
    local label="$3"
    local namespace="$4"

    # Detect primary metric
    local primary_key primary_label
    case "$label" in
        pgbench)  primary_key="tps";               primary_label="TPS" ;;
        sysbench) primary_key="tps";               primary_label="TPS" ;;
        hammerdb) primary_key="nopm";              primary_label="NOPM" ;;
        ycsb)     primary_key="throughputOpsPerSec"; primary_label="Ops/sec" ;;
        fio)      primary_key="totalIops";         primary_label="Total IOPS" ;;
        *)        primary_key="tps";               primary_label="TPS" ;;
    esac

    local completed
    completed="$(echo "$suite_json" | jq '[.status.runMatrix[] | select(.phase == "Completed")] | length')"

    echo ""
    echo -e "${C_BOLD}Results: ${suite_name}${C_RESET}  ${C_GRAY}[${label}]${C_RESET}  ${completed} completed runs"
    rule

    printf "  ${C_BOLD}%-40s %10s %10s %10s %10s %10s${C_RESET}\n" \
        "Run name" "$primary_label" "Rd IOPS" "Wr IOPS" "Lat avg" "Lat p95"
    rule

    echo "$suite_json" | jq -r --arg pk "$primary_key" '
        .status.runMatrix[] |
        select(.phase == "Completed") |
        [
            .name,
            (.result[$pk] // "-"),
            (.result.avgReadIops // "-"),
            (.result.avgWriteIops // "-"),
            (.result.avgLatencyMs // "-"),
            (.result.p95LatencyMs // "-")
        ] | @tsv
    ' | while IFS=$'\t' read -r name primary rd_iops wr_iops lat_avg lat_p95; do
        printf "  %-40s %10s %10s %10s %10s %10s\n" \
            "$name" "$primary" "$rd_iops" "$wr_iops" "$lat_avg" "$lat_p95"
    done

    echo ""
    # Footer: fio also shows read/write BW
    if [[ "$label" == "fio" ]]; then
        echo -e "  ${C_GRAY}Tip: use -f csv to export full bandwidth and latency percentile data${C_RESET}"
        echo ""
    fi
}


cmd_list() {
    local namespace=""
    namespace="$(resolve_namespace)"
    local all_namespaces=false

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -n) namespace="$2"; shift 2 ;;
            -A|--all-namespaces) all_namespaces=true; shift ;;
            -*) error "Unknown flag: $1" ;;
            *) shift ;;
        esac
    done

    echo ""
    echo -e "${C_BOLD}Sherlock Suites${C_RESET}  namespace: ${namespace}"
    rule
    printf "  ${C_BOLD}%-35s %-12s %-10s %-8s %-8s %-12s${C_RESET}\n" \
        "Name" "Kind" "Phase" "Done" "Failed" "Age"
    rule

    local found=false
    for plural in "${!SUITE_KINDS[@]}"; do
        local label="${SUITE_KINDS[$plural]}"
        local ns_flag="-n $namespace"
        $all_namespaces && ns_flag="--all-namespaces"

        # shellcheck disable=SC2086
        local items
        items="$(kubectl get "$plural" $ns_flag \
            --ignore-not-found -o json 2>/dev/null | jq -r '
            .items[] |
            [
                .metadata.name,
                .metadata.namespace,
                (.status.phase // "Pending"),
                (.status.summary.runsCompleted // 0 | tostring),
                (.status.summary.runsFailed // 0 | tostring),
                .metadata.creationTimestamp
            ] | @tsv
        ')" || continue

        [[ -z "$items" ]] && continue
        found=true

        while IFS=$'\t' read -r name ns phase done failed created; do
            # Compute age from creationTimestamp
            local age="-"
            if command -v python3 &>/dev/null && [[ -n "$created" ]]; then
                age="$(python3 -c "
from datetime import datetime, timezone
created = datetime.fromisoformat('${created}'.replace('Z','+00:00'))
delta = datetime.now(timezone.utc) - created
d,s = delta.days, delta.seconds
if d > 0: print(f'{d}d')
elif s > 3600: print(f'{s//3600}h')
else: print(f'{s//60}m')
" 2>/dev/null || echo "-")"
            fi

            local colored_phase
            colored_phase="$(phase_color "$phase")"
            printf "  %-35s %-12s %-20s %-8s %-8s %-12s\n" \
                "$name" "$label" "$colored_phase" "$done" "$failed" "$age"
        done <<< "$items"
    done

    if ! $found; then
        echo "  No Sherlock suites found in namespace '${namespace}'."
        echo "  Run 'sherlock.sh run <file.yaml>' to create one."
    fi
    echo ""
}


cmd_logs() {
    local suite_name="" run_name="" namespace="" kind_flag=""
    namespace="$(resolve_namespace)"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -n) namespace="$2"; shift 2 ;;
            -k|--kind) kind_flag="$2"; shift 2 ;;
            -*) error "Unknown flag: $1" ;;
            *)
                if [[ -z "$suite_name" ]]; then suite_name="$1"
                elif [[ -z "$run_name" ]]; then run_name="$1"
                fi
                shift ;;
        esac
    done

    [[ -z "$suite_name" || -z "$run_name" ]] && \
        error "Usage: sherlock.sh logs <suite-name> <run-name> [-n ns] [-k kind]"

    # Find the job pod for this run
    info "Finding pods for run '${run_name}'..."
    local pod
    pod="$(kubectl get pods -n "$namespace" \
        -l "sherlock.io/run-name=${run_name},sherlock.io/role=benchmark" \
        --ignore-not-found -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)"

    if [[ -z "$pod" ]]; then
        # Try finding via job name pattern
        warn "No running pod found. Searching completed job pods..."
        pod="$(kubectl get pods -n "$namespace" \
            -l "sherlock.io/suite-name=${suite_name}" \
            --field-selector="status.phase=Succeeded" \
            --ignore-not-found -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)"
    fi

    if [[ -z "$pod" ]]; then
        error "No pods found for suite '${suite_name}' run '${run_name}'.\n" \
              "The pod may have been TTL-cleaned. Check job logs with:\n" \
              "  kubectl get jobs -n ${namespace} -l sherlock.io/run-name=${run_name}"
    fi

    info "Streaming logs from pod '${pod}'..."
    kubectl logs -n "$namespace" "$pod" --follow 2>/dev/null || \
        kubectl logs -n "$namespace" "$pod"
}


cmd_delete() {
    local suite_name="" namespace="" kind_flag="" delete_pvcs=false
    namespace="$(resolve_namespace)"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -n) namespace="$2"; shift 2 ;;
            -k|--kind) kind_flag="$2"; shift 2 ;;
            --delete-pvcs) delete_pvcs=true; shift ;;
            -*) error "Unknown flag: $1" ;;
            *)  suite_name="$1"; shift ;;
        esac
    done

    [[ -z "$suite_name" ]] && \
        error "Usage: sherlock.sh delete <suite-name> [-n ns] [-k kind] [--delete-pvcs]"

    local plural
    plural="$(resolve_kind "$suite_name" "$namespace" "$kind_flag")"
    local label="${SUITE_KINDS[$plural]}"

    warn "About to delete Suite '${suite_name}' [${label}] from namespace '${namespace}'."
    if $delete_pvcs; then
        warn "PVCs will also be deleted (--delete-pvcs specified)."
    else
        warn "PVCs will be retained. Use --delete-pvcs to also delete storage."
    fi

    read -r -p "Continue? [y/N] " confirm
    [[ "$confirm" =~ ^[Yy]$ ]] || { info "Cancelled."; exit 0; }

    kubectl delete "$plural" "$suite_name" -n "$namespace"
    success "Suite '${suite_name}' deleted."

    if $delete_pvcs; then
        info "Deleting PVCs for suite '${suite_name}'..."
        kubectl delete pvc -n "$namespace" \
            -l "sherlock.io/suite=${suite_name}" \
            --ignore-not-found
        success "PVCs deleted."
    fi
}


cmd_version() {
    echo "sherlock.sh ${VERSION}"
    echo "kubectl: $(kubectl version --client -o json 2>/dev/null | jq -r '.clientVersion.gitVersion' 2>/dev/null || echo 'unknown')"
}


cmd_help() {
    cat << EOF

${C_BOLD}sherlock.sh${C_RESET} — Sherlock v2 helper script ${C_GRAY}(${VERSION})${C_RESET}

${C_BOLD}USAGE${C_RESET}
  sherlock.sh <command> [options]

${C_BOLD}COMMANDS${C_RESET}
  ${C_CYAN}run${C_RESET}      <file.yaml>              Apply a Suite CR to the cluster
  ${C_CYAN}status${C_RESET}   <suite>                  Show run matrix progress
  ${C_CYAN}results${C_RESET}  <suite>                  Show parsed metrics table
  ${C_CYAN}export${C_RESET}   <suite> -f json|csv      Export results (pipe-friendly)
  ${C_CYAN}list${C_RESET}                              List all Suite CRs
  ${C_CYAN}logs${C_RESET}     <suite> <run>            Stream logs from a benchmark pod
  ${C_CYAN}delete${C_RESET}   <suite> [--delete-pvcs]  Delete a Suite CR
  ${C_CYAN}version${C_RESET}                           Show version info
  ${C_CYAN}help${C_RESET}                              Show this help

${C_BOLD}OPTIONS${C_RESET}
  -n <namespace>    Namespace (default: 'sherlock', or \$SHERLOCK_NAMESPACE)
  -k <kind>         Suite kind: pgbench | hammerdb | sysbench | ycsb | fio
                    (auto-detected if not specified)
  -f <format>       Output format for 'results': table (default) | json | csv

${C_BOLD}NAMESPACE${C_RESET}
  Default namespace is 'sherlock'. Override with:
    sherlock.sh status my-suite -n my-namespace
    SHERLOCK_NAMESPACE=my-namespace sherlock.sh status my-suite

${C_BOLD}EXAMPLES${C_RESET}
  # Run a suite
  sherlock.sh run ./examples/hammerdb-example.yaml

  # Monitor progress
  sherlock.sh status mssql-vu-sweep
  watch -n5 sherlock.sh status mssql-vu-sweep

  # View results
  sherlock.sh results mssql-vu-sweep
  sherlock.sh results mssql-vu-sweep -f json | jq '.[] | .result.nopm'
  sherlock.sh results mssql-vu-sweep -f csv > results.csv

  # List all suites
  sherlock.sh list
  sherlock.sh list -n team-storage

  # Stream logs from a running job
  sherlock.sh logs mssql-vu-sweep suite-vu16-wh100-rm2-tm5

  # Clean up
  sherlock.sh delete mssql-vu-sweep
  sherlock.sh delete mssql-vu-sweep --delete-pvcs

${C_BOLD}DEPENDENCIES${C_RESET}
  Required: kubectl (configured for your cluster), jq
  Optional: python3 (for age calculation in 'list' command)

EOF
}


# ── Main dispatcher ───────────────────────────────────────────────────────────

main() {
    require_kubectl

    if ! command -v jq &>/dev/null; then
        error "jq not found. Please install jq (https://jqlang.github.io/jq/).\n" \
              "  brew install jq      # macOS\n" \
              "  dnf install jq       # RHEL/Fedora\n" \
              "  apt install jq       # Debian/Ubuntu"
    fi

    local cmd="${1:-help}"
    shift || true

    case "$cmd" in
        run)     cmd_run     "$@" ;;
        status)  cmd_status  "$@" ;;
        results) cmd_results "$@" ;;
        export)  cmd_results "$@" ;;   # alias for results with -f flag
        list)    cmd_list    "$@" ;;
        logs)    cmd_logs    "$@" ;;
        delete)  cmd_delete  "$@" ;;
        version) cmd_version       ;;
        help|-h|--help) cmd_help   ;;
        *)       error "Unknown command: '${cmd}'. Run 'sherlock.sh help' for usage." ;;
    esac
}

main "$@"
