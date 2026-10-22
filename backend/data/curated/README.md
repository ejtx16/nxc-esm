# Curated Excel answer key

Used by the **Run Excel accuracy check** button on the KPIs page (`app/kpi/excel_check.py`, scenario `excel_replay`).
The check replays these rows through the agents in an isolated database (`data/kpi_excel.db`) and grades each decision
against `expected_*`. Regenerate with `.venv/bin/python scripts/build_curated.py`.

Every Excel row is kept. `excel_label` is the spreadsheet value, `expected_*` the reviewed label, and `changed=Y`
marks a difference, with the reason. Labels were reviewed by the prototype team, not the client, so a service desk lead
should review them before any accuracy figure is quoted.

| File | Rows | Relabelled | Rule |
|---|---|---|---|
| `alerts_expected.csv` | 80 (Alert Log) | 0 | `True_Positive` kept: it agrees with `Resolution_Notes` on every row. |
| `incidents_expected.csv` | 50 (Incident Log) | 47 | The log labels each of its 25 descriptions two different ways, so labels come from `data/golden/triage_labels.csv` (15 descriptions) plus 10 reviewed in `scripts/build_curated.py`. Most changes are priority only. |
| `srs_expected.csv` | 1,310 | 5 | The 60 logged SRs plus one month at the Automation Opportunity volumes. Self-service = password reset, or standard in-role access to a non-privileged system (`config/entitlements.yaml`). Access requests are 85% in role, 10% out of role and 5% admin (seeded, reproducible). Self-service catalogue items arrive via the portal. `changed` compares with `Automation_Potential=High` for the logged rows. |
| `kb_expected.csv` | 30 | 8 | `Recommended_Action`, except that the older article of each duplicated title is archived. |

Baselines are recomputed from the spreadsheets at run time: alert noise 32.5%, ack time 27.0 min, self-service 15%
(the share rated High automation potential; none of the logged SRs were self-served) and KB accuracy 69.4%. Triage time
(18 min) is the case-study figure, because the spreadsheets do not record it.

## Simulated alert stream (not Excel data)

`alerts_simulated_expected.csv` is 300 synthetic alerts from `scripts/build_simulated_alerts.py` (scenario `alert_noise_sim`).
The 80 logged alerts have labels with no visible pattern, so they cannot show whether the noise logic works. This stream has
a pattern (flapping signatures on chatty systems that self-recover) with about 12% of rows deliberately breaking it. The check
reports it under the alert-noise KPI as `simulated_*` fields. It is a simulation, not a measurement of real alerts.
