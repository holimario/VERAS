# Case-study Controller Profiles

The case-study VERAS profiles use the v0.30 controller with the performance
instructions defined in `veras/src/veras_harness/profiles.py`.

| Setting | Data-center placement | 54-bus network planning |
| --- | --- | --- |
| Profile | `datacenter` | `power-grid` |
| Model | GPT-5.6 Sol | GPT-5.6 Sol |
| Reasoning effort | `xhigh` | `xhigh` |
| Agent wall-time limit | 19,800 s | 15,360 s |
| Timeout scale | 3.0 | 2.0 |
| Recovery reserve fraction | 0.40 | 0.40 |
| Challenger cap | 180 s | 480 s |
| Candidate execution contract | 120-s generator | 900-s solver, four threads |

The frozen controller SHA-256 is
`47e8a7a68f59f558daf254299b208041c3bd15a60d53543ebb42c211d9edfc69`.
The data-center fork SHA-256 is
`2bb70f5d7fca26c17e7fb13614e7d77dce96ff3c54991f5bd2eef13a7c908360`;
the power-grid fork SHA-256 is
`809bf5629dc1bb578dffbbea4db888ec1d0742b0a32fed74a6a4a4d07fc061d4`.

Native Codex runs use the native runner. The two `comparison.json` records
contain the model settings, input hashes, runner hashes, and evaluation
metrics:

- `data_center/comparison.json`
- `power_grid/comparison.json`

For grid source replay, the recorded solver limit is 750 seconds. End-to-end
replay takes 823.924 seconds for Native Codex and 829.552 seconds for VERAS.
