"""Background traffic profiles that failures declare (``ProblemBase.TRAFFIC_PROFILE``)."""

# Partial loss and corruption show in the flows that cross the faulty element,
# as retransmissions and slow transfers; diagnosis systems such as 007 (NSDI
# 2018) and NetBouncer (NSDI 2019) read that evidence from ordinary traffic.
# Uniform all-to-all flows put traffic on every host pair, so some of it
# crosses the fault wherever it is.
LOSS_EVIDENCE_TRAFFIC = {"pattern": "uniform"}
