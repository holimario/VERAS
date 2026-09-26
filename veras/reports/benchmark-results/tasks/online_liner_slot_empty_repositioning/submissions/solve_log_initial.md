# Solve Log: Initial Stage

## Solver

- Status: `optimal`
- Time limit seconds: `60.0`
- Elapsed seconds: `0.0543`
- Warm start added from rollback incumbent: `True`
- Incumbent count: `1`
- SCIP candidate objective: `5157.00000000`
- Dual bound: `5157.00000000`
- Relative gap: `0.00000000`
- Published candidate: `False`

## Guarded Comparison

- Rollback incumbent audited objective: `5157.00000000`
- Candidate audited objective: `5157.00000000`
- Final published audited objective: `5157.00000000`
- Rollback incumbent audit ok: `True`
- Candidate audit ok: `True`
- Final artifact audit ok: `True`

## State Vector

- Artifact present: `True`
- Decision coverage: `54/54`
- Maximum normalized residual theta: `0.00000000`
- Violated hard-constraint groups: `0`
- Export integrality residual: `0.00000000`
- Independent audited objective: `5157.00000000`

## Instance Summary

- Visible bookings: `54`
- Enumerated feasible booking paths: `55`
- Bookings with no feasible path: `B001, B034`
- Minimum acceptance floors: `{'contract': 11, 'premium': 13, 'spot': 5}`

## Objective Components

- Accepted booking revenue: `30601.00000000`
- Rejection penalties: `1792.00000000`
- Empty reposition and carbon cost: `1056.00000000`
- Lease cost: `0.00000000`
- Customs inspection cost: `2775.00000000`
- Empty holding cost: `19821.00000000`

## Audit

- Independent validation was rerun from the exported CSV, not from model variables.
- Corrupted-copy negative audit rejected artifact: `True`
- Restoration event repaired: `empty_move.day` is now exported and checked against `vessel_legs.depart_day`.
- Gate handling assumption: accepted bookings consume gate-out at the origin first-loaded day; empty releases, empty arrivals, and leases consume gate-in.
- Carbon assumption: `carbon_kg_per_teu` is monetized with unit coefficient `1.0` because no separate carbon-price table is provided.
