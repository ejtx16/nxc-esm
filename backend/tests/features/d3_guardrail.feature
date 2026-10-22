Feature: Guardrail prevents unsafe auto-resolution (demo item D3)

  Scenario: Auto-resolve proposed for a Tier-0 P1 incident
    Given the LicenseCore outage scenario has created a P1 incident
    And the triage agent proposes auto-resolution with confidence 0.92
    When the guardrail evaluates the proposal
    Then the verdict is "RequireApproval"
    And the failed rules include "allowed_priorities" and "max_app_tier_blocked"
    And an approval request appears in the guardrail queue
    And an audit record is written with the verdict and reasons

  Scenario: A human approval executes the blocked action and ends the surge
    Given the LicenseCore outage scenario has created a P1 incident
    When a human approves the blocked auto-resolution
    Then the incident is resolved by its runbook
    And surge mode is switched off

  Scenario: Low-risk work is approved when every rule passes
    Given a P3 incident on a Tier-3 application with a trusted runbook and KB article outside any freeze window
    When the guardrail evaluates a resolve proposal with confidence 0.92
    Then the verdict is "Approve"

  Scenario: The kill switch forces every action through human approval
    Given the seeded NexaCore database
    And the global kill switch is on
    When a Critical "LicenseCore" alert arrives
    Then no incident is opened autonomously
    And approvals are waiting for "acknowledge" and "open_incident"
