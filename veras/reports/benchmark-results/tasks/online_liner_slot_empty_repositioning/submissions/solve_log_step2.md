# Solve Log: Step 2 Replan

## Repair Phase

- Rollback source: `step2_incumbent`
- Rollback artifact: `/app/submissions/step2_replan.csv`
- Rollback audited objective: `22693.30000000`
- Rollback audit ok: `True`
- Rollback theta: `0.00000000`
- Rollback violated groups: `0`
- Effective visible stage: `step2`
- `current_step.json` label: `initial`
- Stage-label mismatch noted: `True`

## Performance Phase

- Strategy: `bounded exact MIP with rollback incumbent as MIP start`
- Warm start added: `True`
- Local branching attempted: `False`
- Status: `optimal`
- Time limit seconds: `60.0`
- Elapsed seconds: `0.0605`
- Incumbent count: `1`
- SCIP candidate objective: `22693.30000000`
- Dual bound: `22693.30000000`
- Relative gap: `0.00000000`
- Exact proof available: `True`

## Replan State

- Current day from file: `0`
- Freeze-until day from file: `0`
- Frozen accepted booking prefixes: `0`
- Protected-slot rules: `3`
- Transfer-cap rules: `3`

## Guarded Comparison

- Rollback audited objective: `22693.30000000`
- Candidate audited objective: `22693.30000000`
- Candidate audit ok: `True`
- Candidate minus rollback: `0.00000000`
- Final published audited objective: `22693.30000000`
- Final artifact audit ok: `True`
- Published candidate: `False`

## State Vector

- Artifact present: `True`
- Decision coverage: `88/88`
- Maximum normalized residual theta: `0.00000000`
- Violated hard-constraint groups: `0`
- Export integrality residual: `0.00000000`
- Independent audited objective: `22693.30000000`

## Instance Summary

- Visible bookings: `88`
- Enumerated feasible booking paths: `93`
- Bookings with no feasible path: `B001, B034`
- Minimum acceptance floors: `{'contract': 22, 'premium': 20, 'spot': 8}`

## Objective Components

- Accepted booking revenue: `50923.00000000`
- Rejection penalties: `2267.00000000`
- Empty reposition and carbon cost: `2898.00000000`
- Lease cost: `0.00000000`
- Customs inspection cost: `4728.00000000`
- Empty holding cost: `18336.70000000`
- Replanning change penalty: `0.00000000`

## Audit

- The rollback incumbent was reparsed from disk and audited before optimization.
- Candidate publication was gated on a fresh audit of the written candidate CSV.
- Independent validation was rerun from the exported CSV, not from model variables.
- Corrupted-copy negative audit rejected artifact: `True`
- Stage-2 audit adds protected-slot floors, transfer caps, frozen commitments, and change-penalty recomputation on top of the accepted step-1 checker.
- Gate handling assumption: accepted bookings consume gate-out at the origin first-loaded day; empty releases, empty arrivals, and leases consume gate-in.
- Carbon assumption: `carbon_kg_per_teu` is monetized with unit coefficient `1.0` because no separate carbon-price table is provided.
