# Shared by the two k3s controller startup scripts (BusyBox ash).
set -eu
NIKA_STAGE=network

nika_exit() {
  code=$?
  if [ "$code" -ne 0 ]; then
    mkdir -p /var/run
    printf '%s failed (exit %s)\n' "$NIKA_STAGE" "$code" > /var/run/nika-startup-failed.tmp
    mv /var/run/nika-startup-failed.tmp /var/run/nika-startup-failed
    echo "[nika-startup] $(cat /var/run/nika-startup-failed)" >&2
    kubectl --request-timeout=5s get pods -A -o wide || true
    kubectl --request-timeout=5s get events -A --sort-by=.lastTimestamp | tail -30 || true
  fi
}
trap nika_exit EXIT

nika_run() {
  NIKA_STAGE=$1
  shift
  echo "[nika-startup] $NIKA_STAGE"
  "$@"
}

nika_wait() {
  NIKA_STAGE=$1
  budget=$2
  shift 2
  echo "[nika-startup] waiting for $NIKA_STAGE (up to ${budget}s)"
  deadline=$(($(date +%s) + budget))
  until "$@"; do
    # These states require a configuration/image/process fix, not more waiting.
    failures=
    if [ -f /var/run/nika-images-preloaded ]; then
      failures=$(kubectl --request-timeout=5s get pods -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{" "}{.status.phase}{" "}{range .status.initContainerStatuses[*]}{.state.waiting.reason}{" "}{end}{range .status.containerStatuses[*]}{.state.waiting.reason}{" "}{end}{"\n"}{end}' 2>/dev/null || true)
    fi
    if printf '%s\n' "$failures" | grep -E 'ErrImageNeverPull|InvalidImageName|CreateContainerConfigError|CreateContainerError|CrashLoopBackOff| Failed ' >&2; then
      echo "[nika-startup] terminal Pod failure during $NIKA_STAGE" >&2
      return 1
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      echo "[nika-startup] timeout during $NIKA_STAGE" >&2
      return 1
    fi
    sleep 2
  done
}

nika_ready() {
  stage=$1
  shift
  nika_wait "$stage" 300 kubectl --request-timeout=15s wait --for=condition=ready pod --timeout=5s "$@"
}
