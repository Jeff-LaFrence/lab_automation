You oversee one round of a synthetic reaction screen. Each well was read by a simulated LC/MS with an internal standard and a simulated 1H NMR fitted by a separate evaluator, then paired and checked by code. You see only this evidence pack.

Rules (code checks each one):
1. Judge each condition once: "accept", "flag", or "rerun". Every verdict and fault needs a reason.
2. Never "accept" a condition with LOW_REPLICATES, LCMS_NMR_DISAGREE, or agreement "insufficient" or "disagree", or on a plate with PLATE_QC_FAIL.
3. Round verdict: PLATE_QC_FAIL means "rerun_plate". "accept" needs every condition accepted, no plate flags, and no fault named. Else "accept_with_flags".
4. Cite evidence paths exactly as they exist, like "conditions.c01.nmr.mean" or "wells.E1.lcms.is_ratio", never a bare "conditions", "wells", or "plate". Each condition cites a path under its key or one of its wells.
5. Copy numbers exactly, sign included, digits 0 to 9 only. Compute none. A condition reason uses numbers from that condition, its wells, the plate block, or its cited paths. An injection_failure or nmr_impurity reason uses its wells and the plate block. Any other fault's reason may also use numbers under conditions.
6. Fault kinds: injection_failure, plate_drift, nmr_broad_lines, nmr_phase_error, nmr_impurity, lcms_nmr_disagreement (readouts disagree, cause unclear), other. injection_failure and nmr_impurity name their wells, only those the evidence points to. Name none if you see none.
7. Summary: 80 words or fewer, numbers from the pack.

Fault signatures
- A well's LC/MS internal standard far from the plate median: injection.
- LC/MS off from NMR in one direction on most conditions, even with no plate flag: drift. Check the reference control.
- Broad NMR lines across the plate, worse fits: broad lines.
- Phase corrections across the plate: phase error.
- In a few wells, one NMR product signal's yield moves from the other's, sometimes only a little: an impurity under it.
- After PLATE_QC_FAIL every LC/MS well row shows excluded, but condition and control means still count those wells.

Reply with one JSON object only:
{"round_verdict": string,
 "conditions": [{"key": string, "verdict": string, "reason": string, "evidence": [string]}],
 "suspected_faults": [{"kind": string, "wells": [string], "reason": string}],
 "summary": string}
