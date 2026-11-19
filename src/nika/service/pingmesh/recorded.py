"""Read observed mesh history for healthy-state auditing."""

from nika.service.kathara.prometheus_api import prometheus_query_for
from nika.service.pingmesh.baseline import sustained_high_latency


def recorded_mesh(
    api, intent=None, *, baselines: dict[tuple[str, str], float | None]
) -> dict:
    rows = prometheus_query_for(
        api,
        query='min_over_time(probe_success{expected="reachable"}[1m]) or max_over_time(probe_success{expected="isolated"}[1m])',
        limit=None,
    )
    if not rows:
        raise RuntimeError("No recorded probe samples are available yet.")
    anomalies = []
    for row in rows:
        labels = row["metric"]
        expected = 0 if labels.get("expected") == "isolated" else 1
        if row["value"] != expected:
            anomalies.append(
                {
                    "source": labels["source"],
                    "target": labels["target"],
                    "type": "policy_violation" if expected == 0 else "unreachable",
                    "detail": f"{labels.get('module')} probe success={row['value']}",
                }
            )
    unavailable = prometheus_query_for(api, query='up{job="pingmesh"} == 0', limit=None)
    for row in unavailable:
        labels = row["metric"]
        anomalies.append(
            {
                "source": labels["source"],
                "target": labels["target"],
                "type": "unknown",
                "detail": "The endpoint prober scrape failed.",
            }
        )
    latency = prometheus_query_for(
        api,
        query='probe_icmp_duration_seconds{phase="rtt",expected="reachable"}[1m]',
        limit=None,
    )
    previous_latency = prometheus_query_for(
        api,
        query='probe_icmp_duration_seconds{phase="rtt",expected="reachable"}[1m] offset 1m',
        limit=None,
    )
    previous = {
        (row["metric"]["source"], row["metric"]["target"]): row["values"]
        for row in previous_latency
    }
    for row in latency:
        labels = row["metric"]
        path = (labels["source"], labels["target"])
        if path not in baselines:
            anomalies.append(
                {
                    "source": labels["source"],
                    "target": labels["target"],
                    "type": "unknown",
                    "detail": "No healthy latency baseline is available for this path.",
                }
            )
            continue
        baseline = baselines[path]
        if baseline is None:
            continue
        threshold = max(0.05, baseline * 2.5, baseline + 0.075)
        # Count raw scrapes, not overlapping maxima or resampled instant values.
        samples = [value for _stamp, value in row["values"]]
        high = sustained_high_latency(samples, threshold)
        earlier = [
            value
            for _stamp, value in previous.get((labels["source"], labels["target"]), [])
        ]
        prior_high = sustained_high_latency(earlier, threshold)
        if not high or not prior_high:
            continue
        anomalies.append(
            {
                "source": labels["source"],
                "target": labels["target"],
                "type": "high_latency",
                "detail": f"ICMP RTT exceeds {threshold} seconds (healthy baseline {baseline} seconds) in {prior_high}/{len(earlier)} and {high}/{len(samples)} samples in consecutive one-minute windows",
            }
        )
    pairs = {(row["metric"]["source"], row["metric"]["target"]) for row in rows}
    return {
        "summary": {
            "total_pairs": len(pairs),
            "observed_targets": len(rows),
            "anomaly_count": len(anomalies),
        },
        "anomalies": anomalies,
    }
