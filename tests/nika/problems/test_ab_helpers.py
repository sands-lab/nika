"""Unit tests for ApacheBench summary parsing."""

from __future__ import annotations

from nika.problems.service_networking.ab_helpers import parse_ab_output


_SAMPLE = """
This is ApacheBench, Version 2.3
Benchmarking web99.local (be patient)...
Completed 80 requests

Server Software:
Server Hostname:        web99.local
Server Port:            80

Document Path:          /small
Document Length:        2048 bytes

Concurrency Level:      4
Time taken for tests:   0.512 seconds
Complete requests:      80
Failed requests:        2
Non-2xx responses:      1
Total transferred:      180000 bytes
HTML transferred:       163840 bytes
Requests per second:    156.25 [#/sec] (mean)
Time per request:       25.600 [ms] (mean)
Time per request:       6.400 [ms] (mean, across all concurrent requests)
Transfer rate:          343.75 [Kbytes/sec] received

Percentage of the requests served within a certain time (ms)
  50%      8
  66%     10
  75%     12
  80%     14
  90%     20
  95%     28
  98%     40
  99%     55
 100%     90 (longest request)
"""


def test_parse_ab_output_percentiles_and_errors() -> None:
    summary = parse_ab_output(_SAMPLE)
    assert summary.requests_per_sec == 156.25
    assert summary.complete_requests == 80
    assert summary.failed_requests == 2
    assert summary.non_2xx_responses == 1
    assert summary.error_count == 3
    assert summary.p50_ms == 8.0
    assert summary.p95_ms == 28.0
    assert summary.p99_ms == 55.0
    assert summary.error_rate == 3 / 80


def test_parse_ab_output_empty() -> None:
    summary = parse_ab_output("")
    assert summary.requests_per_sec is None
    assert summary.p95_ms is None
    assert summary.failed_requests == 0
