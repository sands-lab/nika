"""Read mesh history and report latency coverage separately from readiness."""

import math

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
    current = {
        (row["metric"]["source"], row["metric"]["target"]): row["values"]
        for row in latency
    }
    paths = set(baselines) | set(current)
    paths.update(
        (row["metric"]["source"], row["metric"]["target"])
        for row in rows
        if row["metric"].get("expected") != "isolated"
        and row["metric"].get("module", "icmp") == "icmp"
    )
    unverified = []
    latency_failed = False
    for source, target in sorted(paths):
        path = (source, target)
        baseline = baselines.get(path)
        declared = intent is not None and intent.max_expected_rtt_ms is not None
        threshold = intent.latency_threshold_ms(0) / 1000 if declared else None
        basis = "declared design limit"
        if baseline is not None:
            relative = max(0.05, baseline * 2.5, baseline + 0.075)
            if threshold is None or relative < threshold:
                threshold = relative
                basis = f"healthy baseline {baseline} seconds"

        # Count raw scrapes, not overlapping maxima or resampled instant values.
        def values(points):
            return [
                value
                for _stamp, value in points
                if isinstance(value, (int, float))
                and math.isfinite(value)
                and value > 0
            ]

        samples = values(current.get(path, []))
        earlier = values(previous.get(path, []))
        if threshold is None or min(len(samples), len(earlier)) < 2:
            unverified.append(
                {
                    "source": source,
                    "target": target,
                    "type": "latency_unverified",
                    "detail": "A declared latency limit or healthy baseline and repeated observations are required.",
                }
            )
            continue
        high = sustained_high_latency(samples, threshold)
        prior_high = sustained_high_latency(earlier, threshold)
        if not high or not prior_high:
            continue
        latency_failed = True
        anomalies.append(
            {
                "source": source,
                "target": target,
                "type": "high_latency",
                "detail": f"ICMP RTT exceeds {threshold} seconds ({basis}) in {prior_high}/{len(earlier)} and {high}/{len(samples)} samples in consecutive one-minute windows",
            }
        )
    pairs = {(row["metric"]["source"], row["metric"]["target"]) for row in rows}
    return {
        "summary": {
            "total_pairs": len(pairs),
            "observed_targets": len(rows),
            "anomaly_count": len(anomalies),
            "latency_status": "failed"
            if latency_failed
            else "unverified"
            if unverified
            else "verified"
            if paths
            else "not_applicable",
            "latency_pairs": len(paths),
            "latency_unverified_pairs": len(unverified),
        },
        "anomalies": anomalies,
        "unverified": unverified,
    }
