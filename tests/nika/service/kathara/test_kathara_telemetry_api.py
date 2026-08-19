from __future__ import annotations

import pytest
import json
from nika.service.kathara.telemetry_api import KatharaTelemetryAPI
from tests.support.prerequisites import p4_int_prerequisites
from tests.support.kathara_api_base import KatharaScenarioApiSmokeTest

COLLECTOR = "collector"
MEASUREMENT = "flow_stat"


@pytest.mark.skipif(
    not p4_int_prerequisites(), reason="Docker or nika/influxdb image not available"
)
class KatharaTelemetryApiSmokeTest(KatharaScenarioApiSmokeTest):
    SCENARIO = "p4_int"

    def _telemetry_api(self) -> KatharaTelemetryAPI:
        return KatharaTelemetryAPI(lab_name=self._lab_name())

    def test_kathara_telemetry_influx_api(self) -> None:
        api = self._telemetry_api()
        buckets = self.smoke(
            "KatharaTelemetryAPI.influx_list_buckets",
            lambda: api.influx_list_buckets(COLLECTOR),
            expect_type=list,
        )

        assert buckets
        bucket_payload = buckets[0]

        assert "int_bucket" in bucket_payload
        measurements = self.smoke(
            "KatharaTelemetryAPI.influx_get_measurements",
            lambda: api.influx_get_measurements(COLLECTOR),
            expect_type=list,
        )

        assert measurements
        count_rows = self.smoke(
            "KatharaTelemetryAPI.influx_count_measurements",
            lambda: api.influx_count_measurements(MEASUREMENT, host_name=COLLECTOR),
            expect_type=list,
        )

        assert len(count_rows) == 1
        json.loads(count_rows[0])
        sample_rows = self.smoke(
            "KatharaTelemetryAPI.influx_query_measurement",
            lambda: api.influx_query_measurement(
                MEASUREMENT, limit=5, offset=0, host_name=COLLECTOR
            ),
            expect_type=list,
        )

        assert len(sample_rows) == 1
        json.loads(sample_rows[0])
