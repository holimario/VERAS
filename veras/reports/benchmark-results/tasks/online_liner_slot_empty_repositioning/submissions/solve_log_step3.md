# Solve Log: Step 3 Final Replan

## Repair Phase

- Rollback source: `step3_incumbent`
- Rollback artifact: `/app/submissions/step3_replan.csv`
- Rollback audited objective: `46805.30000000`
- Rollback audit ok: `True`
- Rollback theta: `0.00000000`
- Rollback violated groups: `0`
- Effective visible stage: `step3`
- `current_step.json` label: `initial`
- Stage-label mismatch noted: `True`
- Rollback prior-commitment changes vs step 2: `2` (B061, B069)
- Rollback frozen-window violations under public `freeze_until_day=0`: `0`

## Performance Phase

- Strategy: `bounded exact MIP with rollback incumbent as MIP start`
- Warm start added: `True`
- Local branching attempted: `False`
- Status: `optimal`
- Time limit seconds: `295.0`
- Elapsed seconds: `0.0755`
- Incumbent count: `2`
- SCIP candidate objective: `46805.30000000`
- Dual bound: `46805.30000000`
- Relative gap: `0.00000000`
- Exact proof available: `True`

## Replan State

- Current day from file: `0`
- Freeze-until day from file: `0`
- Frozen accepted booking prefixes: `0`
- Protected-slot rules: `4`
- Transfer-cap rules: `4`
- Special-booking rules: `2`

## Guarded Comparison

- Rollback audited objective: `46805.30000000`
- Candidate audited objective: `46805.30000000`
- Candidate audit ok: `True`
- Candidate minus rollback: `0.00000000`
- Candidate prior-commitment changes vs step 2: `2`
- Candidate frozen-window violations under public `freeze_until_day=0`: `0`
- Final published audited objective: `46805.30000000`
- Final artifact audit ok: `True`
- Published candidate: `False`

## State Vector

- Artifact present: `True`
- Decision coverage: `128/128`
- Maximum normalized residual theta: `0.00000000`
- Violated hard-constraint groups: `0`
- Export integrality residual: `0.00000000`
- Independent audited objective: `46805.30000000`

## Instance Summary

- Visible bookings: `128`
- Enumerated feasible booking paths: `133`
- Bookings with no feasible path: `B001, B034`
- Minimum acceptance floors: `{'contract': 32, 'premium': 32, 'spot': 10}`
- Mandatory medical reefer bookings: `B099, B103, B126, B127`
- Final prior-commitment changes vs step 2: `2` (B061, B069)

## Objective Components

- Accepted booking revenue: `77956.00000000`
- Rejection penalties: `2783.00000000`
- Empty reposition and carbon cost: `2460.00000000`
- Lease cost: `1309.00000000`
- Customs inspection cost: `6746.00000000`
- Empty holding cost: `17521.70000000`
- Replanning change penalty: `331.00000000`

## Audit

- The rollback incumbent was reparsed from disk and audited before optimization.
- Candidate publication was gated on a fresh audit of the written candidate CSV.
- Independent validation was rerun from the exported CSV, not from model variables.
- Corrupted-copy negative audit rejected artifact: `True`
- Stage-3 audit adds protected-slot floors, transfer caps, frozen commitments, special-booking mandates, and change-penalty recomputation on top of the accepted step-1 checker.
- Legacy step-2 commitment edits remain publicly identifiable, but with `freeze_until_day=0` the public frozen-prefix audit reports `0` violations.
- Gate handling assumption: accepted bookings consume gate-out at the origin first-loaded day; empty releases, empty arrivals, and leases consume gate-in.
- Carbon assumption: `carbon_kg_per_teu` is monetized with unit coefficient `1.0` because no separate carbon-price table is provided.
