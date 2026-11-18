# Shared by the two k3s controller startup scripts (BusyBox ash).
set -eu
STAGE=network

stage_exit() {
  code=$?
  if [ "$code" -ne 0 ]; then
    mkdir -p /var/run
    printf '%s failed (exit %s)\n' "$STAGE" "$code" > /var/run/startup-failed.tmp
    mv /var/run/startup-failed.tmp /var/run/startup-failed
    echo "[startup] $(cat /var/run/startup-failed)" >&2
    kubectl --request-timeout=5s get pods -A -o wide || true
    kubectl --request-timeout=5s get events -A --sort-by=.lastTimestamp | tail -30 || true
  fi
}
trap stage_exit EXIT

stage_run() {
  STAGE=$1
  shift
  echo "[startup] $STAGE"
  "$@"
}

stage_wait() {
  STAGE=$1
  budget=$2
  shift 2
  echo "[startup] waiting for $STAGE (up to ${budget}s)"
  deadline=$(($(date +%s) + budget))
  until "$@"; do
    # These states require a configuration/image/process fix, not more waiting.
    failures=
    if [ -f /var/run/images-preloaded ]; then
      failures=$(kubectl --request-timeout=5s get pods -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{" "}{.status.phase}{" "}{range .status.initContainerStatuses[*]}{.state.waiting.reason}{" "}{end}{range .status.containerStatuses[*]}{.state.waiting.reason}{" "}{end}{"\n"}{end}' 2>/dev/null || true)
    fi
    if printf '%s\n' "$failures" | grep -E 'ErrImageNeverPull|InvalidImageName|CreateContainerConfigError|CreateContainerError|CrashLoopBackOff| Failed ' >&2; then
      echo "[startup] terminal Pod failure during $STAGE" >&2
      return 1
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      echo "[startup] timeout during $STAGE" >&2
      return 1
    fi
    sleep 2
  done
}

stage_ready() {
  stage=$1
  shift
  stage_wait "$stage" 300 kubectl --request-timeout=15s wait --for=condition=ready pod --timeout=5s "$@"
}
