# Solve Log

## Commands

- `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`

## Incumbent Audit

- Incumbent feasible: `True`
- Incumbent objective: `-6563.54666926`

## Performance Phase

- Mode: `GUARDED REPAIR-AND-PERFORMANCE`
- Bounded neighborhood search: exact enumeration over delayed single-flight requests
- Time limit used: `120.0` seconds
- Charge LP status: `optimal`
- Charge LP dual bound: `61.55129771`
- Charge LP gap: `0.00000000`
- Phase-I max normalized violation `theta`: `0`
- Phase-I total normalized violation: `2.73575356332e-11`

## Candidate Audit

- Candidate feasible: `True`
- Candidate objective: `-6415.94666926`
- Candidate CVaR tail term: `7820.31739128`
- Published candidate: `True`
- Neighborhood `S_CLEAR` explored `150` combinations; best overrides `{'R022': 12, 'R024': 13, 'R035': 17}`
- Neighborhood `S_WIND_NORTH` explored `150` combinations; best overrides `{'R022': 14, 'R024': 13, 'R035': 17}`
- Neighborhood `S_STORM_CELLS` explored `150` combinations; best overrides `{'R022': 14, 'R024': 15, 'R035': 19}`
- Candidate scenario profit `S_CLEAR`: `-4020.31739128`
- Candidate scenario profit `S_WIND_NORTH`: `-4073.45974952`
- Candidate scenario profit `S_STORM_CELLS`: `-4188.27415691`
- Public-data checks passed for acceptance, timing, energy, pads, parking, chargers, grid, corridors, noise, windows, reserves, and group spreads.
