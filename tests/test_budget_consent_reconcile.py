"""Synthetic unit tests for pure bank-consent reconciliation and retention classification."""
from __future__ import annotations

from datetime import datetime, timezone
import math
import random
import unittest

from hestia.budget.consent_reconcile import (
    CONSENT_CLASSIFICATIONS,
    DEFAULT_RETENTION_DAYS,
    FLOW_CLASSIFICATIONS,
    HESTIA_PROVENANCE,
    MAX_CONSENTS,
    MAX_FLOWS,
    MAX_RETENTION_DAYS,
    MAX_TIMESTAMP,
    RULE_VERSION,
    SECONDS_PER_DAY,
    ConsentFlowRecord,
    ConsentReconcileInputError,
    ConsentReconcileLimitError,
    ConsentRecord,
    classify_consents,
)

NOW = 1_800_000_000
RET = DEFAULT_RETENTION_DAYS * SECONDS_PER_DAY


def _consent(consent_id="con-1", **changes):
    values = {
        "owner": "owner",
        "consent_id": consent_id,
        "connection_id": "bank-a",
        "provenance": HESTIA_PROVENANCE,
        "status": "ACTIVE",
        "created_at": NOW - 10 * SECONDS_PER_DAY,
        "valid_until": NOW + 30 * SECONDS_PER_DAY,
        "generation": 1,
    }
    values.update(changes)
    return ConsentRecord(**values)


def _flow(flow_id="flow-1", **changes):
    values = {
        "owner": "owner",
        "flow_id": flow_id,
        "connection_id": "bank-a",
        "status": "adopted",
        "created_at": NOW - 10 * SECONDS_PER_DAY,
        "expires_at": NOW - 10 * SECONDS_PER_DAY + 900,
        "candidate_consent_id": None,
    }
    values.update(changes)
    return ConsentFlowRecord(**values)


class ConsentReconcileTests(unittest.TestCase):
    def test_all_consent_and_flow_classifications_and_poc_external_immutability(self):
        consents = [
            _consent("c-poc", provenance="poc_retired", status="RETIRED", valid_until=NOW - RET - 10),
            _consent("c-ext", provenance="external_file", status="UNADOPTED", valid_until=NOW + 100),
            _consent("c-act-cur", status="ACTIVE", valid_until=NOW + 1),
            _consent("c-act-exp", status="ACTIVE", valid_until=NOW),
            _consent("c-ptr-drift", connection_id="bank-ptr", status="RETIRED", valid_until=NOW + 10),
            _consent("c-unad-uncertain", status="UNADOPTED", valid_until=NOW - RET - 100),
            _consent("c-unad-stale", status="UNADOPTED", valid_until=NOW - RET - 100),
            _consent("c-unad-prog", status="UNADOPTED", valid_until=NOW + 60),
            _consent("c-unad-orph", status="UNADOPTED", valid_until=NOW + 60),
            _consent("c-unad-orph-conn-mismatch", connection_id="bank-b", status="UNADOPTED", valid_until=None),
            _consent("c-unad-exp-ret", status="UNADOPTED", valid_until=NOW - RET + 1),
            _consent("c-unad-exp-pru", status="UNADOPTED", valid_until=NOW - RET),
            _consent("c-ret-live", status="RETIRED", valid_until=NOW + 10),
            _consent("c-ret-ret", status="RETIRED", valid_until=NOW - RET + 1),
            _consent("c-ret-pru", status="RETIRED", valid_until=NOW - RET),
        ]
        flows = [
            _flow("f-uncertain", status="exchange_uncertain",
                  created_at=NOW - RET - 1000, expires_at=NOW - RET - 100,
                  candidate_consent_id="c-unad-uncertain"),
            _flow("f-stale", status="started", created_at=NOW - 1000, expires_at=NOW,
                  candidate_consent_id="c-unad-stale"),
            _flow("f-cand-live", status="candidate", created_at=NOW - 10, expires_at=NOW + 600,
                  candidate_consent_id="c-unad-prog"),
            _flow("f-cand-wrong-conn", connection_id="bank-a", status="candidate",
                  created_at=NOW - 10, expires_at=NOW + 600, candidate_consent_id="c-unad-orph-conn-mismatch"),
            _flow("f-term-ret", status="adopted", created_at=NOW - RET, expires_at=NOW - RET + 1),
            _flow("f-term-pru", status="canceled", created_at=NOW - RET - 100, expires_at=NOW - RET),
        ]
        report = classify_consents(
            "owner", NOW, (c for c in consents), (f for f in flows),
            active_consent_ids={"bank-a": "c-act-cur", "bank-ptr": "c-ptr-drift"},
        )
        self.assertEqual(report.rule_version, RULE_VERSION)
        by_c = {item.consent_id: item for item in report.consents}
        by_f = {item.flow_id: item for item in report.flows}
        self.assertEqual({item.classification for item in report.consents}, set(CONSENT_CLASSIFICATIONS))
        self.assertEqual({item.classification for item in report.flows}, set(FLOW_CLASSIFICATIONS))

        # External and retired PoC sessions are ALWAYS read-only (never remotely closed or locally retired).
        for cid in ("c-poc", "c-ext"):
            item = by_c[cid]
            self.assertEqual((item.classification, item.needs_owner_review,
                              item.remote_close_eligible, item.local_retire_eligible),
                             ("external_read_only", False, False, False))

        self.assertEqual((by_c["c-act-cur"].classification, by_c["c-act-cur"].needs_owner_review),
                         ("active_current", False))
        self.assertEqual((by_c["c-act-exp"].classification, by_c["c-act-exp"].needs_owner_review,
                          by_c["c-act-exp"].remote_close_eligible, by_c["c-act-exp"].local_retire_eligible),
                         ("active_expired", True, False, False))
        # Defense-in-depth: a live active_consent_id pointer is never marked closable or prunable even if drifted.
        self.assertEqual((by_c["c-ptr-drift"].classification, by_c["c-ptr-drift"].needs_owner_review,
                          by_c["c-ptr-drift"].remote_close_eligible, by_c["c-ptr-drift"].local_retire_eligible),
                         ("active_pointer_mismatch", True, False, False))
        # Evidence referenced by an exchange_uncertain flow is never auto-prunable or auto-closable.
        self.assertEqual((by_c["c-unad-uncertain"].classification, by_c["c-unad-uncertain"].needs_owner_review,
                          by_c["c-unad-uncertain"].remote_close_eligible,
                          by_c["c-unad-uncertain"].local_retire_eligible),
                         ("uncertain_flow_evidence", True, False, False))
        self.assertEqual((by_c["c-unad-prog"].classification, by_c["c-unad-prog"].needs_owner_review,
                          by_c["c-unad-prog"].remote_close_eligible),
                         ("unadopted_in_progress", False, False))
        for cid in ("c-unad-orph", "c-unad-orph-conn-mismatch"):
            self.assertEqual((by_c[cid].classification, by_c[cid].needs_owner_review,
                              by_c[cid].remote_close_eligible, by_c[cid].local_retire_eligible),
                             ("unadopted_orphan", True, True, False))
        self.assertEqual((by_c["c-unad-exp-ret"].classification, by_c["c-unad-exp-ret"].needs_owner_review,
                          by_c["c-unad-exp-ret"].local_retire_eligible),
                         ("unadopted_expired_retained", True, False))
        self.assertEqual((by_c["c-unad-exp-pru"].classification, by_c["c-unad-exp-pru"].needs_owner_review,
                          by_c["c-unad-exp-pru"].local_retire_eligible),
                         ("unadopted_expired_prunable", False, True))
        self.assertEqual((by_c["c-ret-live"].classification, by_c["c-ret-live"].needs_owner_review,
                          by_c["c-ret-live"].remote_close_eligible, by_c["c-ret-live"].local_retire_eligible),
                         ("retired_still_valid", True, True, False))
        self.assertEqual((by_c["c-ret-ret"].classification, by_c["c-ret-ret"].local_retire_eligible),
                         ("retired_retained", False))
        self.assertEqual((by_c["c-ret-pru"].classification, by_c["c-ret-pru"].local_retire_eligible),
                         ("retired_prunable", True))

        # Uncertain exchanges are never auto-prunable even past retention.
        self.assertEqual((by_f["f-uncertain"].classification, by_f["f-uncertain"].needs_owner_review,
                          by_f["f-uncertain"].local_retire_eligible),
                         ("uncertain_exchange_review", True, False))
        self.assertEqual((by_f["f-stale"].classification, by_f["f-stale"].needs_owner_review,
                          by_f["f-stale"].local_retire_eligible),
                         ("stale_active_flow", True, False))
        self.assertEqual((by_f["f-cand-live"].classification, by_f["f-cand-live"].needs_owner_review,
                          by_f["f-cand-live"].candidate_consent_id),
                         ("flow_in_progress", False, "c-unad-prog"))
        self.assertEqual((by_f["f-term-ret"].classification, by_f["f-term-ret"].local_retire_eligible),
                         ("terminal_retained", False))
        self.assertEqual((by_f["f-term-pru"].classification, by_f["f-term-pru"].local_retire_eligible),
                         ("terminal_prunable", True))

        # Fractional retention check floors conservatively so records are never pruned early.
        frac = classify_consents(
            "owner", NOW + 0.4,
            [_consent("c-frac", status="RETIRED", valid_until=NOW - RET + 1.0)],
            [],
        )
        self.assertEqual(frac.consents[0].classification, "retired_retained")

        # Exact upper timestamp bound and retention_days=0 work cleanly without float precision loss.
        at_bound = classify_consents(
            "owner", MAX_TIMESTAMP,
            [_consent("c-max", status="RETIRED", created_at=MAX_TIMESTAMP - 1, valid_until=MAX_TIMESTAMP)],
            [],
            retention_days=0,
        )
        self.assertEqual(at_bound.consents[0].classification, "retired_prunable")

    def test_unresolved_flow_evidence_and_retired_pointer_invariants(self):
        # 1. Full matrix: ANY Hestia consent (status x validity x retention x mixed live+stale)
        #    referenced by exchange_uncertain or stale_active_flow gets review=True, remote=False, local=False.
        for flow_kind, expected_class in (("exchange_uncertain", "uncertain_flow_evidence"),
                                          ("stale", "stale_flow_evidence")):
            for status in ("ACTIVE", "UNADOPTED", "RETIRED"):
                for valid_until in (None, NOW + 600, NOW - 10, NOW - RET - 10):
                    for retention_days in (0, DEFAULT_RETENTION_DAYS):
                        for with_live_candidate in (False, True):
                            cid = "c-matrix"
                            flows = [
                                _flow("f-unresolved",
                                      status="exchange_uncertain" if flow_kind == "exchange_uncertain" else "candidate",
                                      created_at=NOW - RET - 50, expires_at=NOW - RET - 10,
                                      candidate_consent_id=cid),
                            ]
                            if with_live_candidate:
                                flows.append(_flow("f-live", status="candidate",
                                                   created_at=NOW - 10, expires_at=NOW + 600,
                                                   candidate_consent_id=cid))
                            finding = classify_consents(
                                "owner", NOW,
                                [_consent(cid, status=status, valid_until=valid_until)],
                                flows,
                                retention_days=retention_days,
                            ).consents[0]
                            self.assertEqual(
                                (finding.classification, finding.needs_owner_review,
                                 finding.remote_close_eligible, finding.local_retire_eligible),
                                (expected_class, True, False, False),
                            )

        # External/PoC sessions remain external_read_only even if referenced by unresolved flows.
        for prov in ("external_file", "poc_retired"):
            finding = classify_consents(
                "owner", NOW,
                [_consent("c-ext-unc", provenance=prov, status="UNADOPTED", valid_until=NOW - RET - 10)],
                [_flow("f-unc", status="exchange_uncertain", created_at=NOW - RET - 50,
                       expires_at=NOW - RET - 10, candidate_consent_id="c-ext-unc"),
                 _flow("f-stale", status="candidate", created_at=NOW - RET - 50,
                       expires_at=NOW - RET - 10, candidate_consent_id="c-ext-unc")],
                retention_days=0,
            ).consents[0]
            self.assertEqual((finding.classification, finding.needs_owner_review,
                              finding.remote_close_eligible, finding.local_retire_eligible),
                             ("external_read_only", False, False, False))

        # Resolved terminal flow referencing a consent does NOT block normal retention.
        resolved = classify_consents(
            "owner", NOW,
            [_consent("c-term-ok", status="UNADOPTED", valid_until=NOW - RET - 10)],
            [_flow("f-adopted", status="adopted", created_at=NOW - RET - 50,
                   expires_at=NOW - RET - 10, candidate_consent_id="c-term-ok")],
        ).consents[0]
        self.assertEqual((resolved.classification, resolved.local_retire_eligible),
                         ("unadopted_expired_prunable", True))

        # 2. Store invariant: RETIRED is only written on successor adoption, so a still-valid RETIRED
        #    consent is remote_close_eligible=True unless active_consent_ids still points to it.
        retired_live = _consent("c-ret-1", status="RETIRED", valid_until=NOW + 3600)
        without_ptr = classify_consents("owner", NOW, [retired_live], []).consents[0]
        self.assertEqual((without_ptr.classification, without_ptr.remote_close_eligible,
                          without_ptr.local_retire_eligible),
                         ("retired_still_valid", True, False))
        with_ptr = classify_consents("owner", NOW, [retired_live], [],
                                     active_consent_ids={"bank-a": "c-ret-1"}).consents[0]
        self.assertEqual((with_ptr.classification, with_ptr.needs_owner_review,
                          with_ptr.remote_close_eligible, with_ptr.local_retire_eligible),
                         ("active_pointer_mismatch", True, False, False))

    def test_permutation_invariance(self):
        consents = [
            _consent("c-1", status="ACTIVE", valid_until=NOW + 100),
            _consent("c-2", status="RETIRED", valid_until=NOW + 100),
            _consent("c-3", status="RETIRED", valid_until=NOW - RET),
            _consent("c-4", provenance="poc_retired", status="RETIRED", valid_until=NOW - RET),
        ]
        flows = [
            _flow("f-1", status="adopted", created_at=NOW - RET - 10, expires_at=NOW - RET),
            _flow("f-2", status="exchange_uncertain", created_at=NOW - 10, expires_at=NOW),
            _flow("f-3", status="created", created_at=NOW - 10, expires_at=NOW + 100),
        ]
        baseline = classify_consents("owner", NOW, consents, flows)
        rng = random.Random(20261121)
        for _ in range(25):
            pc, pf = list(consents), list(flows)
            rng.shuffle(pc)
            rng.shuffle(pf)
            self.assertEqual(classify_consents("owner", NOW, pc, pf), baseline)

    def test_validation_caps_and_fixed_error_messages(self):
        bad_calls = [
            lambda: classify_consents(" ", NOW, [], []),
            lambda: classify_consents(" " * 1025, NOW, [], []),
            lambda: classify_consents("owner\n", NOW, [], []),
            lambda: classify_consents("owner", True, [], []),
            lambda: classify_consents("owner", -1, [], []),
            lambda: classify_consents("owner", MAX_TIMESTAMP + 1, [], []),
            lambda: classify_consents("owner", float(MAX_TIMESTAMP + 1), [], []),
            lambda: classify_consents("owner", 2**53 + 1, [], []),
            lambda: classify_consents("owner", 10**400, [], []),
            lambda: classify_consents("owner", math.inf, [], []),
            lambda: classify_consents("owner", math.nan, [], []),
            lambda: classify_consents("owner", datetime(2026, 11, 21, tzinfo=timezone.utc), [], []),
            lambda: classify_consents("owner", NOW, [], [], retention_days=True),
            lambda: classify_consents("owner", NOW, [], [], retention_days=-1),
            lambda: classify_consents("owner", NOW, [], [], retention_days=MAX_RETENTION_DAYS + 1),
            lambda: classify_consents("owner", NOW, [], [], max_consents=True),
            lambda: classify_consents("owner", NOW, [], [], max_consents=-1),
            lambda: classify_consents("owner", NOW, [], [], max_flows=-1),
            lambda: classify_consents("owner", NOW, "not-iterable", []),
            lambda: classify_consents("owner", NOW, [], {"a": 1}),
            lambda: classify_consents("owner", NOW, ["bad"], []),
            lambda: classify_consents("owner", NOW, [], ["bad"]),
            lambda: classify_consents("owner", NOW, [_consent(owner="other")], []),
            lambda: classify_consents("owner", NOW, [], [_flow(owner="other")]),
            lambda: classify_consents("owner", NOW, [_consent(provenance="unknown")], []),
            lambda: classify_consents("owner", NOW, [_consent(status="DELETED")], []),
            lambda: classify_consents("owner", NOW, [_consent(generation=True)], []),
            lambda: classify_consents("owner", NOW, [_consent(generation=0)], []),
            lambda: classify_consents("owner", NOW, [_consent("dup"), _consent("dup")], []),
            lambda: classify_consents("owner", NOW, [_consent(consent_id="x" * 129)], []),
            lambda: classify_consents("owner", NOW, [_consent(created_at=-1)], []),
            lambda: classify_consents("owner", NOW, [_consent(valid_until=MAX_TIMESTAMP + 1)], []),
            lambda: classify_consents("owner", NOW, [], [_flow(status="unknown")]),
            lambda: classify_consents("owner", NOW, [], [_flow("dup"), _flow("dup")]),
            lambda: classify_consents("owner", NOW, [], [_flow(created_at=NOW, expires_at=NOW - 1)]),
            lambda: classify_consents("owner", NOW, [], [_flow(candidate_consent_id=" ")]),
            lambda: classify_consents("owner", NOW, [], [], active_consent_ids=["not-a-mapping"]),
            lambda: classify_consents("owner", NOW, [], [], active_consent_ids={" ": "c-1"}),
            lambda: classify_consents("owner", NOW, [], [], active_consent_ids={"bank-a": " "}),
        ]
        for call in bad_calls:
            with self.assertRaises(ConsentReconcileInputError) as exc:
                call()
            self.assertEqual(str(exc.exception), "Invalid consent reconciliation input")

        self.assertEqual(classify_consents("owner", NOW, [], [], max_consents=0, max_flows=0).consents, ())
        with self.assertRaises(ConsentReconcileLimitError) as exc:
            classify_consents("owner", NOW, [_consent("a"), _consent("b")], [], max_consents=1)
        self.assertEqual(str(exc.exception), "Consent reconciliation limit exceeded")
        with self.assertRaises(ConsentReconcileLimitError) as exc:
            classify_consents("owner", NOW, [], [_flow("a"), _flow("b")], max_flows=1)
        self.assertEqual(str(exc.exception), "Consent reconciliation limit exceeded")

        at_c = [_consent(f"c-{i:04d}") for i in range(MAX_CONSENTS)]
        self.assertEqual(len(classify_consents("owner", NOW, at_c, []).consents), MAX_CONSENTS)
        with self.assertRaises(ConsentReconcileLimitError):
            classify_consents("owner", NOW, [*at_c, _consent("c-over")], [])

        at_f = [_flow(f"f-{i:04d}") for i in range(MAX_FLOWS)]
        self.assertEqual(len(classify_consents("owner", NOW, [], at_f).flows), MAX_FLOWS)
        with self.assertRaises(ConsentReconcileLimitError):
            classify_consents("owner", NOW, [], [*at_f, _flow("f-over")])

    def test_repr_redacts_all_private_fields(self):
        consent = _consent("con-secret-11", owner="alice-secret", connection_id="conn-secret-22")
        flow = _flow("flow-secret-33", owner="alice-secret", connection_id="conn-secret-22",
                     candidate_consent_id="con-secret-11")
        report = classify_consents("alice-secret", NOW, [consent], [flow])
        combined = repr(consent) + repr(flow) + repr(report) + repr(report.consents[0]) + repr(report.flows[0])
        for token in ("alice-secret", "con-secret-11", "conn-secret-22", "flow-secret-33"):
            self.assertNotIn(token, combined)


if __name__ == "__main__":
    unittest.main()
